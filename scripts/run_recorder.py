"""Recorder daemon: snapshots Kalshi game markets + Pinnacle odds + settlements into data/recorder.sqlite.

Places NO orders and uses NO Kalshi credentials (public market data only).

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
from src.recorder.matching import match_event, outcome_name_for

# Kalshi series -> The Odds API sport key. Only leagues where team-name matching is unambiguous.
LEAGUES = {
    "KXNBAGAME": "basketball_nba",
    "KXNHLGAME": "icehockey_nhl",
    "KXNFLGAME": "americanfootball_nfl",
    "KXEPLGAME": "soccer_epl",
}
KALSHI_EVERY_S = 300          # Kalshi prices: every 5 min (free)
ODDS_EVERY_S = 1800           # Pinnacle odds per active sport: every 30 min (1 credit each)
EVENTS_EVERY_S = 1800         # Free odds event list (drives matching + activity window)
SETTLE_EVERY_S = 1800         # Settlement checks
ODDS_CREDIT_FLOOR = 20        # Stop spending odds credits below this
WINDOW_BEFORE_H = 6.0         # Sport is "active" if a game starts within 6h...
WINDOW_AFTER_H = 3.5          # ...or started less than 3.5h ago
KALSHI_HORIZON_H = 48         # Only snapshot markets expiring within 48h (keeps DB small)

log = logging.getLogger("recorder")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def sport_active(events: list[dict], now: datetime) -> bool:
    for ev in events:
        ct = _parse(ev.get("commence_time"))
        if ct and now - timedelta(hours=WINDOW_AFTER_H) <= ct <= now + timedelta(hours=WINDOW_BEFORE_H):
            return True
    return False


class Recorder:
    def __init__(self, conn, api_key: str | None):
        self.conn = conn
        self.api_key = api_key
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

    async def settle_cycle(self, client) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
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
        if force or self.due("kalshi", KALSHI_EVERY_S):
            await self.kalshi_cycle(client)
        if force or self.due("events", EVENTS_EVERY_S):
            await self.events_cycle(client)
        if force or self.due("odds", ODDS_EVERY_S):
            await self.odds_cycle(client)
        if force or self.due("settle", SETTLE_EVERY_S):
            await self.settle_cycle(client)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--db", default="data/recorder.sqlite")
    args = ap.parse_args()
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    api_key = os.environ.get("THE_ODDS_API_KEY") or os.environ.get("ODDS_API_KEY")
    rec = Recorder(store.connect(args.db), api_key)
    async with httpx.AsyncClient(timeout=20.0) as client:
        if args.once:
            await rec.tick(client, force=True)
            return
        log.info("Recorder started (no trading). Leagues: %s", ", ".join(LEAGUES))
        while True:
            try:
                await rec.tick(client)
            except Exception:  # keep recording; never crash the loop silently
                log.exception("Recorder tick failed")
            await asyncio.sleep(15)


if __name__ == "__main__":
    asyncio.run(main())
