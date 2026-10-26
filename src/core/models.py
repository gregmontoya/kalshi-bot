"""Pydantic domain models for Core, Exchange, Feeds, and Strategy Pods."""

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


class StrategyPodType(str, Enum):
    POD1_MOMENTUM_BASKET = "maker_momentum_basket"
    POD2_VALUE_GREEN_OUT = "sharp_value_green_out"
    POD3_TAIL_LOCK = "tail_lock_harvester"


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
    strategy_pod: Optional[str] = None
    basket_id: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Market(BaseModel):
    ticker: str
    title: str
    subtitle: Optional[str] = None
    category: str = "SPORTS"
    status: str = "open"
    yes_bid: Optional[int] = None
    yes_ask: Optional[int] = None
    no_bid: Optional[int] = None
    no_ask: Optional[int] = None
    last_price: Optional[int] = None
    volume: int = 0
    open_interest: int = 0
    expiration_time: Optional[str] = None


class Position(BaseModel):
    ticker: str
    side: OrderSide
    contracts: int
    avg_price_cents: float
    current_bid_cents: Optional[int] = None
    current_ask_cents: Optional[int] = None
    unrealized_pnl_cents: int = 0
    realized_pnl_cents: int = 0
    strategy_pod: Optional[str] = None
    basket_id: Optional[str] = None


class BasketLeg(BaseModel):
    ticker: str
    market_title: str
    target_entry_price: int
    actual_entry_price: Optional[int] = None
    contracts: int
    status: str = "pending_maker_bid"
    entry_order_id: Optional[str] = None
    tp_order_id: Optional[str] = None
    tp_price: int = 90
    current_price: Optional[int] = None
    current_sharp_prob: Optional[float] = None
    pnl_cents: int = 0
    guillotine_triggered: bool = False
    exit_price: Optional[int] = None


class Basket(BaseModel):
    basket_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = "active"
    legs: list[BasketLeg] = Field(default_factory=list)
    total_invested_cents: int = 0
    total_realized_pnl_cents: int = 0
    total_unrealized_pnl_cents: int = 0


class SharpOdds(BaseModel):
    event_id: str
    sport: str
    team_home: str
    team_away: str
    bookmaker: str
    home_raw_odds: float
    away_raw_odds: float
    home_devigged_prob: float
    away_devigged_prob: float
    devig_method: str = "shin"
    last_updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class GameState(BaseModel):
    event_id: str
    sport: str = "football"
    home_team: str
    away_team: str
    period: int = 1
    clock_display: str = "15:00"
    seconds_remaining: int = 3600
    home_score: int = 0
    away_score: int = 0
    score_margin: int = 0
    possession_team: Optional[str] = None
    down: Optional[int] = None
    distance: Optional[int] = None
    home_timeouts: int = 3
    away_timeouts: int = 3
    win_probability_home: float = 0.50
    is_fourth_quarter: bool = False
    is_final: bool = False


class StadiumWeather(BaseModel):
    venue_name: str
    latitude: float
    longitude: float
    temperature_f: float
    wind_speed_mph: float
    wind_gusts_mph: float
    precipitation_prob: float
    is_dome: bool = False
    impact_note: Optional[str] = None
