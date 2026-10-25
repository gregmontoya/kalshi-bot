"""Tests for KalshiSimulator Paper Trading Engine."""

import pytest
from engine.core.models import OrderAction, OrderSide, ReciprocalOrderBook
from engine.core.orderbook import KalshiOrderBookEngine
from engine.exchange.simulator import KalshiSimulator


def test_simulator_cash_and_maker_fee_accounting():
    sim = KalshiSimulator(initial_cash_cents=100_000)  # $1,000.00
    assert sim.cash_balance_cents == 100_000

    # Place Maker BUY limit order for 10 contracts of YES @ 70c
    order = sim.place_order(
        ticker="DEMO-NFL-KC",
        action=OrderAction.BUY,
        side=OrderSide.YES,
        count=10,
        yes_price=70,
        post_only=True,
    )

    assert order.order_id in sim.resting_orders
    assert sim.cash_balance_cents == 100_000  # Cash not deducted until fill

    # Simulate market tick where best bid reaches 70c (fill occurs!)
    tick_payload = {
        "yes": [[70, 50]],
        "no": [[28, 50]],  # Synthesized ask is 72c
    }
    book = KalshiOrderBookEngine.parse_kalshi_orderbook("DEMO-NFL-KC", tick_payload)
    filled = sim.on_market_tick("DEMO-NFL-KC", book)

    assert len(filled) == 1
    assert filled[0].order_id == order.order_id
    # Maker fee paid is $0.00
    assert filled[0].maker_fee_paid_cents == 0
    # Cost was 10 * 70c = 700c ($7.00)
    assert sim.cash_balance_cents == 100_000 - 700
    # Position created
    assert "DEMO-NFL-KC" in sim.positions
    assert sim.positions["DEMO-NFL-KC"].contracts == 10
    # Fees saved tracked
    assert sim.total_maker_fees_saved_cents > 0


def test_simulator_sell_position_floor_safety_law():
    """Mandatory Safety Rule: sell_position_floor: 0 prevents accidental naked shorting."""
    sim = KalshiSimulator(initial_cash_cents=50_000)

    # Attempt to sell 5 contracts without holding any position -> MUST FAIL
    with pytest.raises(ValueError) as exc:
        sim.place_order(
            ticker="DEMO-TICKER",
            action=OrderAction.SELL,
            side=OrderSide.YES,
            count=5,
            yes_price=80,
        )
    assert "sell_position_floor: 0" in str(exc.value)


def test_simulator_tp_fill_and_pnl():
    sim = KalshiSimulator(initial_cash_cents=100_000)

    # Buy 10 contracts @ 70c
    order = sim.place_order(
        ticker="DEMO-KC",
        action=OrderAction.BUY,
        side=OrderSide.YES,
        count=10,
        yes_price=70,
        post_only=True,
    )
    book_fill = KalshiOrderBookEngine.parse_kalshi_orderbook("DEMO-KC", {"yes": [[70, 100]], "no": [[28, 100]]})
    sim.on_market_tick("DEMO-KC", book_fill)
    assert sim.positions["DEMO-KC"].contracts == 10

    # Rest TP order @ 90c
    tp_order = sim.place_order(
        ticker="DEMO-KC",
        action=OrderAction.SELL,
        side=OrderSide.YES,
        count=10,
        yes_price=90,
        post_only=True,
    )

    # Price moves up to 90c
    book_tp = KalshiOrderBookEngine.parse_kalshi_orderbook("DEMO-KC", {"yes": [[88, 100]], "no": [[10, 100]]})  # Synthesized ask is 90c!
    filled_tp = sim.on_market_tick("DEMO-KC", book_tp)

    assert len(filled_tp) == 1
    # Cash should now be: 100,000 - 700 + 900 = 100,200 (+200c / +$2.00 profit)
    assert sim.cash_balance_cents == 100_200
    assert "DEMO-KC" not in sim.positions  # Fully closed
