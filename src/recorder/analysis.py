"""Edge test: does (de-vigged Pinnacle probability - Kalshi ask - fee) predict settlement profit?

Pass criteria (stated BEFORE seeing data, so they can't be moved afterwards):
  - at least MIN_SIGNALS independent signals (one per game) at the chosen threshold
  - mean P&L per contract after taker fees > 0
  - t-statistic > 2.0
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from src.recorder.devig import devig_power

MIN_SIGNALS = 200
T_STAT_REQUIRED = 2.0
THRESHOLDS = (0.00, 0.02, 0.03, 0.05)
ODDS_MAX_AGE = timedelta(minutes=45)


def taker_fee_per_contract(price: float, contracts: int = 20) -> float:
    """Kalshi taker fee: ceil(0.07 * C * P * (1-P)) cents per order, expressed per contract in dollars."""
    cents = math.ceil(round(7.0 * contracts * price * (1.0 - price), 6))
    return cents / 100.0 / contracts


@dataclass
class Observation:
    ticker: str
    odds_event_id: str
    entry: str            # "close" or "early"
    ask: float
    bid: float | None
    fair: float
    won: bool

    @property
    def fee(self) -> float:
        return taker_fee_per_contract(self.ask)

    @property
    def edge(self) -> float:
        return self.fair - self.ask - self.fee

    @property
    def pnl(self) -> float:
        return (1.0 if self.won else 0.0) - self.ask - self.fee


def _dt(s: str) -> datetime:
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _fair_prob(conn, odds_event_id: str, outcome: str, at: datetime) -> float | None:
    rows = conn.execute(
        "SELECT ts, outcome, price FROM odds_snapshots WHERE odds_event_id=? AND book='pinnacle' AND ts<=? "
        "ORDER BY ts DESC LIMIT 12",
        (odds_event_id, at.isoformat()),
    ).fetchall()
    if not rows:
        return None
    latest_ts = rows[0]["ts"]
    if at - _dt(latest_ts) > ODDS_MAX_AGE:
        return None
    snap = [(r["outcome"], r["price"]) for r in rows if r["ts"] == latest_ts and r["price"]]
    names = [n for n, _ in snap]
    if outcome not in names or len(snap) < 2:
        return None
    probs = devig_power([p for _, p in snap])
    return probs[names.index(outcome)]


def load_observations(conn) -> list[Observation]:
    markets = conn.execute(
        """SELECT m.ticker, m.odds_event_id, m.odds_outcome, m.commence_time, r.result
           FROM market_map m JOIN results r ON r.ticker = m.ticker
           WHERE r.result IN ('yes','no')"""
    ).fetchall()
    out: list[Observation] = []
    windows = {"close": (timedelta(minutes=30), timedelta(0)),
               "early": (timedelta(hours=3), timedelta(hours=1))}
    for m in markets:
        commence = _dt(m["commence_time"])
        for entry, (lo, hi) in windows.items():
            snap = conn.execute(
                "SELECT ts, yes_bid, yes_ask FROM kalshi_snapshots WHERE ticker=? AND ts>=? AND ts<=? "
                "ORDER BY ts DESC LIMIT 1",
                (m["ticker"], (commence - lo).isoformat(), (commence - hi).isoformat()),
            ).fetchone()
            if not snap or not snap["yes_ask"] or not (0.0 < snap["yes_ask"] < 1.0):
                continue
            fair = _fair_prob(conn, m["odds_event_id"], m["odds_outcome"], _dt(snap["ts"]))
            if fair is None:
                continue
            out.append(Observation(m["ticker"], m["odds_event_id"], entry, snap["yes_ask"], snap["yes_bid"],
                                   fair, m["result"] == "yes"))
    return out


def best_per_game(obs: list[Observation]) -> list[Observation]:
    """One signal per game (the side with the largest edge) so correlated sides aren't double-counted."""
    best: dict[str, Observation] = {}
    for o in obs:
        if o.odds_event_id not in best or o.edge > best[o.odds_event_id].edge:
            best[o.odds_event_id] = o
    return list(best.values())


def summarize(sig: list[Observation]) -> dict:
    n = len(sig)
    if n == 0:
        return {"n": 0}
    pnls = [o.pnl for o in sig]
    mean = sum(pnls) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in pnls) / (n - 1)) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n > 1 else float("inf")
    return {
        "n": n,
        "win_rate": sum(o.won for o in sig) / n,
        "avg_ask": sum(o.ask for o in sig) / n,
        "avg_fair": sum(o.fair for o in sig) / n,
        "mean_pnl": mean,
        "total_pnl_per_contract": sum(pnls),
        "t_stat": (mean / se) if se not in (0.0, float("inf")) else 0.0,
    }


def brier(pairs: list[tuple[float, bool]]) -> float | None:
    return sum((p - (1.0 if y else 0.0)) ** 2 for p, y in pairs) / len(pairs) if pairs else None


def build_report(obs: list[Observation]) -> dict:
    report: dict = {"entries": {}}
    for entry in ("close", "early"):
        e_obs = [o for o in obs if o.entry == entry]
        games = best_per_game(e_obs)
        mids = [(((o.bid or o.ask) + o.ask) / 2.0, o.won) for o in e_obs]
        report["entries"][entry] = {
            "markets": len(e_obs),
            "games": len(games),
            "brier_kalshi_mid": brier(mids),
            "brier_pinnacle": brier([(o.fair, o.won) for o in e_obs]),
            "thresholds": {thr: summarize([o for o in games if o.edge >= thr]) for thr in THRESHOLDS},
        }
    close = report["entries"]["close"]["thresholds"]
    passing = [thr for thr, s in close.items()
               if s.get("n", 0) >= MIN_SIGNALS and s["mean_pnl"] > 0 and s["t_stat"] > T_STAT_REQUIRED]
    report["verdict"] = "PASS" if passing else "NOT PROVEN"
    report["passing_thresholds"] = passing
    return report


def format_report(rep: dict) -> str:
    lines = ["=" * 78, "EDGE REPORT: Pinnacle (de-vigged) vs Kalshi ask, held to settlement, after taker fees",
             f"Pass rule: >= {MIN_SIGNALS} games, mean P&L > 0, t > {T_STAT_REQUIRED} (close entry)", "=" * 78]
    for entry, e in rep["entries"].items():
        lines.append(f"\n[{entry.upper()} entry] markets={e['markets']} games={e['games']}  "
                     f"Brier Kalshi={_fmt(e['brier_kalshi_mid'])}  Brier Pinnacle={_fmt(e['brier_pinnacle'])}")
        lines.append(f"  {'edge>=':>7} {'n':>5} {'win%':>6} {'ask':>6} {'fair':>6} {'P&L/ct':>8} {'t':>6}")
        for thr, s in e["thresholds"].items():
            if not s.get("n"):
                lines.append(f"  {thr:>7.2f} {0:>5}")
                continue
            lines.append(f"  {thr:>7.2f} {s['n']:>5} {100*s['win_rate']:>5.1f}% {s['avg_ask']:>6.3f} "
                         f"{s['avg_fair']:>6.3f} {s['mean_pnl']:>+8.4f} {s['t_stat']:>6.2f}")
    lines.append(f"\nVERDICT: {rep['verdict']}" + (f" at thresholds {rep['passing_thresholds']}"
                                                    if rep["passing_thresholds"] else
                                                    "  -> do NOT trade real money on this rule yet."))
    return "\n".join(lines)


def _fmt(x) -> str:
    return "n/a" if x is None else f"{x:.4f}"
