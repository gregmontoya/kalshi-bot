"""Unit tests for Strategy Pods 1, 2, and 3."""

import pytest
from unittest.mock import AsyncMock, MagicMock
from engine.core.models import (
    Basket,
    BasketLeg,
    Order,
    OrderAction,
    OrderSide,
    OrderStatus,
    OrderType,
    SharpOdds,
)
from engine.exchange.simulator import KalshiSimulator
from engine.pods.maker_momentum_basket import MakerMomentumBasketPod
from engine.pods.sharp_value_green_out import SharpValueGreenOutPod
from engine.pods.tail_lock_harvester import TailLockHarvesterPod
from engine.storage.duckdb_manager import DuckDBManager


@pytest.fixture
def memory_duckdb(tmp_path):
    db_path = str(tmp_path / "test_kalshi.duckdb")
    return DuckDBManager(db_path=db_path)


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.create_order = AsyncMock()
    client.cancel_order = AsyncMock()
    return client


@pytest.fixture
def mock_odds():
    odds = MagicMock()
    odds.get_sharp_odds = AsyncMock(return_value=[
        SharpOdds(
            event_id="test-1",
            sport="nfl",
            team_home="Kansas City Chiefs",
            team_away="Denver Broncos",
            bookmaker="pinnacle",
            home_raw_odds=1.35,
            away_raw_odds=3.40,
            home_devigged_prob=0.72,
            away_devigged_prob=0.28,
        )
    ])
    return odds


@pytest.mark.asyncio
async def test_pod1_basket_creation_and_dip_entry(mock_client, memory_duckdb, mock_odds):
    sim = KalshiSimulator()
    pod = MakerMomentumBasketPod(
        client=mock_client,
        simulator=sim,
        duckdb=memory_duckdb,
        odds_client=mock_odds,
        trading_mode="PAPER",
    )
    await pod.start()

    plays = [
        {"ticker": "NFL-KC", "market_title": "Kansas City Chiefs Win", "kalshi_ask": 72, "sharp_prob_float": 0.76},
        {"ticker": "NFL-PHI", "market_title": "Philadelphia Eagles Win", "kalshi_ask": 70, "sharp_prob_float": 0.74},
    ]

    basket = await pod.create_synthetic_basket(plays)

    assert basket is not None
    assert len(basket.legs) == 2
    # Verify resting limit bid placed 2c below current price (72 -> 70c, 70 -> 68c)
    assert basket.legs[0].target_entry_price == 70
    assert basket.legs[1].target_entry_price == 68
    assert basket.legs[0].status == "pending_maker_bid"
    assert basket.legs[0].tp_price == 90

    # Verify DuckDB persistence
    db_basket = memory_duckdb.get_basket(basket.basket_id)
    assert db_basket is not None
    assert len(db_basket.legs) == 2


@pytest.mark.asyncio
async def test_pod1_guillotine_hold_on_high_sharp_prob(mock_client, memory_duckdb, mock_odds):
    """Guillotine rule: if leg <= 48c but sharp prob >= 58%, HOLD (ignore retail panic)."""
    sim = KalshiSimulator()
    pod = MakerMomentumBasketPod(
        client=mock_client,
        simulator=sim,
        duckdb=memory_duckdb,
        odds_client=mock_odds,
        trading_mode="PAPER",
    )
    await pod.start()

    leg = BasketLeg(
        ticker="NFL-KC",
        market_title="Kansas City Chiefs Win",
        target_entry_price=70,
        actual_entry_price=70,
        contracts=10,
        status="tp_resting",
        tp_price=90,
        current_price=70,
        current_sharp_prob=0.72,
    )
    basket = Basket(basket_id="bkt-test-1", status="active", legs=[leg])
    pod.active_baskets["bkt-test-1"] = basket

    # Current bid drops to 45c (<= 48c trigger!)
    # Sharp odds is 0.72 (>= 0.58 threshold)
    await pod._evaluate_guillotine(basket, leg, current_bid=45)

    # Leg should NOT be guillotined! Status remains tp_resting!
    assert leg.guillotine_triggered is False
    assert leg.status == "tp_resting"


@pytest.mark.asyncio
async def test_pod1_guillotine_exit_on_low_sharp_prob(mock_client, memory_duckdb):
    """Guillotine rule: if leg <= 48c AND sharp prob < 50%, fire immediate exit!"""
    sim = KalshiSimulator()
    mock_sharp_crush = MagicMock()
    mock_sharp_crush.get_sharp_odds = AsyncMock(return_value=[
        SharpOdds(
            event_id="test-1",
            sport="nfl",
            team_home="Kansas City Chiefs",
            team_away="Denver Broncos",
            bookmaker="pinnacle",
            home_raw_odds=2.20,
            away_raw_odds=1.70,
            home_devigged_prob=0.44,  # Dropped < 50%!
            away_devigged_prob=0.56,
        )
    ])

    pod = MakerMomentumBasketPod(
        client=mock_client,
        simulator=sim,
        duckdb=memory_duckdb,
        odds_client=mock_sharp_crush,
        trading_mode="PAPER",
    )
    await pod.start()

    # Pre-seed position in simulator so exit does not violate sell_position_floor
    sim.place_order("NFL-KC", OrderAction.BUY, OrderSide.YES, 10, 70, post_only=True)
    sim.positions["NFL-KC"] = MagicMock(contracts=10, side=OrderSide.YES, avg_price_cents=70.0)

    leg = BasketLeg(
        ticker="NFL-KC",
        market_title="Kansas City Chiefs Win",
        target_entry_price=70,
        actual_entry_price=70,
        contracts=10,
        status="tp_resting",
        tp_price=90,
        current_price=70,
        current_sharp_prob=0.72,
    )
    basket = Basket(basket_id="bkt-test-2", status="active", legs=[leg])
    pod.active_baskets["bkt-test-2"] = basket

    # Current bid drops to 46c and sharp prob dropped to 0.44 (<0.50)
    await pod._evaluate_guillotine(basket, leg, current_bid=46)

    # Leg MUST be guillotined immediately!
    assert leg.guillotine_triggered is True
    assert leg.status == "guillotined"
    assert leg.exit_price == 46


@pytest.mark.asyncio
async def test_pod2_green_out_execution(mock_client, memory_duckdb, mock_odds):
    sim = KalshiSimulator()
    pod = SharpValueGreenOutPod(
        client=mock_client,
        simulator=sim,
        duckdb=memory_duckdb,
        odds_client=mock_odds,
        trading_mode="PAPER",
    )
    await pod.start()

    # Pre-seed position: 25 contracts @ 40c
    sim.positions["NFL-VALUE-1"] = MagicMock(contracts=25, side=OrderSide.YES, avg_price_cents=40.0)
    pod.active_positions["NFL-VALUE-1"] = {
        "ticker": "NFL-VALUE-1",
        "contracts": 25,
        "entry_price": 40,
        "current_price": 40,
        "is_greened_out": False,
        "green_out_order_id": None,
        "guaranteed_profit_cents": 0,
    }

    # Bid jumps to 56c (+16c gain!)
    await pod._execute_midgame_green_out("NFL-VALUE-1", pod.active_positions["NFL-VALUE-1"], current_bid=56)

    assert pod.active_positions["NFL-VALUE-1"]["green_out_order_id"] is not None
    # 25 contracts * 16c = 400c ($4.00)
