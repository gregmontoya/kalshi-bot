"""Print the edge report from data/recorder.sqlite or data/history.sqlite.

Usage: PYTHONPATH=. .venv/bin/python scripts/edge_report.py --db data/history.sqlite
"""

from __future__ import annotations

import argparse
import math

from src.recorder import store
from src.recorder.analysis import (
    THRESHOLDS,
    Observation,
    best_per_game,
    brier,
    build_report,
    format_report,
    load_observations,
    summarize,
)


def _maker_price(o: Observation) -> float:
    """Resting maker bid 1c inside the spread (or ask - 1c if bid missing), bounded to [0.01, ask]."""
    if o.bid is not None and o.bid > 0 and o.ask - o.bid >= 0.02:
        return round(o.bid + 0.01, 4)
    return round(max(0.01, o.ask - 0.01), 4)


def _summarize_maker(sig: list[Observation]) -> dict:
    n = len(sig)
    if n == 0:
        return {"n": 0}
    prices = [_maker_price(o) for o in sig]
    pnls = [(1.0 if o.won else 0.0) - p for o, p in zip(sig, prices)]
    mean = sum(pnls) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in pnls) / (n - 1)) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n > 1 else float("inf")
    return {
        "n": n,
        "win_rate": sum(o.won for o in sig) / n,
        "avg_price": sum(prices) / n,
        "avg_fair": sum(o.fair for o in sig) / n,
        "mean_pnl": mean,
        "t_stat": (mean / se) if se not in (0.0, float("inf")) else 0.0,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/recorder.sqlite")
    args = ap.parse_args()
    conn = store.connect(args.db)
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("kalshi_snapshots", "odds_snapshots", "market_map", "results")}
    settled = conn.execute("SELECT COUNT(*) FROM results WHERE result IN ('yes','no')").fetchone()[0]
    print(f"Data so far: {counts} | settled markets: {settled}")
    obs = load_observations(conn)
    print(format_report(build_report(obs)))

    snap_obs = [o for o in obs if o.entry == "snap"]
    if not snap_obs:
        return

    print("\n" + "=" * 78)
    print("MAKER-FIRST ANALYSIS (SNAP entry, resting bid+1c, $0 fee, 1 signal/game)")
    print("Selection by Maker Edge = (de-vigged Pinnacle fair - maker_price)")
    print("=" * 78)
    best_maker: dict[str, Observation] = {}
    for o in snap_obs:
        me = o.fair - _maker_price(o)
        if o.odds_event_id not in best_maker or me > (best_maker[o.odds_event_id].fair - _maker_price(best_maker[o.odds_event_id])):
            best_maker[o.odds_event_id] = o
    maker_games = list(best_maker.values())
    print(f"  {'m_edge>=':>8} {'n':>5} {'win%':>6} {'m_prc':>6} {'ask':>6} {'fair':>6} {'Maker P&L':>10} {'Maker t':>8} {'Taker P&L':>10} {'Taker t':>8}")
    for thr in (0.00, 0.01, 0.02, 0.03, 0.05):
        sub = [o for o in maker_games if (o.fair - _maker_price(o)) >= thr]
        sm = _summarize_maker(sub)
        st = summarize(sub)
        if not sm.get("n"):
            print(f"  {thr:>8.2f} {0:>5}")
            continue
        print(f"  {thr:>8.2f} {sm['n']:>5} {100*sm['win_rate']:>5.1f}% {sm['avg_price']:>6.3f} {st['avg_ask']:>6.3f} "
              f"{sm['avg_fair']:>6.3f} {sm['mean_pnl']:>+10.4f} {sm['t_stat']:>8.2f} "
              f"{st['mean_pnl']:>+10.4f} {st['t_stat']:>8.2f}")

    series_by_ticker = dict(conn.execute("SELECT ticker, series FROM market_map").fetchall())
    print("\n" + "=" * 78)
    print("BREAKDOWN BY LEAGUE (SNAP entry, 1 signal/game)")
    print("=" * 78)
    by_series: dict[str, list[Observation]] = {}
    for o in snap_obs:
        by_series.setdefault(series_by_ticker.get(o.ticker, "UNKNOWN"), []).append(o)

    for series, s_obs in sorted(by_series.items()):
        games = best_per_game(s_obs)
        mids = [(((o.bid or o.ask) + o.ask) / 2.0, o.won) for o in s_obs]
        bk = brier(mids)
        bp = brier([(o.fair, o.won) for o in s_obs])
        print(f"\n[{series}] markets={len(s_obs)} games={len(games)}  "
              f"Brier Kalshi={bk:.4f}  Brier Pinnacle={bp:.4f}")
        print(f"  {'edge>=':>7} {'n':>5} {'win%':>6} {'ask':>6} {'fair':>6} {'Taker P&L':>10} {'Taker t':>8} {'Maker P&L':>10} {'Maker t':>8}")
        for thr in THRESHOLDS:
            sub = [o for o in games if o.edge >= thr]
            st = summarize(sub)
            sm = _summarize_maker(sub)
            if not st.get("n"):
                print(f"  {thr:>7.2f} {0:>5}")
                continue
            print(f"  {thr:>7.2f} {st['n']:>5} {100*st['win_rate']:>5.1f}% {st['avg_ask']:>6.3f} "
                  f"{st['avg_fair']:>6.3f} {st['mean_pnl']:>+10.4f} {st['t_stat']:>8.2f} "
                  f"{sm['mean_pnl']:>+10.4f} {sm['t_stat']:>8.2f}")

    print("\n" + "=" * 78)
    print("BREAKDOWN BY KALSHI PRICE BUCKET (SNAP entry, 1 signal/game, edge >= 0.02)")
    print("=" * 78)
    all_games = best_per_game(snap_obs)
    buckets = [
        ("< $0.38 (Longshot)", 0.00, 0.38),
        ("$0.38 - $0.58 (Pod 2 Value)", 0.38, 0.58),
        ("$0.58 - $0.68 (Mod Favorite)", 0.58, 0.68),
        ("$0.68 - $0.78 (Pod 1 Basket)", 0.68, 0.78),
        (">= $0.78 (Heavy Favorite)", 0.78, 1.01),
    ]
    print(f"  {'Bucket':<28} {'n':>5} {'win%':>6} {'ask':>6} {'fair':>6} {'Taker P&L':>10} {'Taker t':>8} {'Maker P&L':>10} {'Maker t':>8}")
    for label, lo, hi in buckets:
        sub = [o for o in all_games if lo <= o.ask < hi and o.edge >= 0.02]
        st = summarize(sub)
        sm = _summarize_maker(sub)
        if not st.get("n"):
            print(f"  {label:<28} {0:>5}")
            continue
        print(f"  {label:<28} {st['n']:>5} {100*st['win_rate']:>5.1f}% {st['avg_ask']:>6.3f} "
              f"{st['avg_fair']:>6.3f} {st['mean_pnl']:>+10.4f} {st['t_stat']:>8.2f} "
              f"{sm['mean_pnl']:>+10.4f} {sm['t_stat']:>8.2f}")


if __name__ == "__main__":
    main()
