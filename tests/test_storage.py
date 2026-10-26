"""Unit tests for Phase 3 Analytical Persistence: DuckDB, Firebase sync, and BigQuery Grader."""

import pytest
from src.core.models import Basket, BasketLeg, Order, OrderAction, OrderSide, OrderStatus
from src.storage.bigquery_grader import BigQueryGrader
from src.storage.duckdb_manager import DuckDBManager
from src.storage.firebase_sync import FirebaseSyncManager


@pytest.fixture
def temp_duckdb(tmp_path):
    db_file = str(tmp_path / "test_quant.duckdb")
    return DuckDBManager(db_path=db_file)


def test_duckdb_basket_and_leg_persistence(temp_duckdb):
    leg1 = BasketLeg(
        ticker="NFL-KC",
        market_title="Kansas City Chiefs Win",
        target_entry_price=70,
        actual_entry_price=70,
        contracts=10,
        status="filled_holding",
        tp_price=90,
        current_price=74,
        pnl_cents=40,
    )
    leg2 = BasketLeg(
        ticker="NFL-PHI",
        market_title="Philadelphia Eagles Win",
        target_entry_price=68,
        actual_entry_price=68,
        contracts=10,
        status="tp_resting",
        tp_price=90,
        current_price=71,
        pnl_cents=30,
    )

    basket = Basket(
        basket_id="bkt-unit-test-1",
        status="active",
        legs=[leg1, leg2],
        total_invested_cents=1380,
    )

    # Save to DuckDB
    temp_duckdb.save_basket(basket)

    # Retrieve and verify
    retrieved = temp_duckdb.get_basket("bkt-unit-test-1")
    assert retrieved is not None
    assert retrieved.basket_id == "bkt-unit-test-1"
    assert retrieved.status == "active"
    assert len(retrieved.legs) == 2
    assert retrieved.legs[0].ticker == "NFL-KC"
    assert retrieved.legs[0].status == "filled_holding"
    assert retrieved.legs[1].ticker == "NFL-PHI"


def test_duckdb_order_and_fill_recording(temp_duckdb):
    order = Order(
        order_id="ord-test-1",
        client_order_id="cid-test-1",
        ticker="NFL-DET",
        action=OrderAction.BUY,
        side=OrderSide.YES,
        count=25,
        yes_price=42,
        no_price=58,
        post_only=True,
        status=OrderStatus.FILLED,
        strategy_pod="sharp_value_green_out",
    )

    temp_duckdb.record_order(order)
    temp_duckdb.record_fill(
        order=order,
        fill_price_cents=42,
        is_maker=True,
        fee_cents=0,
    )

    # Verify query
    rows = temp_duckdb.conn.execute("SELECT order_id, ticker, action FROM orders WHERE order_id = 'ord-test-1'").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "ord-test-1"

    fills = temp_duckdb.conn.execute("SELECT order_id, is_maker, fee_cents FROM fills WHERE order_id = 'ord-test-1'").fetchall()
    assert len(fills) == 1
    assert fills[0][1] is True
    assert fills[0][2] == 0


def test_bigquery_grader_brier_and_clv():
    grader = BigQueryGrader()

    # Predictions and actual outcomes
    predictions = [0.80, 0.70, 0.60, 0.90]
    outcomes = [1, 1, 0, 1]

    # BS = 1/4 * [(0.8-1)^2 + (0.7-1)^2 + (0.6-0)^2 + (0.9-1)^2]
    # BS = 1/4 * [0.04 + 0.09 + 0.36 + 0.01] = 0.50 / 4 = 0.125
    brier_score = grader.calculate_brier_score(predictions, outcomes)
    assert brier_score == 0.125

    # CLV test: entered at 70c (0.70), closed at 75c (0.75) -> +7.14% CLV
    entry_probs = [0.70]
    closing_probs = [0.75]
    clv = grader.calculate_clv(entry_probs, closing_probs)
    assert clv > 0.07

    # Pod performance report
    trades = [
        {"predicted_prob": 0.80, "outcome": "win", "entry_price_cents": 70, "closing_sharp_prob": 0.76, "pnl_cents": 200},
        {"predicted_prob": 0.75, "outcome": "win", "entry_price_cents": 68, "closing_sharp_prob": 0.74, "pnl_cents": 220},
    ]
    report = grader.grade_pod_performance("maker_momentum_basket", trades)
    assert report["trades_count"] == 2
    assert report["win_rate_pct"] == 100.0
    assert "A" in report["grade"]


@pytest.mark.asyncio
async def test_firebase_sync_manager_local_fallback():
    sync = FirebaseSyncManager(credentials_path=None)
    data = {"engine": "active", "mode": "PAPER", "balance": 10000}

    await sync.sync_state("test_status", data)
    retrieved = sync.get_synced_state("test_status")

    assert retrieved is not None
    assert retrieved["engine"] == "active"
    assert retrieved["balance"] == 10000
