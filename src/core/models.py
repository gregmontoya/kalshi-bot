"""Pydantic domain models for Phase 1 Core."""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class OrderAction(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderSide(str, Enum):
    YES = "yes"
    NO = "no"


class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"


class OrderStatus(str, Enum):
    PENDING = "pending"
    RESTING = "resting"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELED = "canceled"
    REJECTED = "rejected"


class OrderBookLevel(BaseModel):
    price: int = Field(ge=1, le=99, description="Price in cents (1-99)")
    quantity: int = Field(ge=0, description="Resting contracts")


class ReciprocalOrderBook(BaseModel):
    """
    Kalshi Reciprocal Order Book Law:
    Kalshi /markets/{ticker}/orderbook ONLY returns yes bids and no bids.
    Never look for an 'asks' key.
    best_yes_ask = 100 - best_no_bid
    best_no_ask  = 100 - best_yes_bid
    """
    ticker: str
    timestamp: int = Field(default=0, description="Timestamp in milliseconds")
    yes_bids: list[OrderBookLevel] = Field(default_factory=list)
    no_bids: list[OrderBookLevel] = Field(default_factory=list)

    @property
    def best_yes_bid(self) -> Optional[int]:
        if not self.yes_bids:
            return None
        return max(level.price for level in self.yes_bids)

    @property
    def best_no_bid(self) -> Optional[int]:
        if not self.no_bids:
            return None
        return max(level.price for level in self.no_bids)

    @property
    def best_yes_ask(self) -> Optional[int]:
        """Synthesized YES ask: 100 - best NO bid."""
        bnb = self.best_no_bid
        return (100 - bnb) if bnb is not None else None

    @property
    def best_no_ask(self) -> Optional[int]:
        """Synthesized NO ask: 100 - best YES bid."""
        byb = self.best_yes_bid
        return (100 - byb) if byb is not None else None

    @property
    def yes_spread(self) -> Optional[int]:
        byb = self.best_yes_bid
        bya = self.best_yes_ask
        if byb is not None and bya is not None:
            return bya - byb
        return None

    @property
    def no_spread(self) -> Optional[int]:
        bnb = self.best_no_bid
        bna = self.best_no_ask
        if bnb is not None and bna is not None:
            return bna - bnb
        return None

    @property
    def yes_midpoint(self) -> Optional[float]:
        byb = self.best_yes_bid
        bya = self.best_yes_ask
        if byb is not None and bya is not None:
            return (byb + bya) / 2.0
        return None

    def get_synthesized_yes_asks(self) -> list[OrderBookLevel]:
        """
        Synthesizes YES asks ladder from NO bids.
        NO bid at price P with quantity Q is equivalent to an offer to sell YES at (100 - P) with quantity Q.
        Sorted by price ascending (cheapest ask first).
        """
        asks = [
            OrderBookLevel(price=100 - level.price, quantity=level.quantity)
            for level in self.no_bids
        ]
        return sorted(asks, key=lambda x: x.price)

    def get_synthesized_no_asks(self) -> list[OrderBookLevel]:
        """
        Synthesizes NO asks ladder from YES bids.
        YES bid at price P with quantity Q is equivalent to an offer to sell NO at (100 - P) with quantity Q.
        Sorted by price ascending (cheapest ask first).
        """
        asks = [
            OrderBookLevel(price=100 - level.price, quantity=level.quantity)
            for level in self.yes_bids
        ]
        return sorted(asks, key=lambda x: x.price)


class Order(BaseModel):
    order_id: str
    client_order_id: str
    ticker: str
    action: OrderAction
    side: OrderSide
    count: int
    yes_price: int = Field(ge=1, le=99, description="YES price in cents (1-99)")
    no_price: int = Field(ge=1, le=99, description="NO price in cents (100 - yes_price)")
    type: OrderType = OrderType.LIMIT
    post_only: bool = Field(default=True, description="Enforce Maker-First resting order")
    sell_position_floor: int = Field(default=0, description="Mandatory: prevents accidental shorting")
    cancel_on_pause: bool = Field(default=True, description="Mandatory: auto-cancels if market pauses on scoring play")
    status: OrderStatus = OrderStatus.PENDING
    filled_count: int = 0
    remaining_count: int = 0
    taker_fee_paid_cents: int = 0
    maker_fee_paid_cents: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
