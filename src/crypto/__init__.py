"""Pydantic domain models for the Standalone Crypto Quant Engine.

100% isolated from Kalshi Sports models and bankroll.
Enforces:
- LAW 1: Real-time L2 OrderBook Imbalance & Micro-Price (Stoikov)
- LAW 2: Maker-First (`POST_ONLY`) Execution with fee/rebate math
- LAW 3: Mandatory `reduce_only: True` on all exits (never accidentally flip short/long)
- LAW 4: Automatic "Green-Out" partial scale-out + breakeven-plus stop-loss
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class CryptoVenue(str, Enum):
    HYPERLIQUID = "HYPERLIQUID"
    KRAKEN_PRO = "KRAKEN_PRO"
    COINBASE = "COINBASE"


class CryptoOrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class CryptoOrderType(str, Enum):
    POST_ONLY_LIMIT = "post_only_limit"
    IOC_LIMIT = "ioc_limit"
    MARKET_EMERGENCY = "market_emergency"


class CryptoOrderStatus(str, Enum):
    RESTING = "resting"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELED = "canceled"


class CryptoPodType(str, Enum):
    POD1_MOMENTUM_BASKET = "crypto_momentum_basket"
    POD2_WICK_GREEN_OUT = "wick_value_green_out"
    POD3_FUNDING_TAIL_LOCK = "funding_tail_lock"


class L2BookLevel(BaseModel):
    price: float
    size: float

    @property
    def notional_usd(self) -> float:
        return self.price * self.size


class CryptoL2OrderBook(BaseModel):
    """Multi-exchange L2 Order Book with Stoikov micro-price & whale imbalance math."""

    symbol: str  # e.g., "BTC", "ETH", "SOL", "SUI"
    venue: CryptoVenue = CryptoVenue.HYPERLIQUID
    timestamp_ms: int = Field(default_factory=lambda: int(datetime.now(timezone.utc).timestamp() * 1000))
    bids: list[L2BookLevel] = Field(default_factory=list)
    asks: list[L2BookLevel] = Field(default_factory=list)

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid_price(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2.0
        return None

    @property
    def spread_bps(self) -> float:
        mid = self.mid_price
        if mid and self.best_bid and self.best_ask and mid > 0:
            return ((self.best_ask - self.best_bid) / mid) * 10000.0
        return 0.0

    @property
    def micro_price(self) -> Optional[float]:
        """Stoikov volume-weighted micro-price predicting short-term tick direction."""
        if not self.bids or not self.asks:
            return self.mid_price
        bb_p, bb_q = self.bids[0].price, self.bids[0].size
        ba_p, ba_q = self.asks[0].price, self.asks[0].size
        total_q = bb_q + ba_q
        if total_q <= 0:
            return self.mid_price
        return (bb_p * ba_q + ba_p * bb_q) / total_q

    @property
    def bid_depth_usd(self) -> float:
        return sum(level.notional_usd for level in self.bids[:10])

    @property
    def ask_depth_usd(self) -> float:
        return sum(level.notional_usd for level in self.asks[:10])

    @property
    def whale_imbalance_ratio(self) -> float:
        """Ratio of top-10 bid notional to ask notional."""
        ask_usd = self.ask_depth_usd
        bid_usd = self.bid_depth_usd
        if ask_usd <= 0:
            return 10.0 if bid_usd > 0 else 1.0
        return round(bid_usd / ask_usd, 2)


class InstitutionalTruthSignal(BaseModel):
    """Equivalent to de-vigged Pinnacle/Circa odds: Cross-exchange institutional flow & basis."""

    symbol: str
    coinbase_spot_price: float
    kraken_spot_price: float
    hyperliquid_perp_price: float
    vwap_15m: float
    cvd_score: float = Field(ge=0.0, le=1.0, description="0..1 Institutional Cumulative Volume Delta score")
    funding_rate_hourly_bps: float = Field(description="Hourly perpetual funding rate in bps")
    annualized_funding_apy: float = Field(description="Annualized funding yield in %")
    perp_basis_bps: float = Field(description="Perp vs Spot premium/discount in bps")
    whale_imbalance_ratio: float = Field(default=1.0)
    realized_vol_15m_pct: float = Field(default=0.8)


class CryptoOrder(BaseModel):
    order_id: str
    symbol: str
    venue: CryptoVenue
    side: CryptoOrderSide
    order_type: CryptoOrderType = CryptoOrderType.POST_ONLY_LIMIT
    price: float
    quantity: float
    notional_usd: float
    post_only: bool = True
    reduce_only: bool = False
    cancel_on_vol_spike: bool = True
    fee_usd: float = 0.0
    taker_fee_saved_usd: float = 0.0
    status: CryptoOrderStatus = CryptoOrderStatus.RESTING
    pod_type: CryptoPodType
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class CryptoBasketLeg(BaseModel):
    symbol: str
    venue: CryptoVenue = CryptoVenue.HYPERLIQUID
    entry_price: float
    current_price: float
    take_profit_price: float
    guillotine_price: float
    quantity: float
    notional_usd: float
    cvd_score_at_entry: float
    live_cvd_score: float
    whale_imbalance: float
    status: str = "OPEN"  # OPEN, TP_FILLED, GUILLOTINED, GREENED_OUT
    pnl_usd: float = 0.0
    pnl_pct: float = 0.0
    taker_fee_saved_usd: float = 0.0


class CryptoMomentumBasket(BaseModel):
    basket_id: str
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    legs: list[CryptoBasketLeg]
    status: str = "ACTIVE"
    total_notional_usd: float = 0.0
    total_pnl_usd: float = 0.0


class WickGreenOutPosition(BaseModel):
    position_id: str
    symbol: str
    venue: CryptoVenue = CryptoVenue.HYPERLIQUID
    entry_price: float
    current_price: float
    vwap_anchor: float
    initial_quantity: float
    remaining_quantity: float
    notional_usd: float
    green_out_target_price: float
    runner_take_profit_price: float
    stop_loss_price: float
    is_greened_out: bool = False
    locked_profit_usd: float = 0.0
    unrealized_pnl_usd: float = 0.0
    total_pnl_usd: float = 0.0
    status: str = "RESTING_OR_OPEN"
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class FundingTailLockPair(BaseModel):
    pair_id: str
    symbol: str
    spot_venue: CryptoVenue = CryptoVenue.KRAKEN_PRO
    perp_venue: CryptoVenue = CryptoVenue.HYPERLIQUID
    spot_entry_price: float
    perp_entry_price: float
    current_spot_price: float
    current_perp_price: float
    quantity: float
    total_notional_usd: float
    entry_basis_bps: float
    current_basis_bps: float
    annualized_funding_apy: float
    accrued_funding_usd: float = 0.0
    basis_pnl_usd: float = 0.0
    net_delta_usd: float = 0.0  # Always ~0.00 (Delta-Neutral)
    status: str = "LOCKED_HARVESTING"
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
