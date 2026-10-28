# Kalshi Edge Lab

A small, testable system for finding out whether a betting edge actually exists **before** any money is risked.

> The previous auto-trading system (Oct 25–28, 2026) is archived on the git branch
> `archive/gemini-legacy-20261028`. It lost money because its entries had no edge
> (80.3% settled win rate vs 80.5¢ average price paid). See `docs/AUDIT_2026-10-28.md`.

## Pipeline (each stage must pass before the next exists)

1. **Recorder** (`scripts/run_recorder.py`): snapshots Kalshi game prices, Pinnacle odds, and settlements into `data/recorder.sqlite`. Public data only, no orders.
2. **Edge report** (`scripts/edge_report.py`): pre-registered test. PASS needs ≥200 games, positive mean P&L after taker fees, and t > 2.
3. **Paper trader**: built only if stage 2 passes.
4. **Live**: small fixed size, hold to settlement. Built only if stage 3 passes.

## Commands

```bash
PYTHONPATH=. .venv/bin/python tests/test_recorder.py      # tests
PYTHONPATH=. .venv/bin/python scripts/run_recorder.py     # recorder daemon
PYTHONPATH=. .venv/bin/python scripts/edge_report.py      # current verdict
```

Requires `THE_ODDS_API_KEY` in `.env` for Pinnacle odds.
