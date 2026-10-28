"""Backfill ~1.5 years of Kalshi game markets + same-moment Pinnacle odds into data/history.sqlite.

Method: one Pinnacle snapshot per league per game-day (10 credits) gives odds for every game that day.
For each game we record the Kalshi bid/ask from 1-minute candles in the 15 minutes right after that
snapshot. The edge test then compares the two prices taken at the same moment, held to settlement.

Phases (resumable; finished work is never re-paid for):
  1. Kalshi settled markets: archive (/historical/markets) + recent (/markets?status=settled)   free
  2. Pinnacle snapshot per league per game-day                                                10 credits
  3. Match Kalshi games to snapshot events                                                     free
  4. Kalshi 1-min candles right after each game's snapshot                                     free

Usage:
  PYTHONPATH=. .venv/bin/python scripts/backfill.py                          # estimate only
  PYTHONPATH=. .venv/bin/python scripts/backfill.py --spend --max-credits 14000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv

from src.recorder import sources, store
from src.recorder.matching import _et_date, match_event, outcome_name_for, ticker_date

UTC = timezone.utc
# series: (odds sport key, snapshot time UTC on the game's ET date). Ordered by priority for the credit budget.
LEAGUES: dict[str, tuple[str, str]] = {
    "KXNBAGAME": ("basketball_nba", "22:30"),
    "KXNHLGAME": ("icehockey_nhl", "22:30"),
    "KXNFLGAME": ("americanfootball_nfl", "16:30"),
    "KXEPLGAME": ("soccer_epl", "10:30"),
    "KXMLBGAME": ("baseball_mlb", "22:30"),
    "KXLALIGAGAME": ("soccer_spain_la_liga", "10:30"),
    "KXSERIEAGAME": ("soccer_italy_serie_a", "10:30"),
    "KXBUNDESLIGAGAME": ("soccer_germany_bundesliga", "10:30"),
    "KXUCLGAME": ("soccer_uefa_champs_league", "16:15"),
    "KXWNBAGAME": ("basketball_wnba", "22:30"),
    "KXMLSGAME": ("soccer_usa_mls", "23:00"),
}
COST_PER_SNAPSHOT = 10
CANDLE_WINDOW_MIN = 15

EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS bf_events (sport TEXT, odds_event_id TEXT PRIMARY KEY, home_team TEXT,
                                      away_team TEXT, commence_time TEXT);
CREATE TABLE IF NOT EXISTS bf_done (kind TEXT, sport TEXT, key TEXT, PRIMARY KEY (kind, sport, key));
CREATE TABLE IF NOT EXISTS bf_kalshi_markets (series TEXT, ticker TEXT PRIMARY KEY, data TEXT);
"""
log = logging.getLogger("backfill")


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s: str) -> datetime:
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def done(conn, kind, sport, key) -> bool:
    return conn.execute("SELECT 1 FROM bf_done WHERE kind=? AND sport=? AND key=?",
                        (kind, sport, key)).fetchone() is not None


def mark(conn, kind, sport, key) -> None:
    conn.execute("INSERT OR IGNORE INTO bf_done VALUES (?,?,?)", (kind, sport, key))
    conn.commit()


def snapshot_time(d: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h, m, tzinfo=UTC)


def _close(block: dict | None):
    if not block:
        return None
    return block.get("close_dollars", block.get("close"))


async def all_settled(client, series: str, conn=None) -> list[dict]:
    if conn is not None:
        cached = conn.execute("SELECT data FROM bf_kalshi_markets WHERE series=?", (series,)).fetchall()
        if cached:
            return [json.loads(r[0]) for r in cached]
    archived = []
    cursor = None
    while True:
        params = {"series_ticker": series, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        data = (await sources._get(client, f"{sources.KALSHI_BASE}/historical/markets", params)).json()
        archived.extend(m | {"_archived": True} for m in data.get("markets", []))
        cursor = data.get("cursor")
        if not cursor or not data.get("markets"):
            break
    recent = [m | {"_archived": False} for m in await sources.kalshi_settled_markets(client, series)]
    seen, out = set(), []
    for m in recent + archived:
        if m["ticker"] not in seen:
            seen.add(m["ticker"])
            out.append(m)
    if conn is not None and out:
        conn.executemany("INSERT OR REPLACE INTO bf_kalshi_markets VALUES (?,?,?)",
                         [(series, m["ticker"], json.dumps(m)) for m in out])
        conn.commit()
    return out


async def candles(client, series: str, m: dict, start: int, end: int) -> list[dict]:
    if m["_archived"]:
        url = f"{sources.KALSHI_BASE}/historical/markets/{m['ticker']}/candlesticks"
    else:
        url = f"{sources.KALSHI_BASE}/series/{series}/markets/{m['ticker']}/candlesticks"
    r = await sources._get(client, url, {"start_ts": start, "end_ts": end, "period_interval": 1})
    return r.json().get("candlesticks", [])


def _candle_rows(m: dict, series: str, cs: list[dict]) -> list[tuple]:
    return [
        (iso(datetime.fromtimestamp(cd["end_period_ts"], UTC)), m["ticker"], m["event_ticker"],
         series, m.get("yes_sub_title"), _f(_close(cd.get("yes_bid"))),
         _f(_close(cd.get("yes_ask"))), _f(_close(cd.get("price"))),
         _f(cd.get("volume_fp", cd.get("volume"))), None, None, "candle")
        for cd in cs
    ]


def _complement_rows(m_other: dict, series: str, base_rows: list[tuple]) -> list[tuple]:
    """LAW 1 reciprocal orderbook complement for a 2-way game's opposing market."""
    out = []
    for ts, _, ev_tk, _, _, bid, ask, price, vol, oi, exp, st in base_rows:
        c_bid = round(1.0 - ask, 4) if ask is not None else None
        c_ask = round(1.0 - bid, 4) if bid is not None else None
        c_prc = round(1.0 - price, 4) if price is not None else None
        out.append((ts, m_other["ticker"], ev_tk, series, m_other.get("yes_sub_title"),
                    c_bid, c_ask, c_prc, vol, oi, exp, st))
    return out


async def run(args) -> None:
    load_dotenv(override=True)
    key = os.environ["THE_ODDS_API_KEY"]
    conn = store.connect(args.db)
    conn.executescript(EXTRA_SCHEMA)
    now = datetime.now(UTC)
    leagues = {s: v for s, v in LEAGUES.items() if not args.leagues or s in args.leagues.split(",")}

    limits = httpx.Limits(max_connections=20, max_keepalive_connections=20)
    async with httpx.AsyncClient(timeout=30.0, limits=limits) as client:
        markets: dict[str, list[dict]] = {}
        plan: list[tuple[str, str, date]] = []
        done_snaps = {(r[0], r[1]) for r in conn.execute("SELECT sport, key FROM bf_done WHERE kind='snap'").fetchall()}
        for series, (sport, hhmm) in leagues.items():
            markets[series] = await all_settled(client, series, conn)
            days = sorted({d for m in markets[series] if (d := ticker_date(m.get("event_ticker", "")))})
            pending = [d for d in days if (sport, d.isoformat()) not in done_snaps
                       and snapshot_time(d, hhmm) < now - timedelta(hours=6)]
            plan += [(series, sport, d) for d in pending]
            games = len({m["event_ticker"] for m in markets[series]})
            log.info("Plan %-17s %5d games over %4d days, %4d snapshots pending -> %5d credits",
                     series, games, len(days), len(pending), len(pending) * COST_PER_SNAPSHOT)
        log.info("Plan TOTAL %d credits (cap %d)", len(plan) * COST_PER_SNAPSHOT, args.max_credits)
        if not args.spend:
            log.info("Estimate only. Re-run with --spend.")
            return

        # Phase 2: snapshots
        spent = 0
        for i, (series, sport, d) in enumerate(plan):
            if spent + COST_PER_SNAPSHOT > args.max_credits:
                log.warning("Credit cap reached after %d credits; remaining days skipped", spent)
                break
            hhmm = leagues[series][1]
            try:
                snap, usage = await sources.historical_pinnacle(client, key, sport, iso(snapshot_time(d, hhmm)))
            except sources.SourceError as e:
                log.error("snapshot %s %s: %s", sport, d, e)
                continue
            spent += max(usage["last"], 0)
            ts = snap.get("timestamp")
            data = snap.get("data", [])
            if ts:
                store.insert_odds(conn, ts, sport, data)
                conn.executemany("INSERT OR REPLACE INTO bf_events VALUES (?,?,?,?,?)",
                                 [(sport, e["id"], e["home_team"], e["away_team"], e["commence_time"]) for e in data])
            mark(conn, "snap", sport, d.isoformat())
            store.log_usage(conn, iso(datetime.now(UTC)), f"hist_odds:{sport}", usage["remaining"], usage["used"],
                            usage["last"])
            if i % 50 == 0:
                log.info("Snapshots %d/%d, spent %d, credits left %s", i + 1, len(plan), spent, usage["remaining"])
        log.info("Phase2 spent %d credits", spent)

        # Phase 3: fast date-indexed match + Phase 4: candles
        done_candles = {(r[0], r[1]) for r in conn.execute("SELECT sport, key FROM bf_done WHERE kind='candles'").fetchall()}
        pregame_snap = dict(conn.execute(
            "SELECT o.odds_event_id, MAX(o.ts) FROM odds_snapshots o "
            "JOIN bf_events e ON e.odds_event_id = o.odds_event_id "
            "WHERE o.ts < e.commence_time GROUP BY o.odds_event_id"
        ).fetchall())

        recent_groups: dict[tuple[str, str, int, int], list[dict]] = defaultdict(list)
        per_series_jobs: dict[str, list[tuple[str, str, dict, dict | None, int, int]]] = defaultdict(list)
        now_iso = iso(now)

        for series, (sport, _) in leagues.items():
            evs = [dict(r) | {"id": r["odds_event_id"]}
                   for r in conn.execute("SELECT * FROM bf_events WHERE sport=?", (sport,)).fetchall()]
            by_date: dict[int, list[dict]] = defaultdict(list)
            for e in evs:
                d_ord = _et_date(e["commence_time"]).toordinal()
                for off in (-1, 0, 1):
                    by_date[d_ord + off].append(e)

            by_event: dict[str, list[dict]] = defaultdict(list)
            for m in markets[series]:
                by_event[m["event_ticker"]].append(m)
            ok = 0
            for ev_ticker, mkts in by_event.items():
                td = ticker_date(ev_ticker)
                if td is None:
                    continue
                ev = match_event(ev_ticker, [m.get("yes_sub_title", "") for m in mkts], by_date.get(td.toordinal(), ()))
                if not ev:
                    continue
                snap_str = pregame_snap.get(ev["id"])
                if not snap_str:
                    continue
                commence = parse(ev["commence_time"])
                snap_ts = parse(snap_str)
                ok += 1
                start_s = int(snap_ts.timestamp())
                end_s = int(min(snap_ts + timedelta(minutes=CANDLE_WINDOW_MIN), commence).timestamp())
                valid_mkts = []
                for m in mkts:
                    oc = outcome_name_for(m.get("yes_sub_title", ""), ev)
                    res = (m.get("result") or "").lower()
                    if not oc or res not in ("yes", "no"):
                        continue
                    store.upsert_map(conn, m["ticker"], ev_ticker, series, sport, ev["id"], oc,
                                     ev["commence_time"], now_iso)
                    store.upsert_result(conn, m["ticker"], res, m.get("status"), now_iso)
                    valid_mkts.append(m)

                # Schedule candle fetches (for 2-way archived games, 1 HTTP fetch covers both sides via LAW 1)
                if len(valid_mkts) == 2 and all(m.get("_archived") for m in valid_mkts):
                    m0, m1 = valid_mkts
                    d0, d1 = (sport, m0["ticker"]) in done_candles, (sport, m1["ticker"]) in done_candles
                    if d0 and d1:
                        continue
                    if d0 and not d1:
                        existing = conn.execute("SELECT * FROM kalshi_snapshots WHERE ticker=?", (m0["ticker"],)).fetchall()
                        c_rows = _complement_rows(m1, series, [tuple(r) for r in existing])
                        conn.executemany("INSERT INTO kalshi_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", c_rows)
                        conn.execute("INSERT OR IGNORE INTO bf_done VALUES (?,?,?)", ("candles", sport, m1["ticker"]))
                        done_candles.add((sport, m1["ticker"]))
                    elif d1 and not d0:
                        existing = conn.execute("SELECT * FROM kalshi_snapshots WHERE ticker=?", (m1["ticker"],)).fetchall()
                        c_rows = _complement_rows(m0, series, [tuple(r) for r in existing])
                        conn.executemany("INSERT INTO kalshi_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", c_rows)
                        conn.execute("INSERT OR IGNORE INTO bf_done VALUES (?,?,?)", ("candles", sport, m0["ticker"]))
                        done_candles.add((sport, m0["ticker"]))
                    else:
                        per_series_jobs[series].append((series, sport, m0, m1, start_s, end_s))
                else:
                    for m in valid_mkts:
                        if (sport, m["ticker"]) not in done_candles:
                            if m.get("_archived"):
                                per_series_jobs[series].append((series, sport, m, None, start_s, end_s))
                            else:
                                recent_groups[(series, sport, start_s, end_s)].append(m)
            conn.commit()
            log.info("Match %-17s %5d / %5d games matched to a pre-game Pinnacle snapshot", series, ok, len(by_event))

        # Interleave archived jobs round-robin across leagues so every league populates immediately
        archived_jobs = []
        max_len = max((len(v) for v in per_series_jobs.values()), default=0)
        for idx in range(max_len):
            for s in leagues:
                if idx < len(per_series_jobs[s]):
                    archived_jobs.append(per_series_jobs[s][idx])

        # Phase 4a: recent markets via batch candlesticks endpoint
        total = 0
        recent_done = 0
        recent_total = sum(len(v) for v in recent_groups.values())
        for (series, sport, start_s, end_s), mkts_grp in recent_groups.items():
            for i in range(0, len(mkts_grp), 50):
                chunk = mkts_grp[i:i + 50]
                by_tk = {m["ticker"]: m for m in chunk}
                try:
                    r = await sources._get(client, f"{sources.KALSHI_BASE}/markets/candlesticks", {
                        "market_tickers": ",".join(by_tk),
                        "start_ts": start_s,
                        "end_ts": end_s,
                        "period_interval": 1,
                    })
                    batch_data = r.json().get("markets", [])
                except sources.SourceError as e:
                    log.error("batch candles %s: %s", series, e)
                    continue
                rows = []
                for item in batch_data:
                    tk = item.get("market_ticker")
                    if tk in by_tk:
                        rows.extend(_candle_rows(by_tk[tk], series, item.get("candlesticks", [])))
                conn.executemany("INSERT INTO kalshi_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                conn.executemany("INSERT OR IGNORE INTO bf_done VALUES (?,?,?)",
                                 [("candles", sport, tk) for tk in by_tk])
                conn.commit()
                total += len(rows)
                recent_done += len(chunk)
        if recent_total:
            log.info("Recent candles %d/%d markets, %d rows", recent_done, recent_total, total)

        # Phase 4b: archived markets with smooth rate pacer (~9.5 HTTP calls/sec = ~19 markets/sec, 0 rate-limit drops)
        pacer_lock = asyncio.Lock()
        next_slot = 0.0

        async def fetch_archived(job) -> tuple[list[tuple], list[tuple[str, str, str]]] | None:
            nonlocal next_slot
            series, sport, m0, m1, start, end = job
            async with pacer_lock:
                now_m = time.monotonic()
                wait = max(0.0, next_slot - now_m)
                next_slot = max(now_m, next_slot) + 0.105
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                cs = await candles(client, series, m0, start, end)
            except sources.SourceError as e:
                log.error("candles %s: %s", m0["ticker"], e)
                return None
            r0 = _candle_rows(m0, series, cs)
            marks = [("candles", sport, m0["ticker"])]
            if m1 is not None:
                r0 = r0 + _complement_rows(m1, series, r0)
                marks.append(("candles", sport, m1["ticker"]))
            return r0, marks

        for i in range(0, len(archived_jobs), 200):
            batch_res = await asyncio.gather(*(fetch_archived(j) for j in archived_jobs[i:i + 200]))
            rows = []
            marks = []
            for res in batch_res:
                if res is not None:
                    r, mk = res
                    rows.extend(r)
                    marks.extend(mk)
            conn.executemany("INSERT INTO kalshi_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            conn.executemany("INSERT OR IGNORE INTO bf_done VALUES (?,?,?)", marks)
            conn.commit()
            total += len(rows)
            log.info("Archived jobs %d/%d (%d total candle rows)",
                     min(i + 200, len(archived_jobs)), len(archived_jobs), total)
        log.info("Backfill complete.")


def _f(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/history.sqlite")
    ap.add_argument("--spend", action="store_true")
    ap.add_argument("--max-credits", type=int, default=14000)
    ap.add_argument("--leagues", default="", help="comma-separated Kalshi series to include (default all)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
