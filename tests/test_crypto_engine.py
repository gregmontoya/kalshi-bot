"""Unit tests for the Standalone Crypto Quant Engine (Fees, Green-Out, Delta-Neutral, and Orchestrator)."""

import pytest
from src.crypto.fee_math import CryptoFeeMathEngine
from src.crypto.models import CryptoVenue
from src.crypto.orchestrator import CryptoEngineOrchestrator


def test_crypto_maker_rebate_and_fee_savings():
    # Hyperliquid Maker order on $1,000 notional pays -1.0 bps ($0.10 rebate) vs $6.00 retail taker fee
    res = CryptoFeeMathEngine.calculate_order_fee_usd(
        notional_usd=1000.0, venue=CryptoVenue.HYPERLIQUID, is_maker=True
    )
    assert res["fee_usd"] == -0.10
    assert res["rebate_earned_usd"] == 0.10
    assert res["saved_vs_retail_taker_usd"] == 6.10


def test_crypto_wick_green_out_plan():
    # Catch SOL wick at $173.50, bounce to $178.00 -> sell 60% and lock risk-free profit
    plan = CryptoFeeMathEngine.calculate_green_out_plan(
        entry_price=173.50,
        current_price=178.00,
        quantity=5.7637,
        scale_out_fraction=0.60,
        protective_stop_buffer_pct=0.45,
        venue=CryptoVenue.HYPERLIQUID,
    )
    assert plan["locked_realized_profit_usd"] > 15.0
    assert plan["new_protective_stop_price"] > 173.50
    assert plan["guaranteed_min_total_profit_usd"] > plan["locked_realized_profit_usd"]


def test_delta_neutral_funding_lock_math():
    lock = CryptoFeeMathEngine.calculate_delta_neutral_lock(
        spot_price=176.40,
        perp_price=176.72,
        funding_rate_hourly_bps=3.60,
        pair_notional_usd=1000.0,
    )
    assert lock["directional_price_risk_pct"] == 0.0
    assert lock["annualized_funding_apy"] > 30.0
    assert lock["daily_cash_yield_usd"] > 0.0


@pytest.mark.asyncio
async def test_crypto_orchestrator_snapshot_and_green_out():
    engine = CryptoEngineOrchestrator()
    snap = await engine.get_dashboard_snapshot("SOL")
    assert snap["isolated_from_kalshi_sports"] is True
    assert snap["bankroll"]["current_equity_usd"] > snap["bankroll"]["starting_equity_usd"]
    assert len(snap["pod1_momentum_basket"]["baskets"][0]["legs"]) == 3
    assert len(snap["pod2_wick_green_out"]["positions"]) == 2
    assert len(snap["pod3_funding_tail_lock"]["locked_pairs"]) == 2

    # Trigger Green-Out on remaining open profitable positions
    greened_count = engine.trigger_green_out_all()
    assert greened_count == 1
