"""In-game paper tester: strict price rules like "buy at 90c, stop out at 85c, else hold to settlement".

PAPER ONLY. Fed by the WebSocket order books in ws_paper.WsPaperBook. Assumes realistic in-game execution:
entries buy at the current YES ask and stops sell at the current YES bid (both taker, fee charged), 10 contracts.
Settlement P&L is computed later from the `results` table.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

log = logging.getLogger("ingame_paper")

CONTRACTS = 10
# (name, entry_price, stop_price)
VARIANTS = [
    ("buy90_stop85", 0.90, 0.85),
    ("buy88_stop83", 0.88, 0.83),
    ("buy92_stop87", 0.92, 0.87),
    ("buy90_hold", 0.90, None),       # control: same entry, never stop
]
ENTRY_BAND = 0.02                     # enter when ask is within [entry - 2c, entry]

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingame_paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    variant TEXT NOT NULL,
    ticker TEXT NOT NULL,
    event_ticker TEXT NOT NULL,
    series TEXT,
    entry_ts TEXT NOT NULL,
    entry_price REAL NOT NULL,
    entry_fee REAL NOT NULL,
    exit_ts TEXT,
    exit_price REAL,
    exit_fee REAL,
    status TEXT NOT NULL,             -- OPEN | STOPPED
    UNIQUE(variant, event_ticker)
);
"""


def taker_fee(price: float, contracts: int = CONTRACTS) -> float:
    """Kalshi taker fee in dollars: round_up(0.07 * C * P * (1-P)) to the cent."""
    return math.ceil(0.07 * contracts * price * (1 - price) * 100 - 1e-9) / 100.0


class InGamePaper:
    def __init__(self, conn) -> None:
        self.conn = conn
        conn.executescript(SCHEMA)
        self.entered = {(r[0], r[1]) for r in conn.execute("SELECT variant, event_ticker FROM ingame_paper_trades")}
        self.open = {(r["variant"], r["ticker"]): r["id"] for r in
                     conn.execute("SELECT id, variant, ticker FROM ingame_paper_trades WHERE status='OPEN'")}
        self.entries = 0
        self.stops = 0

    def on_book(self, tk: str, meta: dict, bid: float | None, ask: float | None) -> None:
        ct = meta.get("commence_time")
        now = datetime.now(timezone.utc)
        if not ct or datetime.fromisoformat(ct.replace("Z", "+00:00")) > now:
            return  # live (in-game) only
        ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        ev = meta["event_ticker"]
        for name, entry, stop in VARIANTS:
            key = (name, tk)
            if key in self.open:
                if stop is not None and bid is not None and bid <= stop + 1e-9:
                    self.conn.execute(
                        "UPDATE ingame_paper_trades SET status='STOPPED', exit_ts=?, exit_price=?, exit_fee=? WHERE id=?",
                        (ts, bid, taker_fee(bid), self.open.pop(key)))
                    self.conn.commit()
                    self.stops += 1
                    log.info("INGAME STOP  %-13s %-30s sold @ %.2f", name, tk, bid)
                continue
            if (name, ev) in self.entered or ask is None:
                continue
            if entry - ENTRY_BAND - 1e-9 <= ask <= entry + 1e-9:
                cur = self.conn.execute(
                    """INSERT OR IGNORE INTO ingame_paper_trades
                       (variant, ticker, event_ticker, series, entry_ts, entry_price, entry_fee, status)
                       VALUES (?,?,?,?,?,?,?,'OPEN')""",
                    (name, tk, ev, meta.get("series"), ts, ask, taker_fee(ask)))
                self.conn.commit()
                self.entered.add((name, ev))
                if cur.rowcount:
                    self.open[key] = cur.lastrowid
                    self.entries += 1
                    log.info("INGAME ENTER %-13s %-30s bought @ %.2f", name, tk, ask)


def report(conn) -> str:
    """P&L per variant using settled results (YES pays $1). Open trades without results are excluded."""
    lines = [f"{'variant':14} {'n':>4} {'stopped':>7} {'won':>4} {'lost':>4} {'P&L $':>8} {'per trade':>9}"]
    for name, _, _ in VARIANTS:
        rows = conn.execute(
            """SELECT t.*, r.result FROM ingame_paper_trades t LEFT JOIN results r ON r.ticker=t.ticker
               WHERE t.variant=?""", (name,)).fetchall()
        n = stopped = won = lost = 0
        pnl = 0.0
        for t in rows:
            cost = t["entry_price"] * CONTRACTS + t["entry_fee"]
            if t["status"] == "STOPPED":
                pnl += t["exit_price"] * CONTRACTS - t["exit_fee"] - cost
                stopped += 1
            elif t["result"] in ("yes", "no"):
                pnl += (CONTRACTS if t["result"] == "yes" else 0) - cost
                won += t["result"] == "yes"
                lost += t["result"] == "no"
            else:
                continue
            n += 1
        lines.append(f"{name:14} {n:4d} {stopped:7d} {won:4d} {lost:4d} {pnl:8.2f} {pnl / n if n else 0:9.3f}")
    return "\n".join(lines)
