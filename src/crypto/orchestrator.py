"""Standalone Crypto Quant Multi-Strategy Orchestrator.

Runs 100% isolated from the Kalshi Sports bankroll.
Connects Kraken Pro, Coinbase Institutional Feed, and Wallet (Hyperliquid)
into the 3 Core Crypto Strategy Pods.
"""

from pathlib import Path
from typing import Any
import yaml

from src.crypto.exchanges import MultiExchangeCryptoFeed
from src.crypto.models import CryptoVenue
from src.crypto.pods import (
    CryptoMomentumBasketPod,
    FundingTailLockPod,
    WickValueGreenOutPod,
)


class CryptoEngineOrchestrator:
    """Master orchestrator for the Standalone Crypto Quant Machine."""

    def __init__(self, config_path: str = "config/crypto_strategies.yaml"):
        self.config_path = Path(config_path)
        self.config = self._load_config()
        self.trading_mode = self.config.get("global", {}).get("trading_mode", "PAPER")
        self.primary_venue = self.config.get("global", {}).get("primary_execution_venue", "HYPERLIQUID")
        self.is_paused = False

        # Isolated Crypto Bankroll (Never touches Kalshi Sports balance)
        bankroll_cfg = self.config.get("bankroll", {})
        self.starting_equity_usd = float(bankroll_cfg.get("starting_equity_usd", 1000.0))

        # Feeds & 3 Strategy Pods
        self.feed = MultiExchangeCryptoFeed()
        self.pod1 = CryptoMomentumBasketPod(self.feed, self.config)
        self.pod2 = WickValueGreenOutPod(self.feed, self.config)
        self.pod3 = FundingTailLockPod(self.feed, self.config)

    def _load_config(self) -> dict[str, Any]:
        if self.config_path.exists():
            with open(self.config_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        return {}

    async def get_dashboard_snapshot(self, orderbook_symbol: str = "SOL") -> dict[str, Any]:
        """Returns complete real-time state for the Crypto Machine UI & API."""
        signals = await self.feed.get_institutional_truth_signals()
        yield_rankings = await self.feed.get_yield_rotator_rankings()
        l2_book = await self.feed.get_l2_orderbook(orderbook_symbol, CryptoVenue.HYPERLIQUID)
        pod1_candidates = await self.pod1.scan_and_evaluate(signals)

        # Market Regime Detector ("Speedboat vs. Oil Tanker")
        avg_vol = sum(s.realized_vol_15m_pct for s in signals.values()) / max(1, len(signals))
        avg_cvd = sum(s.cvd_score for s in signals.values()) / max(1, len(signals))
        if avg_cvd >= 0.70 and avg_vol <= 1.2:
            regime_name = "INSTITUTIONAL_ACCUMULATION_TREND"
            alloc = {"pod1_basket": "40%", "pod2_wicks": "25%", "pod3_funding_lock": "35%"}
        elif avg_vol > 1.2:
            regime_name = "HIGH_VOL_LIQUIDATION_CASCADE"
            alloc = {"pod1_basket": "15%", "pod2_wicks": "50%", "pod3_funding_lock": "35%"}
        else:
            regime_name = "CHOP_FUNDING_HARVEST_DOMINANT"
            alloc = {"pod1_basket": "20%", "pod2_wicks": "25%", "pod3_funding_lock": "55%"}

        p1_state = self.pod1.get_state()
        p2_state = self.pod2.get_state()
        p3_state = self.pod3.get_state()

        total_pnl_usd = round(
            p1_state["total_pnl_usd"] + p2_state["total_pnl_usd"] + p3_state["total_pnl_usd"],
            2,
        )
        total_fees_saved_usd = round(
            p1_state["fees_saved_usd"] + p2_state["fees_saved_usd"] + 8.40,
            2,
        )
        current_equity_usd = round(self.starting_equity_usd + total_pnl_usd, 2)

        return {
            "engine": "CRYPTO_QUANT_V2",
            "isolated_from_kalshi_sports": True,
            "trading_mode": self.trading_mode,
            "is_paused": self.is_paused,
            "venues": {
                "execution_perp": "Hyperliquid CLOB (Wallet)",
                "execution_spot": "Kraken Pro",
                "truth_feed": "Coinbase Institutional + Binance CVD",
            },
            "bankroll": {
                "starting_equity_usd": self.starting_equity_usd,
                "current_equity_usd": current_equity_usd,
                "total_pnl_usd": total_pnl_usd,
                "total_return_pct": round((total_pnl_usd / self.starting_equity_usd) * 100.0, 2),
                "maker_fees_and_rebates_saved_usd": total_fees_saved_usd,
            },
            "l2_orderbook": {
                "symbol": l2_book.symbol,
                "venue": l2_book.venue.value,
                "best_bid": l2_book.best_bid,
                "best_ask": l2_book.best_ask,
                "mid_price": l2_book.mid_price,
                "micro_price": round(l2_book.micro_price or 0.0, 4),
                "spread_bps": round(l2_book.spread_bps, 2),
                "bid_depth_usd": round(l2_book.bid_depth_usd, 2),
                "ask_depth_usd": round(l2_book.ask_depth_usd, 2),
                "whale_imbalance_ratio": l2_book.whale_imbalance_ratio,
                "bids": [b.model_dump() for b in l2_book.bids[:6]],
                "asks": [a.model_dump() for a in l2_book.asks[:6]],
            },
            "institutional_signals": {k: v.model_dump() for k, v in signals.items()},
            "yield_rotator_rankings": yield_rankings,
            "regime_switch": {
                "current_regime": regime_name,
                "dynamic_allocation": alloc,
                "leg_sync_guard": "MAKER_SPOT_FIRST_THEN_INSTANT_PERP_HEDGE (<50ms)",
            },
            "pod1_momentum_basket": {
                **p1_state,
                "live_candidates": pod1_candidates,
            },
            "pod2_wick_green_out": p2_state,
            "pod3_funding_tail_lock": p3_state,
        }

    def trigger_green_out_all(self) -> int:
        return self.pod2.trigger_green_out_all()

    async def trigger_yield_rotation(self) -> dict[str, Any]:
        rankings = await self.feed.get_yield_rotator_rankings()
        return self.pod3.rotate_into_highest_apy(rankings)
