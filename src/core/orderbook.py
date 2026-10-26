"""Reciprocal Order Book Law & Depth Analysis for Kalshi Markets.

Non-Negotiable Kalshi Law:
Kalshi /markets/{ticker}/orderbook ONLY returns 'yes' bids and 'no' bids.
NEVER look for an 'asks' key.
best_yes_ask = 100 - best_no_bid
best_no_ask  = 100 - best_yes_bid
"""

import logging
from typing import Any, Optional
from src.core.models import OrderBookLevel, OrderSide, ReciprocalOrderBook

logger = logging.getLogger(__name__)


class KalshiOrderBookEngine:
    """Implements Kalshi Reciprocal Order Book Law and Maker-First liquidity calculations."""

    @staticmethod
    def parse_kalshi_orderbook(ticker: str, payload: dict[str, Any]) -> ReciprocalOrderBook:
        """
        Parses Kalshi orderbook response payload.
        Handles both:
        - {"orderbook": {"yes": [[p, q], ...], "no": [[p, q], ...]}}
        - {"yes": [[p, q], ...], "no": [[p, q], ...]}
        Guarantees asks key is NEVER accessed.
        """
        raw_ob = payload.get("orderbook", payload)

        if "asks" in raw_ob:
            logger.warning("Kalshi API payload contained unexpected 'asks' key; ignoring in accordance with Reciprocal Law.")

        yes_bids_raw = raw_ob.get("yes", [])
        no_bids_raw = raw_ob.get("no", [])

        yes_bids: list[OrderBookLevel] = []
        for item in yes_bids_raw:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                yes_bids.append(OrderBookLevel(price=int(item[0]), quantity=int(item[1])))
            elif isinstance(item, dict) and "price" in item and "quantity" in item:
                yes_bids.append(OrderBookLevel(price=int(item["price"]), quantity=int(item["quantity"])))

        no_bids: list[OrderBookLevel] = []
        for item in no_bids_raw:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                no_bids.append(OrderBookLevel(price=int(item[0]), quantity=int(item[1])))
            elif isinstance(item, dict) and "price" in item and "quantity" in item:
                no_bids.append(OrderBookLevel(price=int(item["price"]), quantity=int(item["quantity"])))

        # Sort bids descending by price (highest bid first)
        yes_bids.sort(key=lambda x: x.price, reverse=True)
        no_bids.sort(key=lambda x: x.price, reverse=True)

        return ReciprocalOrderBook(
            ticker=ticker,
            timestamp=int(payload.get("timestamp", 0) or 0),
            yes_bids=yes_bids,
            no_bids=no_bids,
        )

    @classmethod
    def get_market_depth(cls, book: ReciprocalOrderBook) -> dict[str, Any]:
        """Calculates derived metrics from reciprocal order book."""
        yes_bids = book.yes_bids
        yes_asks = book.get_synthesized_yes_asks()

        best_yes_bid = book.best_yes_bid
        best_yes_ask = book.best_yes_ask
        best_no_bid = book.best_no_bid
        best_no_ask = book.best_no_ask

        # Depth at top of book
        top_yes_bid_qty = yes_bids[0].quantity if yes_bids else 0
        top_yes_ask_qty = yes_asks[0].quantity if yes_asks else 0

        # Micro-price calculation (imbalance-weighted midpoint)
        microprice: Optional[float] = None
        if best_yes_bid is not None and best_yes_ask is not None:
            total_qty = top_yes_bid_qty + top_yes_ask_qty
            if total_qty > 0:
                microprice = (top_yes_bid_qty * best_yes_ask + top_yes_ask_qty * best_yes_bid) / total_qty
            else:
                microprice = (best_yes_bid + best_yes_ask) / 2.0

        return {
            "ticker": book.ticker,
            "best_yes_bid": best_yes_bid,
            "best_yes_ask": best_yes_ask,
            "best_no_bid": best_no_bid,
            "best_no_ask": best_no_ask,
            "yes_spread": book.yes_spread,
            "no_spread": book.no_spread,
            "yes_midpoint": book.yes_midpoint,
            "yes_microprice": round(microprice, 2) if microprice is not None else None,
            "top_yes_bid_qty": top_yes_bid_qty,
            "top_yes_ask_qty": top_yes_ask_qty,
            "total_yes_bid_depth": sum(l.quantity for l in yes_bids),
            "total_yes_ask_depth": sum(l.quantity for l in yes_asks),
        }

    @classmethod
    def is_maker_order(
        cls,
        book: ReciprocalOrderBook,
        side: OrderSide,
        action: str,  # "buy" or "sell"
        limit_price: int,
    ) -> bool:
        """
        Enforces Maker-First Law:
        Verifies that placing an order at limit_price rests on the book rather than crossing the spread.
        Crossing the spread incurs Kalshi taker fee: ceil(0.07 * contracts * P * (1-P)).

        - BUY YES: Must be < best_yes_ask to rest. If >= best_yes_ask, it crosses and pays taker fee!
        - SELL YES: Must be > best_yes_bid to rest. If <= best_yes_bid, it crosses and pays taker fee!
        - BUY NO: Must be < best_no_ask to rest.
        - SELL NO: Must be > best_no_bid to rest.
        """
        if side == OrderSide.YES:
            if action.lower() == "buy":
                best_ask = book.best_yes_ask
                if best_ask is None:
                    return True
                return limit_price < best_ask
            else:  # sell
                best_bid = book.best_yes_bid
                if best_bid is None:
                    return True
                return limit_price > best_bid
        else:  # NO
            if action.lower() == "buy":
                best_ask = book.best_no_ask
                if best_ask is None:
                    return True
                return limit_price < best_ask
            else:  # sell
                best_bid = book.best_no_bid
                if best_bid is None:
                    return True
                return limit_price > best_bid

    @classmethod
    def suggest_maker_entry_price(
        cls,
        book: ReciprocalOrderBook,
        side: OrderSide,
        offset_cents: int = 2,
    ) -> Optional[int]:
        """
        Calculates resting maker entry price to catch dip fills:
        For BUY YES: places maker bid at (best_yes_bid - offset) or (best_yes_ask - offset).
        Guaranteed to be strictly less than best_yes_ask.
        """
        if side == OrderSide.YES:
            best_bid = book.best_yes_bid
            best_ask = book.best_yes_ask
            if best_bid is not None:
                suggested = best_bid - offset_cents
            elif best_ask is not None:
                suggested = best_ask - offset_cents - 1
            else:
                return None
            if best_ask is not None and suggested >= best_ask:
                suggested = best_ask - 1
            return max(1, min(99, suggested))
        else:
            best_bid = book.best_no_bid
            best_ask = book.best_no_ask
            if best_bid is not None:
                suggested = best_bid - offset_cents
            elif best_ask is not None:
                suggested = best_ask - offset_cents - 1
            else:
                return None
            if best_ask is not None and suggested >= best_ask:
                suggested = best_ask - 1
            return max(1, min(99, suggested))
