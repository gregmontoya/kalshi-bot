"""SQLite storage (WAL mode so the report can read while the recorder writes)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS kalshi_snapshots (
    ts TEXT NOT NULL, ticker TEXT NOT NULL, event_ticker TEXT, series TEXT, yes_sub_title TEXT,
    yes_bid REAL, yes_ask REAL, last_price REAL, volume REAL, open_interest REAL,
    expected_expiration TEXT, status TEXT
);
CREATE INDEX IF NOT EXISTS ix_ks_ticker_ts ON kalshi_snapshots(ticker, ts);

CREATE TABLE IF NOT EXISTS odds_snapshots (
    ts TEXT NOT NULL, sport TEXT, odds_event_id TEXT NOT NULL, commence_time TEXT,
    home_team TEXT, away_team TEXT, book TEXT, book_last_update TEXT, outcome TEXT, price REAL
);
CREATE INDEX IF NOT EXISTS ix_os_event_ts ON odds_snapshots(odds_event_id, ts);

CREATE TABLE IF NOT EXISTS market_map (
    ticker TEXT PRIMARY KEY, event_ticker TEXT, series TEXT, sport TEXT,
    odds_event_id TEXT, odds_outcome TEXT, commence_time TEXT, matched_at TEXT
);

CREATE TABLE IF NOT EXISTS results (
    ticker TEXT PRIMARY KEY, result TEXT, status TEXT, checked_at TEXT
);

CREATE TABLE IF NOT EXISTS api_usage (
    ts TEXT, source TEXT, credits_remaining INTEGER, credits_used INTEGER, credits_last INTEGER
);
"""


def connect(path: str | Path = "data/recorder.sqlite") -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


def _f(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def insert_kalshi_markets(conn, ts: str, series: str, markets: list[dict]) -> int:
    rows = [
        (ts, m.get("ticker"), m.get("event_ticker"), series, m.get("yes_sub_title"),
         _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars")), _f(m.get("last_price_dollars")),
         _f(m.get("volume_fp")), _f(m.get("open_interest_fp")),
         m.get("expected_expiration_time"), m.get("status"))
        for m in markets if m.get("ticker")
    ]
    conn.executemany("INSERT INTO kalshi_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return len(rows)


def insert_odds(conn, ts: str, sport: str, events: list[dict]) -> int:
    rows = []
    for ev in events:
        for bm in ev.get("bookmakers", []):
            for mk in bm.get("markets", []):
                if mk.get("key") != "h2h":
                    continue
                for oc in mk.get("outcomes", []):
                    rows.append((ts, sport, ev["id"], ev.get("commence_time"), ev.get("home_team"),
                                 ev.get("away_team"), bm.get("key"), bm.get("last_update"),
                                 oc.get("name"), _f(oc.get("price"))))
    conn.executemany("INSERT INTO odds_snapshots VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return len(rows)


def upsert_map(conn, ticker, event_ticker, series, sport, odds_event_id, outcome, commence, ts) -> None:
    conn.execute(
        "INSERT INTO market_map VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET "
        "odds_event_id=excluded.odds_event_id, odds_outcome=excluded.odds_outcome, "
        "commence_time=excluded.commence_time, matched_at=excluded.matched_at",
        (ticker, event_ticker, series, sport, odds_event_id, outcome, commence, ts),
    )


def upsert_result(conn, ticker, result, status, ts) -> None:
    conn.execute(
        "INSERT INTO results VALUES (?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET "
        "result=excluded.result, status=excluded.status, checked_at=excluded.checked_at",
        (ticker, result, status, ts),
    )


def log_usage(conn, ts, source, remaining, used, last) -> None:
    conn.execute("INSERT INTO api_usage VALUES (?,?,?,?,?)", (ts, source, remaining, used, last))
    conn.commit()
