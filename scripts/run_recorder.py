"""Recorder daemon: snapshots Kalshi game markets + Pinnacle odds + settlements + Paper Maker Shadow Book.

Places NO live orders and uses NO Kalshi credentials (public market data only).

Usage:
    PYTHONPATH=. .venv/bin/python scripts/run_recorder.py            # run forever
    PYTHONPATH=. .venv/bin/python scripts/run_recorder.py --once     # one full cycle then exit
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv

from src.recorder import sources, store
from src.recorder.analysis import _fair_prob
from src.recorder.live_maker import LiveMaker
from src.recorder.matching import match_event, outcome_name_for

# Kalshi series -> The Odds API sport key. Only leagues where team-name matching is unambiguous.
LEAGUES = {
    "KXNBAGAME": "basketball_nba",
    "KXNHLGAME": "icehockey_nhl",
    "KXNFLGAME": "americanfootball_nfl",
    "KXEPLGAME": "soccer_epl",
    "KXMLBGAME": "baseball_mlb",
}
KALSHI_EVERY_S = 300          # Kalshi prices: every 5 min (free)
ODDS_EVERY_S = 1800           # Pinnacle odds per active sport: every 30 min (1 credit each)
EVENTS_EVERY_S = 1800         # Free odds event list (drives matching + activity window)
SETTLE_EVERY_S = 1800         # Settlement checks
ODDS_CREDIT_FLOOR = 20        # Stop spending odds credits below this
WINDOW_BEFORE_H = 6.0         # Sport is "active" if a game starts within 6h...
WINDOW_AFTER_H = 3.5          # ...or started less than 3.5h ago
KALSHI_HORIZON_H = 48         # Only snapshot markets expiring within 48h (keeps DB small)
PAPER_MIN_EDGE = 0.02         # Rest paper Maker order when (Pinnacle fair - maker_price) >= 2.0c

log = logging.getLogger("recorder")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def sport_active(events: list[dict], now: datetime) -> bool:
    for ev in events:
        ct = _parse(ev.get("commence_time"))
        if ct and now - timedelta(hours=WINDOW_AFTER_H) <= ct <= now + timedelta(hours=WINDOW_BEFORE_H):
            return True
    return False


class Recorder:
    def __init__(self, conn, api_key: str | None, kalshi_every_s: int = KALSHI_EVERY_S,
                 live_maker: LiveMaker | None = None):
        self.conn = conn
        self.api_key = api_key
        self.kalshi_every_s = kalshi_every_s
        self.live_maker = live_maker
        self.last_run: dict[str, float] = defaultdict(float)
        self.events: dict[str, list[dict]] = {}
        self.open_markets: dict[str, list[dict]] = {}
        self.credits_remaining: int | None = None

    def due(self, key: str, every: float) -> bool:
        if time.time() - self.last_run[key] >= every:
            self.last_run[key] = time.time()
            return True
        return False

    async def kalshi_cycle(self, client) -> None:
        ts, now = now_iso(), datetime.now(timezone.utc)
        for series in LEAGUES:
            try:
                mkts = await sources.kalshi_open_markets(client, series)
            except sources.SourceError as e:
                log.error("Kalshi %s failed: %s", series, e)
                continue
            self.open_markets[series] = mkts
            near = [m for m in mkts if (exp := _parse(m.get("expected_expiration_time")))
                    and exp <= now + timedelta(hours=KALSHI_HORIZON_H)]
            n = store.insert_kalshi_markets(self.conn, ts, series, near)
            log.info("Kalshi %-10s %3d open, %3d snapshotted", series, len(mkts), n)

    async def events_cycle(self, client) -> None:
        if not self.api_key:
            return
        for series, sport in LEAGUES.items():
            try:
                self.events[sport] = await sources.odds_events(client, self.api_key, sport)
            except sources.SourceError as e:
                log.error("Odds events %s failed: %s", sport, e)
                continue
            self.match_series(series, sport)

    def match_series(self, series: str, sport: str) -> None:
        by_event: dict[str, list[dict]] = defaultdict(list)
        for m in self.open_markets.get(series, []):
            by_event[m.get("event_ticker", "")].append(m)
        matched = unmatched = 0
        ts = now_iso()
        for ev_ticker, mkts in by_event.items():
            ev = match_event(ev_ticker, [m.get("yes_sub_title", "") for m in mkts], self.events.get(sport, []))
            if not ev:
                unmatched += 1
                continue
            for m in mkts:
                oc = outcome_name_for(m.get("yes_sub_title", ""), ev)
                if oc:
                    store.upsert_map(self.conn, m["ticker"], ev_ticker, series, sport, ev["id"], oc,
                                     ev.get("commence_time"), ts)
            matched += 1
        self.conn.commit()
        log.info("Match  %-10s %3d events matched, %3d unmatched (no odds listing or ambiguous)",
                 series, matched, unmatched)

    async def odds_cycle(self, client) -> None:
        if not self.api_key:
            log.warning("No THE_ODDS_API_KEY set; skipping Pinnacle odds")
            return
        now = datetime.now(timezone.utc)
        for sport in LEAGUES.values():
            if not sport_active(self.events.get(sport, []), now):
                continue
            if self.credits_remaining is not None and self.credits_remaining <= ODDS_CREDIT_FLOOR:
                log.warning("Odds credits at %s (floor %s); pausing odds polling", self.credits_remaining,
                            ODDS_CREDIT_FLOOR)
                return
            try:
                evs, usage = await sources.pinnacle_odds(client, self.api_key, sport)
            except sources.SourceError as e:
                log.error("Pinnacle %s failed: %s", sport, e)
                continue
            self.credits_remaining = usage["remaining"]
            n = store.insert_odds(self.conn, now_iso(), sport, evs)
            store.log_usage(self.conn, now_iso(), f"odds:{sport}", usage["remaining"], usage["used"], usage["last"])
            log.info("Odds   %-22s %3d events, %4d outcome rows, credits left %s", sport, len(evs), n,
                     usage["remaining"])

    async def paper_cycle(self, client: httpx.AsyncClient) -> None:
        """Paper Maker Shadow Book (+ optional $50 Hard-Fenced Micro-Live Maker): places resting Maker bids."""
        now = datetime.now(timezone.utc)
        ts = now_iso()

        # 1. Update existing RESTING orders (fill check + pre-game expiry) and record closing_mid
        resting = self.conn.execute("SELECT * FROM paper_orders WHERE status='RESTING'").fetchall()
        for po in resting:
            ct = _parse(po["commence_time"])
            if not ct:
                continue
            fill = self.conn.execute(
                """SELECT ts, yes_bid, yes_ask, last_price FROM kalshi_snapshots
                   WHERE ticker=? AND ts>? AND ts<?
                   AND ((yes_ask IS NOT NULL AND yes_ask>0 AND yes_ask<=?)
                        OR (last_price IS NOT NULL AND last_price>0 AND last_price<=?))
                   ORDER BY ts ASC LIMIT 1""",
                (po["ticker"], po["placed_ts"], po["commence_time"], po["maker_price"], po["maker_price"]),
            ).fetchone()
            if fill:
                f_fill = _fair_prob(self.conn, po["odds_event_id"], self._outcome_for(po["ticker"]), _parse(fill["ts"]))
                self.conn.execute(
                    "UPDATE paper_orders SET status='FILLED', filled_ts=?, fair_at_fill=? WHERE id=?",
                    (fill["ts"], f_fill, po["id"]),
                )
                log.info("Paper FILL   %-28s @ %.2f (fair_place=%.3f fair_fill=%s)",
                         po["ticker"], po["maker_price"], po["fair_at_place"],
                         f"{f_fill:.3f}" if f_fill else "n/a")
            elif now >= ct:
                self.conn.execute("UPDATE paper_orders SET status='UNFILLED' WHERE id=?", (po["id"],))

        needs_clv = self.conn.execute(
            "SELECT id, ticker, commence_time FROM paper_orders WHERE closing_mid IS NULL AND commence_time<=?",
            (ts,),
        ).fetchall()
        for po in needs_clv:
            snap = self.conn.execute(
                """SELECT yes_bid, yes_ask FROM kalshi_snapshots
                   WHERE ticker=? AND ts<=? AND yes_ask>0 AND yes_ask<1
                   ORDER BY ts DESC LIMIT 1""",
                (po["ticker"], po["commence_time"]),
            ).fetchone()
            if snap:
                mid = ((snap["yes_bid"] or snap["yes_ask"]) + snap["yes_ask"]) / 2.0
                self.conn.execute("UPDATE paper_orders SET closing_mid=? WHERE id=?", (mid, po["id"]))

        # 2. Scan pre-game matched markets (within 6h of tip-off) for new resting Maker orders
        paper_events = {r[0] for r in self.conn.execute("SELECT event_ticker FROM paper_orders").fetchall()}
        live_events = {r[0] for r in self.conn.execute("SELECT event_ticker FROM live_orders").fetchall()}
        # An event is "done" once paper has it AND (live is off OR live has it); rejected live attempts get retried.
        existing_events = paper_events & live_events if self.live_maker is not None else paper_events
        horizon = (now + timedelta(hours=WINDOW_BEFORE_H)).strftime("%Y-%m-%dT%H:%M:%SZ")
        min_ct = (now + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        candidates = self.conn.execute(
            "SELECT * FROM market_map WHERE commence_time>? AND commence_time<=?",
            (min_ct, horizon),
        ).fetchall()

        best_by_event: dict[str, tuple[float, dict, float, float, float]] = {}
        for m in candidates:
            if m["event_ticker"] in existing_events:
                continue
            snap = self.conn.execute(
                "SELECT ts, yes_bid, yes_ask FROM kalshi_snapshots WHERE ticker=? ORDER BY ts DESC LIMIT 1",
                (m["ticker"],),
            ).fetchone()
            if not snap or not snap["yes_ask"] or snap["yes_ask"] <= 0 or snap["yes_ask"] >= 1:
                continue
            bid = snap["yes_bid"] or max(0.01, snap["yes_ask"] - 0.02)
            ask = snap["yes_ask"]
            m_prc = round(bid + 0.01, 4) if ask - bid >= 0.02 else round(max(0.01, ask - 0.01), 4)
            if not (0.20 <= m_prc <= 0.90):
                continue
            fair = _fair_prob(self.conn, m["odds_event_id"], m["odds_outcome"], _parse(snap["ts"]))
            if fair is None:
                continue
            edge = fair - m_prc
            if edge >= PAPER_MIN_EDGE:
                prev = best_by_event.get(m["event_ticker"])
                if not prev or edge > prev[0]:
                    best_by_event[m["event_ticker"]] = (edge, dict(m), m_prc, ask, fair)

        for ev_ticker, (edge, m, m_prc, ask, fair) in best_by_event.items():
            if ev_ticker not in paper_events:
                self.conn.execute(
                    """INSERT OR IGNORE INTO paper_orders
                       (placed_ts, ticker, event_ticker, series, odds_event_id, commence_time,
                        maker_price, ask_at_place, fair_at_place, edge_at_place, status)
                       VALUES (?,?,?,?,?,?,?,?,?,?,'RESTING')""",
                    (ts, m["ticker"], ev_ticker, m["series"], m["odds_event_id"], m["commence_time"],
                     m_prc, ask, fair, edge),
                )
                log.info("Paper PLACE  %-28s RESTING bid=%.2f (ask=%.2f fair=%.3f edge=+%.3f)",
                         m["ticker"], m_prc, ask, fair, edge)
            if self.live_maker is not None and ev_ticker not in live_events:
                ct_dt = _parse(m["commence_time"])
                if ct_dt is not None:
                    try:
                        await self.live_maker.place_resting_buy(
                            client, self.conn, m["ticker"], ev_ticker, m["series"],
                            m["commence_time"], int(ct_dt.timestamp()), m_prc, ask, fair, ts
                        )
                    except Exception:
                        log.exception("LiveMaker failed on %s", m["ticker"])
        self.conn.commit()

    def _outcome_for(self, ticker: str) -> str:
        r = self.conn.execute("SELECT odds_outcome FROM market_map WHERE ticker=?", (ticker,)).fetchone()
        return r[0] if r else ""

    async def settle_cycle(self, client) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = self.conn.execute(
            """SELECT DISTINCT k.ticker FROM kalshi_snapshots k
               LEFT JOIN results r ON r.ticker = k.ticker
               WHERE k.expected_expiration < ? AND (r.result IS NULL OR r.result = '')""",
            (cutoff,),
        ).fetchall()
        done = 0
        for row in rows:
            try:
                m = await sources.kalshi_market(client, row["ticker"])
            except sources.SourceError as e:
                log.error("Settle %s failed: %s", row["ticker"], e)
                continue
            res = (m.get("result") or "").lower()
            store.upsert_result(self.conn, row["ticker"], res, m.get("status"), now_iso())
            done += res in ("yes", "no")
            await asyncio.sleep(0.12)
        self.conn.commit()
        if rows:
            log.info("Settle checked %d markets, %d resolved", len(rows), done)

    async def tick(self, client, force: bool = False) -> None:
        if force or self.due("events", EVENTS_EVERY_S):
            await self.events_cycle(client)
        if force or self.due("kalshi", self.kalshi_every_s):
            await self.kalshi_cycle(client)
            self.match_all()
        if force or self.due("odds", ODDS_EVERY_S):
            await self.odds_cycle(client)
        await self.paper_cycle(client)
        if force or self.due("settle", SETTLE_EVERY_S):
            await self.settle_cycle(client)

    def match_all(self) -> None:
        for series, sport in LEAGUES.items():
            if self.events.get(sport) and self.open_markets.get(series):
                self.match_series(series, sport)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--db", default="data/recorder.sqlite")
    ap.add_argument("--interval-min", type=int, default=5, help="Kalshi polling interval in minutes")
    ap.add_argument("--live-maker", action="store_true", help="Enable $50 hard-fenced Micro-Live Maker orders")
    args = ap.parse_args()
    load_dotenv(override=True)
    if args.live_maker:
        os.environ["LIVE_MAKER_ENABLED"] = "true"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    api_key = os.environ.get("THE_ODDS_API_KEY") or os.environ.get("ODDS_API_KEY")
    live_maker = LiveMaker.from_env()
    rec = Recorder(store.connect(args.db), api_key, kalshi_every_s=max(60, args.interval_min * 60),
                   live_maker=live_maker)
    async with httpx.AsyncClient(timeout=20.0) as client:
        if args.once:
            await rec.tick(client, force=True)
            return
        mode = f"LIVE MAKER ($50 cap, floor=${live_maker.cash_floor:.2f})" if live_maker else "PAPER ONLY"
        log.info("Recorder started (%s). Leagues: %s", mode, ", ".join(LEAGUES))
        while True:
            try:
                await rec.tick(client)
            except Exception:  # keep recording; never crash the loop silently
                log.exception("Recorder tick failed")
            await asyncio.sleep(15)


if __name__ == "__main__":
    asyncio.run(main())
