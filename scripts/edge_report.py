"""Print the edge report from data/recorder.sqlite.

Usage: PYTHONPATH=. .venv/bin/python scripts/edge_report.py
"""

import argparse

from src.recorder import store
from src.recorder.analysis import build_report, format_report, load_observations


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/recorder.sqlite")
    args = ap.parse_args()
    conn = store.connect(args.db)
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("kalshi_snapshots", "odds_snapshots", "market_map", "results")}
    settled = conn.execute("SELECT COUNT(*) FROM results WHERE result IN ('yes','no')").fetchone()[0]
    print(f"Data so far: {counts} | settled markets: {settled}")
    print(format_report(build_report(load_observations(conn))))


if __name__ == "__main__":
    main()
