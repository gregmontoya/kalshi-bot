"""Tests for Reciprocal Order Book Law and Maker-First liquidity calculations."""

import pytest
from engine.core.models import OrderSide
from engine.core.orderbook import KalshiOrderBookEngine


def test_reciprocal_order_book_law():
    """
    Kalshi Law:
    Kalshi orderbook ONLY returns 'yes' bids and 'no' bids.
    best_yes_ask = 100 - best_no_bid
    best_no_ask  = 100 - best_yes_bid
    """
    # Simulate Kalshi response with yes and no bids
    payload = {
        "orderbook": {
            "yes": [
                [72, 150],  # best yes bid: 72c, 150 contracts
                [71, 300],
                [70, 500],
            ],
            "no": [
                [25, 200],  # best no bid: 25c, 200 contracts
                [24, 400],
                [22, 100],
            ],
        },
        "timestamp": 1714521600000,
    }

    book = KalshiOrderBookEngine.parse_kalshi_orderbook("TEST-TICKER", payload)

    # 1. Best bids
    assert book.best_yes_bid == 72
    assert book.best_no_bid == 25

    # 2. Reciprocal synthetic asks
    # best_yes_ask = 100 - best_no_bid = 100 - 25 = 75c
    assert book.best_yes_ask == 75
    # best_no_ask = 100 - best_yes_bid = 100 - 72 = 28c
    assert book.best_no_ask == 28

    # 3. Spreads
    # YES spread = 75 - 72 = 3c
    assert book.yes_spread == 3
    # NO spread = 28 - 25 = 3c
    assert book.no_spread == 3

    # 4. Midpoint
    # (72 + 75) / 2 = 73.5
    assert book.yes_midpoint == 73.5


def test_synthesized_ask_ladders():
    payload = {
        "yes": [[60, 50], [58, 100]],
        "no": [[35, 80], [30, 120]],
    }
    book = KalshiOrderBookEngine.parse_kalshi_orderbook("DEMO-NFL", payload)

    yes_asks = book.get_synthesized_yes_asks()
    # no bids: 35c (qty 80) -> yes ask at 100 - 35 = 65c (qty 80)
    # no bids: 30c (qty 120) -> yes ask at 100 - 30 = 70c (qty 120)
    assert len(yes_asks) == 2
    assert yes_asks[0].price == 65
    assert yes_asks[0].quantity == 80
    assert yes_asks[1].price == 70
    assert yes_asks[1].quantity == 120

    no_asks = book.get_synthesized_no_asks()
    # yes bids: 60c (qty 50) -> no ask at 100 - 60 = 40c (qty 50)
    # yes bids: 58c (qty 100) -> no ask at 100 - 58 = 42c (qty 100)
    assert len(no_asks) == 2
    assert no_asks[0].price == 40
    assert no_asks[0].quantity == 50
    assert no_asks[1].price == 42
    assert no_asks[1].quantity == 100


def test_maker_first_order_crossing_check():
    payload = {
        "yes": [[70, 100]],
        "no": [[25, 100]],  # Synthesized YES ask is 75c
    }
    book = KalshiOrderBookEngine.parse_kalshi_orderbook("DEMO-NFL", payload)

    # BUY YES:
    # 72c is < 75c (best_yes_ask) -> RESTS as Maker (fee = $0.00)
    assert KalshiOrderBookEngine.is_maker_order(book, OrderSide.YES, "buy", 72) is True
    # 74c is < 75c -> RESTS as Maker
    assert KalshiOrderBookEngine.is_maker_order(book, OrderSide.YES, "buy", 74) is True
    # 75c equals best_yes_ask -> CROSSES book, becomes Taker (violates Maker-First law!)
    assert KalshiOrderBookEngine.is_maker_order(book, OrderSide.YES, "buy", 75) is False
    # 76c > 75c -> CROSSES book
    assert KalshiOrderBookEngine.is_maker_order(book, OrderSide.YES, "buy", 76) is False

    # SELL YES:
    # 72c is > 70c (best_yes_bid) -> RESTS as Maker
    assert KalshiOrderBookEngine.is_maker_order(book, OrderSide.YES, "sell", 72) is True
    # 70c <= best_yes_bid -> CROSSES book, becomes Taker
    assert KalshiOrderBookEngine.is_maker_order(book, OrderSide.YES, "sell", 70) is False


def test_suggest_maker_entry_price():
    payload = {
        "yes": [[72, 100]],
        "no": [[25, 100]],  # best_yes_ask is 75c
    }
    book = KalshiOrderBookEngine.parse_kalshi_orderbook("DEMO-NFL", payload)

    # 2c dip fill below best bid: 72 - 2 = 70c
    suggested = KalshiOrderBookEngine.suggest_maker_entry_price(book, OrderSide.YES, offset_cents=2)
    assert suggested == 70
    assert KalshiOrderBookEngine.is_maker_order(book, OrderSide.YES, "buy", suggested) is True
