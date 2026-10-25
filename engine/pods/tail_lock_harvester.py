"""Strategy Pod 3: Tail-Lock Harvester.

Specification:
1. Scan 4th quarter contracts at 95c to 97c ONLY when live ESPN clock/possession/timeout feed
   confirms mathematical win probability >= 99.2%.
2. Maker-First execution: Rest limit buy orders at 95c-96c to harvest 4-5c premium with zero taker fees.
3. Holds to 100c expiration settlement.
"""

import asyncio
import logging
from typing import Any, Optional
from engine.config import settings
from engine.core.math_engine import MathEngine
from engine.core.models import (
    GameState,
    Order,
    OrderAction,
    OrderSide,
    OrderStatus,
    OrderType,
    ReciprocalOrderBook,
    StrategyPodType,
)
from engine.exchange.kalshi_client import KalshiClient
from engine.exchange.simulator import KalshiSimulator
from engine.feeds.espn_hidden_api import ESPNHiddenApiClient
from engine.pods.base import BaseStrategyPod
from engine.storage.duckdb_manager import DuckDBManager

logger = logging.getLogger(__name__)


class TailLockHarvesterPod(BaseStrategyPod):
    """Pod 3: Late 4th-Quarter Tail-Lock Harvester."""

    def __init__(
        self,
        client: KalshiClient,
        simulator: Optional[KalshiSimulator],
        duckdb: DuckDBManager,
        espn_client: ESPNHiddenApiClient,
        trading_mode: str = "PAPER",
    ):
        super().__init__(name="tail_lock_harvester", pod_type=StrategyPodType.POD3_TAIL_LOCK)
        self.client = client
        self.simulator = simulator
        self.duckdb = duckdb
        self.espn_client = espn_client
        self.trading_mode = trading_mode
        self.active_harvests: dict[str, dict[str, Any]] = {}

    async def start(self) -> None:
        self.is_running = True
        logger.info("Pod 3 [tail_lock_harvester] active in %s mode.", self.trading_mode)

    async def stop(self) -> None:
        self.is_running = False
        logger.info("Pod 3 [tail_lock_harvester] stopped.")

    async def scan_opportunities(self) -> list[dict[str, Any]]:
        """
        Scans for 4th quarter contracts at 95c to 97c where ESPN clock & possession confirm >= 99.2% win probability.
        """
        if self.is_paused or not self.is_running:
            return []

        # 1. Fetch live game states from ESPN
        games = await self.espn_client.get_scoreboard(sport="football", league="nfl")
        fourth_quarter_games = [g for g in games if g.is_fourth_quarter and not g.is_final]

        if not fourth_quarter_games:
            return []

        # 2. Fetch Kalshi markets
        try:
            markets = await self.client.get_markets(status="open")
        except Exception as e:
            logger.error("Could not fetch markets for Pod 3: %s", e)
            markets = []

        qualified_harvests = []

        for game in fourth_quarter_games:
            # Check team names in market titles
            for m in markets:
                if not m.yes_bid or not m.yes_ask:
                    continue

                is_home = game.home_team.lower() in m.title.lower()
                is_away = game.away_team.lower() in m.title.lower()

                if not (is_home or is_away):
                    continue

                # Determine which team is in lead
                if game.score_margin > 0 and is_home:
                    leader_score_margin = game.score_margin
                    has_ball = (game.possession_team == game.home_team)
                    opp_timeouts = game.away_timeouts
                    espn_win_prob = game.win_probability_home
                elif game.score_margin < 0 and is_away:
                    leader_score_margin = abs(game.score_margin)
                    has_ball = (game.possession_team == game.away_team)
                    opp_timeouts = game.home_timeouts
                    espn_win_prob = 1.0 - game.win_probability_home
                else:
                    continue

                # Contract price check: 95c to 97c zone
                ask_price = m.yes_ask
                if not (settings.POD3_ENTRY_PRICE_MIN <= ask_price <= settings.POD3_ENTRY_PRICE_MAX):
                    continue

                # Mathematical Win Probability Verification
                math_result = MathEngine.calculate_tail_lock_probability(
                    is_fourth_quarter=True,
                    seconds_remaining=game.seconds_remaining,
                    score_margin=leader_score_margin,
                    has_possession=has_ball,
                    down=game.down,
                    opp_timeouts_remaining=opp_timeouts,
                    espn_win_prob=espn_win_prob,
                )

                if math_result["qualifies_tail_lock"]:
                    entry_record = {
                        "ticker": m.ticker,
                        "market_title": m.title,
                        "price": ask_price,
                        "calculated_win_prob": math_result["calculated_win_prob"],
                        "score_margin": leader_score_margin,
                        "seconds_remaining": game.seconds_remaining,
                        "reason": math_result["reason"],
                    }
                    qualified_harvests.append(entry_record)

                    if m.ticker not in self.active_harvests:
                        await self._submit_tail_lock_entry(
                            ticker=m.ticker,
                            bid_price=min(ask_price, 96),  # Rest maker bid at 95c-96c
                            contracts=settings.POD3_DEFAULT_CONTRACTS,
                            info=entry_record,
                        )

        return qualified_harvests

    async def _submit_tail_lock_entry(
        self,
        ticker: str,
        bid_price: int,
        contracts: int,
        info: dict[str, Any],
    ) -> None:
        """Enters tail lock with resting Maker limit buy order."""
        logger.info(
            "Pod 3: HARVEST TRIGGER for %s @ %dc (Prob: %.2f%% - %s)",
            ticker,
            bid_price,
            info["calculated_win_prob"] * 100.0,
            info["reason"],
        )

        if self.trading_mode == "LIVE":
            order = await self.client.create_order(
                ticker=ticker,
                action=OrderAction.BUY,
                side=OrderSide.YES,
                count=contracts,
                yes_price=bid_price,
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
                yes_price=bid_price,
                post_only=True,
                strategy_pod=self.name,
            )

        self.active_harvests[ticker] = {
            "ticker": ticker,
            "order_id": order.order_id,
            "contracts": contracts,
            "entry_price": bid_price,
            "expected_payout": contracts * 100,
            "expected_profit_cents": contracts * (100 - bid_price),
            "status": "resting_maker_buy",
            "info": info,
        }

    async def on_orderbook_tick(self, ticker: str, book: ReciprocalOrderBook) -> None:
        pass

    async def on_fill(self, order: Order) -> None:
        if order.strategy_pod != self.pod_type and order.strategy_pod != self.name:
            return

        if order.ticker in self.active_harvests:
            harv = self.active_harvests[order.ticker]
            harv["status"] = "filled_harvesting"
            logger.info(
                "Pod 3: Tail-Lock filled for %s! Harvesting +$%0.2f at settlement.",
                order.ticker,
                harv["expected_profit_cents"] / 100.0,
            )

    def get_state(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.pod_type.value,
            "is_running": self.is_running,
            "is_paused": self.is_paused,
            "harvests_count": len(self.active_harvests),
            "harvests": list(self.active_harvests.values()),
        }
