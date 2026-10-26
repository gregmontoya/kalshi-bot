"""Strategy Pod 2: Sharp Value & Mid-Game Green Out.

Specification:
1. Pre-game Maker limit buy (38c to 58c) when de-vigged sharp probability exceeds Kalshi price by >= 5c.
2. Mid-Game Green Out: When position rises +16c (e.g., opening drive TD), calculate exact contracts to sell
   (or opposing NO to buy) to lock in equal guaranteed profit regardless of final outcome.
3. Maker-First Execution: Rest limit orders with post_only: true, fees = $0.00.
"""

import asyncio
import logging
from typing import Any, Optional
from src.config import settings
from src.core.fee_math import FeeMathEngine
from src.core.models import (
    Order,
    OrderAction,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    ReciprocalOrderBook,
    StrategyPodType,
)
from src.exchange.kalshi_client import KalshiClient
from src.exchange.simulator import KalshiSimulator
from src.feeds.odds_api import OddsApiClient
from src.pods.base import BaseStrategyPod
from src.storage.duckdb_manager import DuckDBManager

logger = logging.getLogger(__name__)


class SharpValueGreenOutPod(BaseStrategyPod):
    """Pod 2: Sharp Value entry with mathematical mid-game green-out locking."""

    def __init__(
        self,
        client: KalshiClient,
        simulator: Optional[KalshiSimulator],
        duckdb: DuckDBManager,
        odds_client: OddsApiClient,
        trading_mode: str = "PAPER",
    ):
        super().__init__(name="sharp_value_green_out", pod_type=StrategyPodType.POD2_VALUE_GREEN_OUT)
        self.client = client
        self.simulator = simulator
        self.duckdb = duckdb
        self.odds_client = odds_client
        self.trading_mode = trading_mode
        self.active_positions: dict[str, dict[str, Any]] = {}
        self.pending_entries: dict[str, Order] = {}

    async def start(self) -> None:
        self.is_running = True
        logger.info("Pod 2 [sharp_value_green_out] active in %s mode.", self.trading_mode)

    async def stop(self) -> None:
        self.is_running = False
        logger.info("Pod 2 [sharp_value_green_out] stopped.")

    async def scan_opportunities(self) -> list[dict[str, Any]]:
        """
        Scans pre-game markets in 38c - 58c range where de-vigged sharp prob >= kalshi_price + 5c.
        """
        if self.is_paused or not self.is_running:
            return []

        sharp_odds = await self.odds_client.get_sharp_odds()
        sharp_map = {s.team_home.lower(): s for s in sharp_odds}

        try:
            markets = await self.client.get_markets(status="open")
        except Exception as e:
            logger.error("Could not fetch markets for Pod 2: %s", e)
            markets = []

        qualified_entries = []

        for m in markets:
            if not m.yes_bid or not m.yes_ask:
                continue

            price = m.yes_ask
            if not (settings.POD2_ENTRY_PRICE_MIN <= price <= settings.POD2_ENTRY_PRICE_MAX):
                continue

            matched_sharp = None
            for team, sharp in sharp_map.items():
                if team in m.title.lower():
                    matched_sharp = sharp
                    break

            if matched_sharp:
                sharp_prob_cents = int(matched_sharp.home_devigged_prob * 100.0)
                edge = sharp_prob_cents - price
                if edge >= settings.POD2_MIN_SHARP_EDGE_CENTS:
                    qualified_entries.append({
                        "ticker": m.ticker,
                        "market_title": m.title,
                        "kalshi_price": price,
                        "sharp_prob_cents": sharp_prob_cents,
                        "edge_cents": edge,
                        "bookmaker": matched_sharp.bookmaker,
                    })

                    if m.ticker not in self.active_positions and m.ticker not in self.pending_entries:
                        await self._submit_value_entry(
                            ticker=m.ticker,
                            entry_price=m.yes_bid,
                            contracts=settings.POD2_DEFAULT_CONTRACTS,
                            market_title=m.title,
                            sharp_prob=matched_sharp.home_devigged_prob,
                        )

        return qualified_entries

    async def _submit_value_entry(
        self,
        ticker: str,
        entry_price: int,
        contracts: int,
        market_title: str,
        sharp_prob: float,
    ) -> None:
        """Places pre-game resting Maker limit buy order."""
        logger.info(
            "Pod 2: Placing Maker value buy for %s: %d contracts @ %dc (Sharp Prob: %.1f%%)",
            ticker,
            contracts,
            entry_price,
            sharp_prob * 100.0,
        )

        if self.trading_mode == "LIVE":
            order = await self.client.create_order(
                ticker=ticker,
                action=OrderAction.BUY,
                side=OrderSide.YES,
                count=contracts,
                yes_price=entry_price,
                post_only=True,
                strategy_pod=self.name,
            )
        else:
            assert self.simulator is not None
            order = self.simulator.place_order(
                ticker=ticker,
                action=OrderAction.BUY,
                side=OrderSide.YES,
                count=contracts,
                yes_price=entry_price,
                post_only=True,
                strategy_pod=self.name,
            )

        self.pending_entries[order.order_id] = order

    async def on_fill(self, order: Order) -> None:
        """When pre-game Maker buy fills, register active position for mid-game green out monitoring."""
        if order.strategy_pod != self.pod_type.value and order.strategy_pod != self.name:
            return

        if order.action == OrderAction.BUY and order.side == OrderSide.YES:
            self.active_positions[order.ticker] = {
                "ticker": order.ticker,
                "contracts": order.count,
                "entry_price": order.yes_price,
                "current_price": order.yes_price,
                "is_greened_out": False,
                "green_out_order_id": None,
                "guaranteed_profit_cents": 0,
            }
            if order.order_id in self.pending_entries:
                del self.pending_entries[order.order_id]
            logger.info("Pod 2: Position opened for %s @ %dc. Monitoring for +16c green-out.", order.ticker, order.yes_price)

        elif order.action == OrderAction.SELL:
            if order.ticker in self.active_positions:
                pos = self.active_positions[order.ticker]
                pos["is_greened_out"] = True
                profit = (order.yes_price - pos["entry_price"]) * order.count
                pos["guaranteed_profit_cents"] = profit
                logger.info(
                    "Pod 2: GREEN-OUT FILL CONFIRMED for %s @ %dc! Locked in +$%0.2f equal profit.",
                    order.ticker,
                    order.yes_price,
                    profit / 100.0,
                )

    async def on_orderbook_tick(self, ticker: str, book: ReciprocalOrderBook) -> None:
        """
        Monitors live order book for +16c gain.
        When position rises +16c, executes equal green-out!
        """
        if ticker not in self.active_positions:
            return

        pos = self.active_positions[ticker]
        if pos["is_greened_out"] or pos["green_out_order_id"] is not None:
            return

        best_yes_bid = book.best_yes_bid
        if best_yes_bid is None:
            return

        pos["current_price"] = best_yes_bid
        gain = best_yes_bid - pos["entry_price"]

        if gain >= settings.POD2_GREEN_OUT_GAIN_CENTS:
            await self._execute_midgame_green_out(ticker, pos, current_bid=best_yes_bid)

    async def _execute_midgame_green_out(self, ticker: str, pos: dict[str, Any], current_bid: int) -> None:
        """
        Calculates exact contracts to sell or opposing NO to buy to lock in equal guaranteed profit.
        """
        green_calc = FeeMathEngine.calculate_green_out(
            entry_price_cents=pos["entry_price"],
            current_yes_bid_cents=current_bid,
            initial_contracts=pos["contracts"],
        )

        logger.info(
            "Pod 2: +%dc GAIN REACHED on %s (entry: %dc -> bid: %dc). "
            "Executing Mid-Game Green Out to lock in guaranteed $%0.2f equal profit.",
            green_calc["gain_cents"],
            ticker,
            pos["entry_price"],
            current_bid,
            green_calc["guaranteed_profit_dollars"],
        )

        if self.trading_mode == "LIVE":
            order = await self.client.create_order(
                ticker=ticker,
                action=OrderAction.SELL,
                side=OrderSide.YES,
                count=pos["contracts"],
                yes_price=current_bid,
                post_only=True,
                strategy_pod=self.name,
            )
        else:
            assert self.simulator is not None
            order = self.simulator.place_order(
                ticker=ticker,
                action=OrderAction.SELL,
                side=OrderSide.YES,
                count=pos["contracts"],
                yes_price=current_bid,
                post_only=True,
                strategy_pod=self.name,
            )

        pos["green_out_order_id"] = order.order_id

    def get_state(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.pod_type.value,
            "is_running": self.is_running,
            "is_paused": self.is_paused,
            "active_positions_count": len(self.active_positions),
            "positions": list(self.active_positions.values()),
        }
