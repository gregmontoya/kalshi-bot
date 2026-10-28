"""WebSocket paper book: live Kalshi order books + Pinnacle fair value, evaluated on every update.

PAPER ONLY. Places no orders. Runs alongside the 5-minute REST recorder in the same process and writes to
`ws_paper_orders` so we can compare it against the REST paper book (`paper_orders`) and the live $50 book.

Order-book math follows LAW 1: Kalshi sends only YES bids and NO bids.
    best_yes_ask = 1.00 - best_no_bid
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import websockets

from src.recorder.analysis import _fair_prob

WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
WS_PATH = "/trade-api/ws/v2"
MIN_EDGE = 0.02
PRICE_MIN, PRICE_MAX = 0.20, 0.90
WINDOW_BEFORE_H = 6.0
MIN_LEAD_MIN = 10
EVAL_THROTTLE_S = 2.0        # evaluate each ticker at most every 2s
FAIR_CACHE_S = 60.0          # re-read Pinnacle fair from DB at most every 60s per ticker
RESUB_EVERY_S = 60.0         # pick up newly matched markets
STATS_EVERY_S = 300.0

log = logging.getLogger("ws_paper")


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class Book:
    """One market's YES-bid and NO-bid ladders (price -> contracts)."""

    def __init__(self) -> None:
        self.yes: dict[float, float] = {}
        self.no: dict[float, float] = {}

    def load_snapshot(self, msg: dict) -> None:
        self.yes = {float(p): float(q) for p, q in (msg.get("yes_dollars_fp") or []) if float(q) > 0}
        self.no = {float(p): float(q) for p, q in (msg.get("no_dollars_fp") or []) if float(q) > 0}

    def apply_delta(self, msg: dict) -> None:
        side = self.yes if msg.get("side") == "yes" else self.no
        p, d = float(msg["price_dollars"]), float(msg["delta_fp"])
        q = side.get(p, 0.0) + d
        if q > 1e-9:
            side[p] = q
        else:
            side.pop(p, None)

    def best_yes_bid(self) -> tuple[float | None, float]:
        if not self.yes:
            return None, 0.0
        p = max(self.yes)
        return p, self.yes[p]

    def best_yes_ask(self) -> tuple[float | None, float]:
        """LAW 1: YES ask = 1 - best NO bid; depth = contracts resting at that NO bid."""
        if not self.no:
            return None, 0.0
        p = max(self.no)
        return round(1.0 - p, 4), self.no[p]


def maker_price(bid: float | None, ask: float) -> float:
    """Same rule as the REST paper book: improve the bid by 1c if the spread allows, else ask - 1c."""
    b = bid if bid else max(0.01, ask - 0.02)
    return round(b + 0.01, 4) if ask - b >= 0.02 else round(max(0.01, ask - 0.01), 4)


class WsPaperBook:
    def __init__(self, conn, headers_fn) -> None:
        self.conn = conn
        self.headers_fn = headers_fn
        self.books: dict[str, Book] = {}
        self.meta: dict[str, dict] = {}
        self.subscribed: set[str] = set()
        self.done_events: set[str] = set()
        self.last_eval: dict[str, float] = {}
        self.fair_cache: dict[str, tuple[float, float | None, float | None]] = {}
        self.msgs = 0
        self.signals = 0
        self.fills = 0

    # ---------- universe ----------
    def wanted(self) -> dict[str, dict]:
        now = datetime.now(timezone.utc)
        rows = self.conn.execute(
            "SELECT * FROM market_map WHERE commence_time>? AND commence_time<=?",
            (_iso(now + timedelta(minutes=MIN_LEAD_MIN)), _iso(now + timedelta(hours=WINDOW_BEFORE_H))),
        ).fetchall()
        out = {r["ticker"]: dict(r) for r in rows}
        # keep tracking tickers with resting ws paper orders until tip-off (for fill detection)
        for r in self.conn.execute("SELECT ticker FROM ws_paper_orders WHERE status='RESTING'").fetchall():
            if r["ticker"] not in out:
                m = self.conn.execute("SELECT * FROM market_map WHERE ticker=?", (r["ticker"],)).fetchone()
                if m:
                    out[r["ticker"]] = dict(m)
        return out

    # ---------- signal logic ----------
    def _fair(self, tk: str, m: dict) -> tuple[float | None, float | None]:
        now = time.time()
        c = self.fair_cache.get(tk)
        if c and now - c[0] < FAIR_CACHE_S:
            return c[1], c[2]
        at = datetime.now(timezone.utc)
        fair = _fair_prob(self.conn, m["odds_event_id"], m["odds_outcome"], at)
        row = self.conn.execute(
            "SELECT MAX(ts) FROM odds_snapshots WHERE odds_event_id=? AND book='pinnacle'", (m["odds_event_id"],)
        ).fetchone()
        age = (at - _parse(row[0])).total_seconds() if row and row[0] else None
        self.fair_cache[tk] = (now, fair, age)
        return fair, age

    def evaluate(self, tk: str) -> None:
        m, book = self.meta.get(tk), self.books.get(tk)
        if not m or not book:
            return
        now_s = time.time()
        bid, bid_q = book.best_yes_bid()
        ask, ask_q = book.best_yes_ask()
        if ask is None or not (0 < ask < 1):
            return
        ts = _iso(datetime.now(timezone.utc))

        # fill check for resting ws paper orders: someone is offering at or below our bid
        po = self.conn.execute(
            "SELECT id, maker_price, commence_time FROM ws_paper_orders WHERE ticker=? AND status='RESTING'", (tk,)
        ).fetchone()
        if po:
            ct = _parse(po["commence_time"])
            if ct and datetime.now(timezone.utc) >= ct:
                self.conn.execute("UPDATE ws_paper_orders SET status='UNFILLED' WHERE id=?", (po["id"],))
            elif ask <= po["maker_price"] + 1e-9:
                self.conn.execute("UPDATE ws_paper_orders SET status='FILLED', filled_ts=? WHERE id=?",
                                  (ts, po["id"]))
                self.fills += 1
                log.info("WS FILL   %-30s @ %.2f", tk, po["maker_price"])
            self.conn.commit()
            return

        if m["event_ticker"] in self.done_events:
            return
        if now_s - self.last_eval.get(tk, 0.0) < EVAL_THROTTLE_S:
            return
        self.last_eval[tk] = now_s
        ct = _parse(m["commence_time"])
        if not ct or ct <= datetime.now(timezone.utc) + timedelta(minutes=MIN_LEAD_MIN):
            return
        mp = maker_price(bid, ask)
        if not (PRICE_MIN <= mp <= PRICE_MAX):
            return
        fair, age = self._fair(tk, m)
        if fair is None or fair - mp < MIN_EDGE:
            return
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO ws_paper_orders
               (placed_ts, ticker, event_ticker, series, odds_event_id, commence_time, maker_price,
                bid_at_place, ask_at_place, fair_at_place, edge_at_place, odds_age_s, bid_depth, ask_depth, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'RESTING')""",
            (ts, tk, m["event_ticker"], m["series"], m["odds_event_id"], m["commence_time"], mp,
             bid, ask, fair, fair - mp, age, bid_q, ask_q),
        )
        self.conn.commit()
        self.done_events.add(m["event_ticker"])
        if cur.rowcount:
            self.signals += 1
            log.info("WS PLACE  %-30s bid=%.2f (book %.2f/%.2f, depth %d/%d, fair=%.3f edge=+%.3f, odds age %s)",
                     tk, mp, bid or 0, ask, bid_q, ask_q, fair, fair - mp,
                     f"{age/60:.0f}m" if age is not None else "?")

    # ---------- message handling ----------
    def handle(self, msg: dict) -> None:
        typ, body = msg.get("type"), msg.get("msg") or {}
        tk = body.get("market_ticker")
        if typ == "orderbook_snapshot" and tk:
            self.books.setdefault(tk, Book()).load_snapshot(body)
        elif typ == "orderbook_delta" and tk:
            if tk not in self.books:
                return
            self.books[tk].apply_delta(body)
        elif typ == "error":
            log.warning("WS error: %s", body)
            return
        else:
            return
        self.msgs += 1
        self.evaluate(tk)

    async def run_once(self) -> None:
        hdr = self.headers_fn("GET", WS_PATH)
        hdr.pop("Content-Type", None)
        self.books.clear()
        self.subscribed.clear()
        self.done_events = {r[0] for r in self.conn.execute("SELECT event_ticker FROM ws_paper_orders").fetchall()}
        last_seq: dict[int, int] = {}
        async with websockets.connect(WS_URL, additional_headers=hdr, ping_interval=20, max_size=2**24) as ws:
            log.info("WS connected")
            next_resub, next_stats, cmd_id = 0.0, time.time() + STATS_EVERY_S, 1
            while True:
                now = time.time()
                if now >= next_resub:
                    want = self.wanted()
                    self.meta.update(want)
                    new = sorted(set(want) - self.subscribed)
                    if new:
                        cmd_id += 1
                        await ws.send(json.dumps({"id": cmd_id, "cmd": "subscribe",
                                                  "params": {"channels": ["orderbook_delta"],
                                                             "market_tickers": new}}))
                        self.subscribed.update(new)
                        log.info("WS subscribed +%d markets (%d total)", len(new), len(self.subscribed))
                    next_resub = now + RESUB_EVERY_S
                if now >= next_stats:
                    log.info("WS stats: %d book updates, %d markets, %d signals, %d fills",
                             self.msgs, len(self.books), self.signals, self.fills)
                    next_stats = now + STATS_EVERY_S
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=10)
                except asyncio.TimeoutError:
                    continue
                msg = json.loads(raw)
                sid, seq = msg.get("sid"), msg.get("seq")
                if sid is not None and seq is not None:
                    prev = last_seq.get(sid)
                    if prev is not None and seq != prev + 1 and msg.get("type") == "orderbook_delta":
                        raise RuntimeError(f"sequence gap on sid {sid}: {prev} -> {seq}")
                    last_seq[sid] = seq
                self.handle(msg)


async def run_forever(conn, headers_fn) -> None:
    book = WsPaperBook(conn, headers_fn)
    backoff = 2.0
    while True:
        try:
            await book.run_once()
            backoff = 2.0
        except asyncio.CancelledError:
            raise
        except Exception as e:  # never take down the recorder
            log.warning("WS disconnected (%s); reconnecting in %.0fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
