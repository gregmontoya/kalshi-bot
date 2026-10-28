"""Tests for the recorder + edge analysis. Run: PYTHONPATH=. .venv/bin/python tests/test_recorder.py"""

import random
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.recorder import store
from src.recorder.analysis import (MIN_SIGNALS, build_report, load_observations, taker_fee_per_contract)
from src.recorder.devig import devig_multiplicative, devig_power
from src.recorder.live_maker import CASH_FLOOR_DOLLARS, LiveMaker
from src.recorder.matching import match_event, outcome_name_for, team_matches, ticker_date


def test_devig_sums_to_one_and_removes_vig():
    for odds in ([1.91, 1.91], [1.78, 2.16], [1.28, 4.04], [2.1, 3.4, 3.6]):
        for fn in (devig_multiplicative, devig_power):
            p = fn(odds)
            assert abs(sum(p) - 1.0) < 1e-9, (fn.__name__, odds, p)
            assert all(pi < 1.0 / o + 1e-12 for pi, o in zip(p, odds)), "de-vig must lower each implied prob"
    assert abs(devig_power([1.91, 1.91])[0] - 0.5) < 1e-9
    # power method shades the longshot down relative to multiplicative
    assert devig_power([1.28, 4.04])[1] < devig_multiplicative([1.28, 4.04])[1]


def test_taker_fee_matches_spec():
    # GEMINI.md: $0.50 contract, 100 contracts -> $1.75
    assert abs(taker_fee_per_contract(0.50, 100) * 100 - 1.75) < 1e-9
    assert taker_fee_per_contract(0.80, 20) > 0


def test_team_name_matching_real_names():
    pos = [("Los Angeles C", "Los Angeles Clippers"), ("Los Angeles L", "Los Angeles Lakers"),
           ("New York I", "New York Islanders"), ("New York R", "New York Rangers"),
           ("St. Louis", "St Louis Blues"), ("Oklahoma City", "Oklahoma City Thunder"),
           ("Golden State", "Golden State Warriors"), ("Brighton", "Brighton and Hove Albion"),
           ("Tottenham", "Tottenham Hotspur"), ("New York G", "New York Giants"),
           ("Los Angeles", "Los Angeles Kings"), ("Manchester United", "Manchester United"),
           ("Chicago WS", "Chicago White Sox"), ("Chicago C", "Chicago Cubs"), ("A's", "Athletics"),
           ("A's", "Oakland Athletics"), ("New York Y", "New York Yankees"), ("Los Angeles D", "Los Angeles Dodgers")]
    neg = [("Los Angeles C", "Los Angeles Lakers"), ("New York I", "New York Rangers"),
           ("Manchester City", "Manchester United"), ("Miami", "Milwaukee Bucks"),
           ("New York J", "New York Giants"), ("Chicago WS", "Chicago Cubs"), ("Chicago C", "Chicago White Sox"),
           ("New York M", "New York Yankees")]
    for k, f in pos:
        assert team_matches(k, f), (k, f)
    for k, f in neg:
        assert not team_matches(k, f), (k, f)


def test_event_matching_and_ambiguity():
    assert str(ticker_date("KXNBAGAME-26OCT28CHADET")) == "2026-10-28"
    odds = [
        {"id": "e1", "home_team": "Detroit Pistons", "away_team": "Charlotte Hornets",
         "commence_time": "2026-10-28T23:10:00Z"},
        {"id": "e2", "home_team": "Detroit Pistons", "away_team": "Charlotte Hornets",
         "commence_time": "2026-11-15T23:10:00Z"},
        {"id": "e3", "home_team": "Arsenal", "away_team": "Chelsea", "commence_time": "2026-10-31T15:00:00Z"},
    ]
    ev = match_event("KXNBAGAME-26OCT28CHADET", ["Charlotte", "Detroit"], odds)
    assert ev and ev["id"] == "e1"
    assert outcome_name_for("Charlotte", ev) == "Charlotte Hornets"
    # late-evening ET game whose UTC commence is next day still matches
    ev = match_event("KXEPLGAME-26OCT31ARSCHE", ["Arsenal", "Chelsea", "Tie"], odds)
    assert ev and ev["id"] == "e3" and outcome_name_for("Tie", ev) == "Draw"
    assert match_event("KXNBAGAME-26OCT28CHADET", ["Charlotte", "Boston"], odds) is None
    assert match_event("KXNBAGAME-26NOV05CHADET", ["Charlotte", "Detroit"], odds) is None  # wrong date
    dup = odds[:1] + [dict(odds[0], id="e9")]
    assert match_event("KXNBAGAME-26OCT28CHADET", ["Charlotte", "Detroit"], dup) is None  # ambiguous


def _synthetic_db(path: Path, n_games: int, kalshi_bias: float, seed: int):
    """Games where Pinnacle is the true probability; Kalshi ask = truth - kalshi_bias (+ noise)."""
    rnd = random.Random(seed)
    conn = store.connect(path)
    base = datetime(2026, 10, 1, tzinfo=timezone.utc)
    for g in range(n_games):
        commence = base + timedelta(hours=3 * g)
        p_true = rnd.uniform(0.35, 0.80)
        vig = 1.04
        o_home, o_away = 1.0 / (p_true * vig), 1.0 / ((1 - p_true) * vig)
        ev_id, tk_h, tk_a = f"ev{g}", f"KXT-G{g}-H", f"KXT-G{g}-A"
        won_home = rnd.random() < p_true
        z = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731  (stored format)
        snap_ts = z(commence - timedelta(minutes=10))
        odds_ts = z(commence - timedelta(minutes=20))
        for tk, outcome, p, won in ((tk_h, "Home", p_true, won_home), (tk_a, "Away", 1 - p_true, not won_home)):
            ask = min(0.97, max(0.03, round(p - kalshi_bias + rnd.gauss(0, 0.01), 2)))
            conn.execute("INSERT INTO kalshi_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (snap_ts, tk, f"KXT-G{g}", "KXT", outcome, ask - 0.02, ask, ask, 100, 0,
                          z(commence), "active"))
            store.upsert_map(conn, tk, f"KXT-G{g}", "KXT", "x", ev_id, outcome, z(commence), snap_ts)
            store.upsert_result(conn, tk, "yes" if won else "no", "finalized", snap_ts)
        store.insert_odds(conn, odds_ts, "x", [{
            "id": ev_id, "commence_time": z(commence), "home_team": "Home", "away_team": "Away",
            "bookmakers": [{"key": "pinnacle", "last_update": odds_ts, "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": o_home}, {"name": "Away", "price": o_away}]}]}]}])
    conn.commit()
    return conn


def test_report_detects_real_edge_and_rejects_none():
    with tempfile.TemporaryDirectory() as d:
        edge_conn = _synthetic_db(Path(d) / "edge.sqlite", n_games=1500, kalshi_bias=0.08, seed=1)
        rep = build_report(load_observations(edge_conn))
        s = rep["entries"]["snap"]["thresholds"][0.03]
        assert rep["entries"]["close"]["thresholds"][0.03]["n"] == s["n"]
        assert s["n"] >= MIN_SIGNALS and s["mean_pnl"] > 0, s
        assert rep["verdict"] == "PASS", rep

        fair_conn = _synthetic_db(Path(d) / "fair.sqlite", n_games=1500, kalshi_bias=0.0, seed=2)
        rep = build_report(load_observations(fair_conn))
        assert rep["verdict"] == "NOT PROVEN", rep["entries"]["close"]["thresholds"]

        small = _synthetic_db(Path(d) / "small.sqlite", n_games=40, kalshi_bias=0.08, seed=3)
        assert build_report(load_observations(small))["verdict"] == "NOT PROVEN"  # too few games


def test_stale_odds_are_ignored():
    with tempfile.TemporaryDirectory() as d:
        conn = _synthetic_db(Path(d) / "s.sqlite", n_games=5, kalshi_bias=0.05, seed=4)
        for r in conn.execute("SELECT DISTINCT ts FROM odds_snapshots").fetchall():
            old = datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
            conn.execute("UPDATE odds_snapshots SET ts=? WHERE ts=?",
                         ((old - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"), r["ts"]))
        conn.commit()
        assert load_observations(conn) == []


def test_live_maker_guardrails():
    # Valid $10 max per game at $0.60 maker bid -> 16 contracts ($9.60)
    cts, reason = LiveMaker.size_order(0.60, 0.61, 0.625, 358.72, CASH_FLOOR_DOLLARS, 0.0)
    assert cts == 16 and reason == "ok"
    # Crossing the spread (maker_price >= ask) is rejected
    assert LiveMaker.size_order(0.61, 0.61, 0.640, 358.72)[0] == 0
    # Edge below 2.0c is rejected
    assert LiveMaker.size_order(0.60, 0.61, 0.615, 358.72)[0] == 0
    # Cash at or below $308.72 floor is rejected
    assert LiveMaker.size_order(0.60, 0.61, 0.630, 308.72)[0] == 0
    # $45 already committed out of $50 cap -> only $5 left -> 8 contracts at $0.60 ($4.80)
    cts, _ = LiveMaker.size_order(0.60, 0.61, 0.630, 358.72, CASH_FLOOR_DOLLARS, 45.0)
    assert cts == 8


if __name__ == "__main__":
    import sys
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print("PASS", t.__name__)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("FAIL", t.__name__, repr(e))
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
