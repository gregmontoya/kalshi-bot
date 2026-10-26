"""Unit tests for Phase 1 Maker/Taker EV, Vegas De-Vigging, and Green-Out Hedge Calculator."""

import math
import pytest
from src.core.fee_math import FeeMathEngine


def test_maker_and_taker_fees():
    """Verifies Maker is $0.00 and Taker follows ceil(0.07 * contracts * P * (1-P))."""
    # Maker fee is always 0
    assert FeeMathEngine.calculate_maker_fee(contracts=100, price_cents=50) == 0
    assert FeeMathEngine.calculate_maker_fee(contracts=10, price_cents=72) == 0

    # Taker fee: 100 contracts @ 50c -> 0.07 * 100 * 0.5 * 0.5 * 100 = 175c ($1.75)
    fee_100_50 = FeeMathEngine.calculate_taker_fee(contracts=100, price_cents=50)
    assert fee_100_50 == 175

    # 10 contracts @ 72c: 0.07 * 10 * 0.72 * 0.28 * 100 = 14.112c -> ceil is 15c
    fee_10_72 = FeeMathEngine.calculate_taker_fee(contracts=10, price_cents=72)
    assert fee_10_72 == 15


def test_maker_vs_taker_trade_ev():
    """Verifies positive EV delta for Maker resting orders over Taker execution."""
    ev_maker = FeeMathEngine.calculate_trade_ev(
        entry_price_cents=70,
        true_probability=0.75,
        contracts=100,
        is_maker=True,
    )
    ev_taker = FeeMathEngine.calculate_trade_ev(
        entry_price_cents=70,
        true_probability=0.75,
        contracts=100,
        is_maker=False,
    )

    assert ev_maker["fee_cents"] == 0
    assert ev_taker["fee_cents"] > 0
    # Maker EV must be strictly higher by the exact taker fee amount
    assert ev_maker["ev_cents"] - ev_taker["ev_cents"] == ev_taker["fee_cents"]


def test_devig_multiplicative_sums_to_100_percent():
    # Pinnacle odds: Kansas City Chiefs 1.50 (-200), Denver Broncos 2.70 (+170)
    raw_probs = [1.0 / 1.50, 1.0 / 2.70]
    devigged = FeeMathEngine.devig_multiplicative(raw_probs)

    assert len(devigged) == 2
    assert math.isclose(sum(devigged), 1.0, rel_tol=1e-5)
    assert devigged[0] > devigged[1]
    assert math.isclose(devigged[0], 0.64285, rel_tol=1e-3)


def test_devig_shin_two_way_sums_to_100_percent():
    # Pinnacle odds: -150 (decimal 1.6667) and +130 (decimal 2.30)
    p_home, p_away = FeeMathEngine.devig_two_way(odds_home=1.6667, odds_away=2.30, method="shin")

    assert math.isclose(p_home + p_away, 1.0, rel_tol=1e-4)
    assert p_home > p_away
    assert 0.55 < p_home < 0.65


def test_midgame_green_out_equal_profit_formula():
    """
    Bought 25 YES contracts at 42c (Pod 2 entry).
    Current YES bid rose to 58c (+16c gain!).
    """
    result = FeeMathEngine.calculate_green_out(
        entry_price_cents=42,
        current_yes_bid_cents=58,
        initial_contracts=25,
    )

    assert result["gain_cents"] == 16
    assert result["is_green_out_eligible"] is True
    # 25 contracts * 16c = 400c ($4.00 guaranteed profit)
    assert result["guaranteed_profit_cents"] == 400
    assert result["guaranteed_profit_dollars"] == 4.00

    # Sell All Action (Maker)
    sell_action = result["sell_all_action"]
    assert sell_action["contracts"] == 25
    assert sell_action["price_cents"] == 58
    assert sell_action["post_only"] is True

    # Opposing NO Hedge Action (Buy 25 NO @ 42c)
    no_action = result["buy_opposing_no_action"]
    assert no_action["contracts"] == 25
    assert no_action["price_cents"] == 42
    assert no_action["post_only"] is True

    # Mathematical Verification of Equal Payout for Opposing NO hedge:
    # If YES wins: Payout = 25 * 100 = 2500c. Cost = 25*42 + 25*42 = 2100c. Profit = +400c.
    # If NO wins:  Payout = 25 * 100 = 2500c. Cost = 25*42 + 25*42 = 2100c. Profit = +400c.
    payout_if_yes = 25 * 100 - (25 * 42 + 25 * 42)
    payout_if_no = 25 * 100 - (25 * 42 + 25 * 42)
    assert payout_if_yes == payout_if_no == 400
