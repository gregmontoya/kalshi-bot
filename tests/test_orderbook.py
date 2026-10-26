"""Unit tests for Phase 1 Reciprocal Order Book Law, Fixed-Point Parsing & Ask Synthesis."""

import pytest
from src.core.models import OrderSide
from src.core.orderbook import KalshiOrderBookEngine


def test_reciprocal_order_book_law():
    """
    Kalshi Law:
    Kalshi orderbook ONLY returns 'yes' bids and 'no' bids.
    best_yes_ask = 100 - best_no_bid
    best_no_ask  = 100 - best_yes_bid
    """
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
    assert book.best_yes_ask == 75  # 100 - 25
    assert book.best_no_ask == 28   # 100 - 72

    # 3. Spreads
    assert book.yes_spread == 3  # 75 - 72
    assert book.no_spread == 3   # 28 - 25

    # 4. Midpoint
    assert book.yes_midpoint == 73.5


def test_fixed_point_dollar_strings_orderbook_fp():
    """
    LAW 2: Fixed-Point Dollar Strings (API Format)
    Kalshi v2 orderbook_fp uses yes_dollars and no_dollars strings like "0.7200".
    """
    payload_fp = {
        "orderbook_fp": {
            "yes_dollars": [
                ["0.7400", "50.00"],
                ["0.7200", "100.00"],
            ],
            "no_dollars": [
                ["0.2300", "80.00"],
                ["0.2000", "120.00"],
            ],
        },
        "timestamp": 1714521600000,
    }

    book = KalshiOrderBookEngine.parse_kalshi_orderbook("FP-TICKER", payload_fp)

    assert book.best_yes_bid == 74
    assert book.best_no_bid == 23
    # Reciprocal: 1.00 - 0.23 = 0.77 (77c)
    assert book.best_yes_ask == 77
    assert book.best_no_ask == 26

    depth = KalshiOrderBookEngine.get_market_depth(book)
    assert depth["best_yes_bid_dollars"] == "0.7400"
    assert depth["best_yes_ask_dollars"] == "0.7700"


def test_synthesized_ask_ladders():
    payload = {
        "yes": [[60, 50], [58, 100]],
        "no": [[35, 80], [30, 120]],
    }
    book = KalshiOrderBookEngine.parse_kalshi_orderbook("DEMO-NFL", payload)

    yes_asks = book.get_synthesized_yes_asks()
    assert len(yes_asks) == 2
    assert yes_asks[0].price == 65
    assert yes_asks[0].quantity == 80
    assert yes_asks[1].price == 70
    assert yes_asks[1].quantity == 120

    no_asks = book.get_synthesized_no_asks()
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
    assert KalshiOrderBookEngine.is_maker_order(book, OrderSide.YES, "buy", 72) is True
    assert KalshiOrderBookEngine.is_maker_order(book, OrderSide.YES, "buy", 74) is True
    assert KalshiOrderBookEngine.is_maker_order(book, OrderSide.YES, "buy", 75) is False
    assert KalshiOrderBookEngine.is_maker_order(book, OrderSide.YES, "buy", 76) is False

    # SELL YES:
    assert KalshiOrderBookEngine.is_maker_order(book, OrderSide.YES, "sell", 72) is True
    assert KalshiOrderBookEngine.is_maker_order(book, OrderSide.YES, "sell", 70) is False


def test_suggest_maker_entry_price():
    payload = {
        "yes": [[72, 100]],
        "no": [[25, 100]],  # best_yes_ask is 75c
    }
    book = KalshiOrderBookEngine.parse_kalshi_orderbook("DEMO-NFL", payload)

    suggested = KalshiOrderBookEngine.suggest_maker_entry_price(book, OrderSide.YES, offset_cents=2)
    assert suggested == 70
    assert KalshiOrderBookEngine.is_maker_order(book, OrderSide.YES, "buy", suggested) is True
