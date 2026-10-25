"""Tests for Mathematical & Quantitative Engine."""

import math
import pytest
from engine.core.math_engine import MathEngine


def test_taker_fee_formula():
    """Kalshi Taker fee: ceil(0.07 * contracts * P * (1-P))"""
    # 100 contracts at 50c (P = 0.5):
    # 0.07 * 100 * 0.5 * 0.5 * 100 = 175 cents ($1.75)
    fee = MathEngine.calculate_taker_fee(contracts=100, price_cents=50)
    assert fee == 175

    # Maker fee is always $0.00
    maker_fee = MathEngine.calculate_maker_fee(contracts=100, price_cents=50)
    assert maker_fee == 0


def test_devig_multiplicative_sums_to_100_percent():
    # Pinnacle odds: Kansas City Chiefs 1.50 (-200), Denver Broncos 2.70 (+170)
    # Raw implied: 1/1.50 = 0.66667, 1/2.70 = 0.37037 (Sum = 1.03704)
    raw_probs = [1.0 / 1.50, 1.0 / 2.70]
    devigged = MathEngine.devig_multiplicative(raw_probs)

    assert len(devigged) == 2
    assert math.isclose(sum(devigged), 1.0, rel_tol=1e-5)
    assert devigged[0] > devigged[1]
    assert math.isclose(devigged[0], 0.64285, rel_tol=1e-3)


def test_devig_shin_two_way():
    # Pinnacle odds: -150 (decimal 1.6667) and +130 (decimal 2.30)
    p_home, p_away = MathEngine.devig_two_way(odds_home=1.6667, odds_away=2.30, method="shin")

    assert math.isclose(p_home + p_away, 1.0, rel_tol=1e-4)
    assert p_home > p_away
    # In Shin model, longshot bias is slightly adjusted compared to favorite
    assert 0.55 < p_home < 0.65


def test_green_out_formula_equal_guaranteed_profit():
    # Bought 25 YES contracts at 42c (Pod 2 value buy)
    # Current YES bid rose to 58c (+16c gain)
    result = MathEngine.calculate_green_out(
        entry_price_cents=42,
        current_yes_bid_cents=58,
        initial_contracts=25,
    )

    assert result["gain_cents"] == 16
    assert result["is_green_out_eligible"] is True
    # 25 contracts * 16c = 400c ($4.00 guaranteed profit)
    assert result["guaranteed_profit_cents"] == 400
    assert result["guaranteed_profit_dollars"] == 4.00

    # Sell all action
    sell_action = result["sell_all_action"]
    assert sell_action["contracts"] == 25
    assert sell_action["price_cents"] == 58
    assert sell_action["post_only"] is True

    # Opposing NO hedge action (buy 25 NO @ 100 - 58 = 42c)
    no_action = result["buy_opposing_no_action"]
    assert no_action["contracts"] == 25
    assert no_action["price_cents"] == 42


def test_tail_lock_probability_kneel_down():
    # Scenario: 4th quarter, 45 seconds left, Team A leads by 4 points.
    # Team A has ball, 2nd down (2 kneel downs left = 2 * 40 = 80s burnable).
    # Opponent has 0 timeouts. Opponent cannot stop clock!
    res = MathEngine.calculate_tail_lock_probability(
        is_fourth_quarter=True,
        seconds_remaining=45,
        score_margin=4,
        has_possession=True,
        down=2,
        opp_timeouts_remaining=0,
    )

    assert res["qualifies_tail_lock"] is True
    assert res["calculated_win_prob"] == 1.000
    assert "Kneel-Down Lock" in res["reason"]


def test_tail_lock_probability_multi_score():
    # 4th quarter, 2 minutes left (120s), Team leads by 17 points (3 scores)
    res = MathEngine.calculate_tail_lock_probability(
        is_fourth_quarter=True,
        seconds_remaining=120,
        score_margin=17,
        has_possession=False,
        down=1,
        opp_timeouts_remaining=2,
    )

    assert res["qualifies_tail_lock"] is True
    assert res["calculated_win_prob"] >= 0.992
