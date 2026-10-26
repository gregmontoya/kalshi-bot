"""Unit and integration tests for Phase 2 Exchange Clients and Simulator."""

import pytest
from unittest.mock import AsyncMock, patch
from src.core.auth import KalshiAuthSigner
from src.core.models import OrderAction, OrderSide, OrderStatus, OrderType
from src.core.orderbook import KalshiOrderBookEngine
from src.exchange.kalshi_client import KalshiClient
from src.exchange.kalshi_ws import KalshiWebSocketClient
from src.exchange.simulator import KalshiSimulator


@pytest.fixture
def auth_signer():
    priv, _ = KalshiAuthSigner.generate_key_pair(2048)
    return KalshiAuthSigner(key_id="test-key-exchange", private_key=priv)


@pytest.mark.asyncio
async def test_kalshi_client_mandatory_safety_flags(auth_signer):
    """
    Non-negotiable Exchange Laws:
    - sell_position_floor: 0 on all sell orders
    - cancel_on_pause: true on resting orders
    - post_only: true default (Maker-First)
    """
    client = KalshiClient(signer=auth_signer)

    captured_payloads = []

    async def mock_request(method, path, params=None, json_data=None, authenticated=True):
        captured_payloads.append(json_data)
        return {
            "order": {
                "order_id": "mock-ord-123",
                "status": "resting",
            }
        }

    client.request = mock_request  # type: ignore

    # 1. Test BUY Order
    order_buy = await client.create_order(
        ticker="DEMO-TICKER",
        action=OrderAction.BUY,
        side=OrderSide.YES,
        count=10,
        yes_price=70,
    )
    assert captured_payloads[0]["cancel_on_pause"] is True
    assert captured_payloads[0]["post_only"] is True
    assert "sell_position_floor" not in captured_payloads[0]

    # 2. Test SELL Order: MUST contain sell_position_floor: 0
    order_sell = await client.create_order(
        ticker="DEMO-TICKER",
        action=OrderAction.SELL,
        side=OrderSide.YES,
        count=10,
        yes_price=90,
    )
    assert captured_payloads[1]["sell_position_floor"] == 0
    assert captured_payloads[1]["cancel_on_pause"] is True
    assert captured_payloads[1]["post_only"] is True


def test_simulator_matching_and_fee_law():
    sim = KalshiSimulator(initial_cash_cents=500_000)

    # 1. Place resting Maker BUY order
    order = sim.place_order(
        ticker="NFL-TEST",
        action=OrderAction.BUY,
        side=OrderSide.YES,
        count=20,
        yes_price=68,
        post_only=True,
    )
    assert order.status == OrderStatus.RESTING
    assert "NFL-TEST" not in sim.positions

    # 2. Market tick arrives where best bid reaches 68c -> Fills as Maker ($0.00 fee)
    tick = {"yes": [[68, 100]], "no": [[30, 100]]}
    book = KalshiOrderBookEngine.parse_kalshi_orderbook("NFL-TEST", tick)
    filled = sim.on_market_tick("NFL-TEST", book)

    assert len(filled) == 1
    assert filled[0].order_id == order.order_id
    assert filled[0].maker_fee_paid_cents == 0
    assert sim.positions["NFL-TEST"].contracts == 20
    assert sim.total_maker_fees_saved_cents > 0


def test_simulator_sell_position_floor_rejection():
    """Simulator strictly enforces sell_position_floor: 0 and rejects naked shorts."""
    sim = KalshiSimulator()

    with pytest.raises(ValueError) as exc_info:
        sim.place_order(
            ticker="NFL-NO-POSITION",
            action=OrderAction.SELL,
            side=OrderSide.YES,
            count=10,
            yes_price=85,
        )
    assert "sell_position_floor: 0" in str(exc_info.value)


@pytest.mark.asyncio
async def test_kalshi_ws_listener_dispatch():
    ws_client = KalshiWebSocketClient()
    received_events = []

    async def listener(msg_type, data):
        received_events.append((msg_type, data))

    ws_client.add_listener(listener)

    # Trigger subscriber callback directly
    await ws_client._subscribers[0]("orderbook_delta", {"ticker": "TEST-1", "yes": [70, 100]})
    assert len(received_events) == 1
    assert received_events[0][0] == "orderbook_delta"
    assert received_events[0][1]["ticker"] == "TEST-1"
