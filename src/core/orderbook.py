"""Reciprocal Order Book Law, Fixed-Point Parsing & Whale Tape Imbalance Engine.

Non-Negotiable Kalshi Laws:
- LAW 1: Kalshi /markets/{ticker}/orderbook returns ONLY 'yes' bids and 'no' bids.
  Never look for an 'asks' array.
  best_yes_ask = 1.00 - best_no_bid  (or 100 - best_no_bid in cents)
  best_no_ask  = 1.00 - best_yes_bid (or 100 - best_yes_bid in cents)
- LAW 2: Fixed-Point Dollar Strings: supports orderbook_fp with yes_dollars and no_dollars.
- LAW 3: Whale Tape Reader: 10:1 order book dollar imbalance detector.
"""

from decimal import Decimal
import logging
from typing import Any, Optional
from src.core.models import OrderBookLevel, OrderSide, ReciprocalOrderBook

logger = logging.getLogger(__name__)


class KalshiOrderBookEngine:
    """Implements Kalshi Reciprocal Order Book Law, Fixed-Point string math, and Whale Tape reading."""

    @staticmethod
    def parse_kalshi_orderbook(ticker: str, payload: dict[str, Any]) -> ReciprocalOrderBook:
        """
        Parses Kalshi orderbook response payload.
        Supports both:
        1. Fixed-point dollar format (orderbook_fp):
           yes_dollars: [["0.7200", "100.00"], ...]
           no_dollars:  [["0.2800", "100.00"], ...]
        2. Legacy cent integer format:
           yes: [[72, 100], ...]
           no:  [[28, 100], ...]
        Guarantees asks key is NEVER accessed.
        """
        raw_ob = payload.get("orderbook_fp") or payload.get("orderbook") or payload

        if "asks" in raw_ob:
            logger.warning("Payload contained unexpected 'asks' key; ignoring per Reciprocal Law.")

        yes_bids_raw = raw_ob.get("yes_dollars") or raw_ob.get("yes", [])
        no_bids_raw = raw_ob.get("no_dollars") or raw_ob.get("no", [])

        yes_bids = KalshiOrderBookEngine._parse_level_list(yes_bids_raw)
        no_bids = KalshiOrderBookEngine._parse_level_list(no_bids_raw)

        # Sort bids descending by price (highest bid first)
        yes_bids.sort(key=lambda x: x.price, reverse=True)
        no_bids.sort(key=lambda x: x.price, reverse=True)

        return ReciprocalOrderBook(
            ticker=ticker,
            timestamp=int(payload.get("timestamp", 0) or 0),
            yes_bids=yes_bids,
            no_bids=no_bids,
        )

    @staticmethod
    def _parse_level_list(raw_levels: list[Any]) -> list[OrderBookLevel]:
        """Parses list of price/quantity pairs supporting both float/string dollars and integer cents."""
        levels = []
        for item in raw_levels:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                p_raw, q_raw = item[0], item[1]
                p_cents = KalshiOrderBookEngine._to_cents(p_raw)
                q_int = int(float(q_raw))
                if 1 <= p_cents <= 99 and q_int > 0:
                    levels.append(OrderBookLevel(price=p_cents, quantity=q_int))
            elif isinstance(item, dict) and "price" in item and "quantity" in item:
                p_cents = KalshiOrderBookEngine._to_cents(item["price"])
                q_int = int(float(item["quantity"]))
                if 1 <= p_cents <= 99 and q_int > 0:
                    levels.append(OrderBookLevel(price=p_cents, quantity=q_int))
        return levels

    @staticmethod
    def _to_cents(val: Any) -> int:
        """Converts dollar string '0.7200', float 0.72, or integer cents 72 to integer cents."""
        if isinstance(val, str):
            f = float(val)
            return int(round(f * 100)) if f <= 1.0 else int(round(f))
        elif isinstance(val, float):
            return int(round(val * 100)) if val <= 1.0 else int(round(val))
        return int(val)

    @classmethod
    def get_market_depth(cls, book: ReciprocalOrderBook) -> dict[str, Any]:
        """Calculates derived metrics from reciprocal order book."""
        yes_bids = book.yes_bids
        yes_asks = book.get_synthesized_yes_asks()

        best_yes_bid = book.best_yes_bid
        best_yes_ask = book.best_yes_ask
        best_no_bid = book.best_no_bid
        best_no_ask = book.best_no_ask

        top_yes_bid_qty = yes_bids[0].quantity if yes_bids else 0
        top_yes_ask_qty = yes_asks[0].quantity if yes_asks else 0

        microprice: Optional[float] = None
        if best_yes_bid is not None and best_yes_ask is not None:
            total_qty = top_yes_bid_qty + top_yes_ask_qty
            if total_qty > 0:
                microprice = (top_yes_bid_qty * best_yes_ask + top_yes_ask_qty * best_yes_bid) / total_qty
            else:
                microprice = (best_yes_bid + best_yes_ask) / 2.0

        # Dollar depth for Whale Tape Reader
        bid_depth_dollars = sum(l.price * l.quantity for l in yes_bids) / 100.0
        ask_depth_dollars = sum(l.price * l.quantity for l in yes_asks) / 100.0

        return {
            "ticker": book.ticker,
            "best_yes_bid": best_yes_bid,
            "best_yes_ask": best_yes_ask,
            "best_no_bid": best_no_bid,
            "best_no_ask": best_no_ask,
            "best_yes_bid_dollars": f"{best_yes_bid / 100.0:.4f}" if best_yes_bid else None,
            "best_yes_ask_dollars": f"{best_yes_ask / 100.0:.4f}" if best_yes_ask else None,
            "yes_spread": book.yes_spread,
            "no_spread": book.no_spread,
            "yes_midpoint": book.yes_midpoint,
            "yes_microprice": round(microprice, 2) if microprice is not None else None,
            "top_yes_bid_qty": top_yes_bid_qty,
            "top_yes_ask_qty": top_yes_ask_qty,
            "bid_depth_dollars": round(bid_depth_dollars, 2),
            "ask_depth_dollars": round(ask_depth_dollars, 2),
        }

    @classmethod
    def check_whale_tape_imbalance(
        cls,
        book: ReciprocalOrderBook,
        threshold_ratio: float = 10.0,
    ) -> dict[str, Any]:
        """
        Whale Tape Reader:
        Triggers alert when bid vs ask dollar depth exceeds 10:1 ratio.
        Identifies heavy institutional buying or selling pressure.
        """
        yes_bids = book.yes_bids
        yes_asks = book.get_synthesized_yes_asks()

        bid_depth_dollars = sum((l.price / 100.0) * l.quantity for l in yes_bids)
        ask_depth_dollars = sum((l.price / 100.0) * l.quantity for l in yes_asks)

        has_whale_buy = False
        has_whale_sell = False
        imbalance_ratio = 1.0

        if ask_depth_dollars > 0 and bid_depth_dollars / ask_depth_dollars >= threshold_ratio:
            has_whale_buy = True
            imbalance_ratio = round(bid_depth_dollars / ask_depth_dollars, 2)
        elif bid_depth_dollars > 0 and ask_depth_dollars / bid_depth_dollars >= threshold_ratio:
            has_whale_sell = True
            imbalance_ratio = round(ask_depth_dollars / bid_depth_dollars, 2)

        return {
            "ticker": book.ticker,
            "has_whale_alert": has_whale_buy or has_whale_sell,
            "whale_direction": "BUY (Heavy Bid)" if has_whale_buy else ("SELL (Heavy Ask)" if has_whale_sell else "BALANCED"),
            "imbalance_ratio": imbalance_ratio,
            "bid_depth_dollars": round(bid_depth_dollars, 2),
            "ask_depth_dollars": round(ask_depth_dollars, 2),
            "threshold_ratio": threshold_ratio,
        }

    @classmethod
    def is_maker_order(
        cls,
        book: ReciprocalOrderBook,
        side: OrderSide,
        action: str,
        limit_price: int,
    ) -> bool:
        """
        Enforces Maker-First Law:
        Verifies that placing an order at limit_price rests on the book rather than crossing the spread.
        """
        if side == OrderSide.YES:
            if action.lower() == "buy":
                best_ask = book.best_yes_ask
                return best_ask is None or limit_price < best_ask
            else:
                best_bid = book.best_yes_bid
                return best_bid is None or limit_price > best_bid
        else:
            if action.lower() == "buy":
                best_ask = book.best_no_ask
                return best_ask is None or limit_price < best_ask
            else:
                best_bid = book.best_no_bid
                return best_bid is None or limit_price > best_bid

    @classmethod
    def suggest_maker_entry_price(
        cls,
        book: ReciprocalOrderBook,
        side: OrderSide,
        offset_cents: int = 2,
    ) -> Optional[int]:
        """Calculates resting maker entry price to catch dip fills inside spread."""
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
