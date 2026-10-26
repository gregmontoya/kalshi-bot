"""Unit tests for Phase 4 Institutional Risk, Circuit Breakers, Whale Tape, and CLV Grader."""

import pytest
from src.core.models import OrderBookLevel, ReciprocalOrderBook
from src.core.orderbook import KalshiOrderBookEngine
from src.feeds.entity_mapper import EntityMapper
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.clv_grader import InstitutionalCLVGrader


def test_circuit_breaker_sell_position_floor():
    cb = CircuitBreaker(max_daily_drawdown_dollars=500.0)

    # Attempt to sell 15 contracts while only holding 10 -> REJECTED
    safe, reason = cb.check_order_safety(
        action="sell",
        contracts=15,
        held_contracts=10,
        total_cash_cents=100_000,
        order_cost_cents=1050,
    )
    assert safe is False
    assert "sell_position_floor: 0" in reason

    # Sell 10 contracts while holding 10 -> APPROVED
    safe_ok, _ = cb.check_order_safety(
        action="sell",
        contracts=10,
        held_contracts=10,
        total_cash_cents=100_000,
        order_cost_cents=700,
    )
    assert safe_ok is True


def test_circuit_breaker_daily_drawdown_tripwire():
    cb = CircuitBreaker(max_daily_drawdown_dollars=100.00)

    # First loss of $40 (4000c) -> Not tripped
    cb.record_pnl(-4000)
    assert cb.is_tripped is False

    # Second loss of $70 (7000c) -> Total loss $110 >= $100 -> TRIPS BREAKER!
    cb.record_pnl(-7000)
    assert cb.is_tripped is True
    assert "Daily drawdown cap hit" in cb.trip_reason

    # All further orders must be rejected
    safe, reason = cb.check_order_safety(
        action="buy",
        contracts=10,
        held_contracts=0,
        total_cash_cents=100_000,
        order_cost_cents=700,
    )
    assert safe is False
    assert "TRIPPED" in reason


def test_clv_truth_meter():
    # Entered at 70c, closed at 74c 1 min before kickoff -> +5.71% CLV
    clv_result = InstitutionalCLVGrader.calculate_clv(entry_price_cents=70, closing_price_cents=74)
    assert clv_result["has_positive_clv"] is True
    assert clv_result["clv_pct"] == 5.71

    # Entered at 70c, closed at 65c -> -7.14% CLV
    clv_bad = InstitutionalCLVGrader.calculate_clv(entry_price_cents=70, closing_price_cents=65)
    assert clv_bad["has_positive_clv"] is False
    assert clv_bad["clv_pct"] == -7.14


def test_exponential_decay_brier_score():
    trades = [
        {"predicted_prob": 0.80, "outcome": "win", "timestamp": "2026-10-25T12:00:00"},
        {"predicted_prob": 0.75, "outcome": "win", "timestamp": "2026-10-24T12:00:00"},
        {"predicted_prob": 0.70, "outcome": "win", "timestamp": "2026-10-23T12:00:00"},
        {"predicted_prob": 0.50, "outcome": "loss", "timestamp": "2026-10-20T12:00:00"},
        {"predicted_prob": 0.40, "outcome": "loss", "timestamp": "2026-10-18T12:00:00"},
    ]
    res = InstitutionalCLVGrader.calculate_exponential_decay_brier_score(trades)
    assert res["sample_size"] == 5
    assert res["weighted_brier_score"] < 0.20
    assert res["confidence_weight"] > 0.60


def test_whale_tape_reader_10_to_1_imbalance():
    # Construct book with heavy bid depth ($720 bids vs $50 asks -> 14.4:1 ratio)
    book = ReciprocalOrderBook(
        ticker="NFL-KC-WHALE",
        timestamp=1714521600000,
        yes_bids=[OrderBookLevel(price=72, quantity=1000)],  # $720.00
        no_bids=[OrderBookLevel(price=95, quantity=100)],    # Synthesized ask is 5c, $50.00
    )

    imbalance = KalshiOrderBookEngine.check_whale_tape_imbalance(book, threshold_ratio=10.0)
    assert imbalance["has_whale_alert"] is True
    assert "BUY" in imbalance["whale_direction"]
    assert imbalance["imbalance_ratio"] >= 10.0


def test_entity_mapper_nfl_aliases():
    assert EntityMapper.match_team_from_ticker_or_title("KXNFL-KC-DEN") == "Kansas City Chiefs"
    assert EntityMapper.match_team_from_ticker_or_title("Philadelphia Eagles Win Margin") == "Philadelphia Eagles"
    assert EntityMapper.match_team_from_ticker_or_title("DET @ GB Moneyline") == "Detroit Lions"
