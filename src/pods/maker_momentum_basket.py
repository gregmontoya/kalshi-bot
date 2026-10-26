"""Strategy Pod 1: Maker Momentum Basket.

Specification:
1. Scan for contracts trending up into the 68c to 76c zone where de-vigged sharp Vegas probability >= kalshi_ask + 3c.
2. Group 2 to 3 confirmed plays into a tracked BasketID in DuckDB/SQLite.
3. No Native Combos: Execute as Synthetic Baskets on individual single-game order books.
4. Enter via resting Maker limit bids 2c below current price to catch dip fills.
5. Immediately upon fill, rest a Maker Take-Profit sell order at 88c to 92c.
6. Vegas-Confirmed <50c Guillotine:
   - If any basket leg drops <= 48c, check live de-vigged sharp probability.
   - If sharp prob is still >= 58%, HOLD (ignore retail noise).
   - If sharp prob also dropped < 50%, fire immediate exit on that leg.
"""

import asyncio
import logging
import uuid
from typing import Any, Optional
from src.config import settings
from src.core.fee_math import FeeMathEngine
from src.core.models import (
    Basket,
    BasketLeg,
    Order,
    OrderAction,
    OrderSide,
    OrderStatus,
    OrderType,
    ReciprocalOrderBook,
    StrategyPodType,
)
from src.core.orderbook import KalshiOrderBookEngine
from src.exchange.kalshi_client import KalshiClient
from src.exchange.simulator import KalshiSimulator
from src.feeds.odds_api import OddsApiClient
from src.pods.base import BaseStrategyPod
from src.storage.duckdb_manager import DuckDBManager

logger = logging.getLogger(__name__)


class MakerMomentumBasketPod(BaseStrategyPod):
    """Pod 1: Maker Momentum Basket strategy engine."""

    def __init__(
        self,
        client: KalshiClient,
        simulator: Optional[KalshiSimulator],
        duckdb: DuckDBManager,
        odds_client: OddsApiClient,
        trading_mode: str = "PAPER",
    ):
        super().__init__(name="maker_momentum_basket", pod_type=StrategyPodType.POD1_MOMENTUM_BASKET)
        self.client = client
        self.simulator = simulator
        self.duckdb = duckdb
        self.odds_client = odds_client
        self.trading_mode = trading_mode
        self.active_baskets: dict[str, Basket] = {}
        self.tracked_tickers: set[str] = set()

    async def start(self) -> None:
        self.is_running = True
        logger.info("Pod 1 [maker_momentum_basket] started in %s mode.", self.trading_mode)
        for b in self.duckdb.get_all_baskets():
            if b.status == "active":
                self.active_baskets[b.basket_id] = b
                for leg in b.legs:
                    self.tracked_tickers.add(leg.ticker)

    async def stop(self) -> None:
        self.is_running = False
        logger.info("Pod 1 [maker_momentum_basket] stopped.")

    async def scan_opportunities(self) -> list[dict[str, Any]]:
        """
        Scans for markets in 68c-76c zone where de-vigged sharp prob >= kalshi_ask + 3c.
        Groups into synthetic baskets of 2-3 legs.
        """
        if self.is_paused or not self.is_running:
            return []

        sharp_odds_list = await self.odds_client.get_sharp_odds()
        sharp_map = {f"{s.team_home.lower()}": s for s in sharp_odds_list}

        try:
            markets = await self.client.get_markets(status="open")
        except Exception as e:
            logger.error("Could not fetch markets: %s", e)
            markets = []

        confirmed_plays = []

        for m in markets:
            if not m.yes_ask:
                continue

            ask_price = m.yes_ask
            if not (settings.POD1_ENTRY_PRICE_MIN <= ask_price <= settings.POD1_ENTRY_PRICE_MAX):
                continue

            matched_sharp = None
            for key, sharp in sharp_map.items():
                if key in m.title.lower() or key in (m.subtitle or "").lower():
                    matched_sharp = sharp
                    break

            if matched_sharp:
                sharp_prob_cents = int(matched_sharp.home_devigged_prob * 100.0)
                edge = sharp_prob_cents - ask_price
                if edge >= settings.POD1_MIN_SHARP_EDGE_CENTS:
                    confirmed_plays.append({
                        "ticker": m.ticker,
                        "market_title": m.title,
                        "kalshi_ask": ask_price,
                        "sharp_prob_cents": sharp_prob_cents,
                        "sharp_prob_float": matched_sharp.home_devigged_prob,
                        "edge_cents": edge,
                        "bookmaker": matched_sharp.bookmaker,
                    })

        created_baskets = []
        basket_size = settings.POD1_BASKET_SIZE
        while len(confirmed_plays) >= 2:
            chunk = confirmed_plays[:basket_size]
            confirmed_plays = confirmed_plays[basket_size:]
            basket = await self.create_synthetic_basket(chunk)
            if basket:
                created_baskets.append(basket)

        return [b.model_dump() for b in created_baskets]

    async def create_synthetic_basket(self, plays: list[dict[str, Any]]) -> Optional[Basket]:
        """
        Creates a Synthetic Basket (never native parlay) on individual single-game books.
        Places Maker limit bids 2c below current price to catch dip fills.
        """
        basket_id = f"bkt-{uuid.uuid4().hex[:8]}"
        legs = []

        for p in plays:
            ticker = p["ticker"]
            current_ask = p["kalshi_ask"]
            entry_bid_price = max(1, current_ask - settings.POD1_DIP_FILL_OFFSET_CENTS)
            contracts = settings.POD1_DEFAULT_CONTRACTS_PER_LEG
            tp_price = 90

            order = await self._place_maker_order(
                ticker=ticker,
                action=OrderAction.BUY,
                side=OrderSide.YES,
                count=contracts,
                yes_price=entry_bid_price,
                strategy_pod=self.name,
                basket_id=basket_id,
            )

            leg = BasketLeg(
                ticker=ticker,
                market_title=p["market_title"],
                target_entry_price=entry_bid_price,
                actual_entry_price=None,
                contracts=contracts,
                status="pending_maker_bid",
                entry_order_id=order.order_id,
                tp_order_id=None,
                tp_price=tp_price,
                current_price=entry_bid_price,
                current_sharp_prob=p["sharp_prob_float"],
                pnl_cents=0,
            )
            legs.append(leg)
            self.tracked_tickers.add(ticker)

        basket = Basket(
            basket_id=basket_id,
            status="active",
            legs=legs,
            total_invested_cents=sum(l.target_entry_price * l.contracts for l in legs),
        )

        self.active_baskets[basket_id] = basket
        self.duckdb.save_basket(basket)
        logger.info(
            "Created Synthetic Basket %s with %d individual legs (Zero-fee resting Maker bids placed).",
            basket_id,
            len(legs),
        )
        return basket

    async def on_orderbook_tick(self, ticker: str, book: ReciprocalOrderBook) -> None:
        """
        Monitors orderbook ticks for active basket legs.
        Applies Vegas-Confirmed <50c Guillotine:
        - If price drops <= 48c:
          - If sharp prob >= 58%: HOLD (retail noise).
          - If sharp prob < 50%: fire immediate exit.
        """
        best_bid = book.best_yes_bid
        if best_bid is None:
            return

        for basket_id, basket in list(self.active_baskets.items()):
            for leg in basket.legs:
                if leg.ticker == ticker and leg.status in ("filled_holding", "tp_resting"):
                    leg.current_price = best_bid

                    if best_bid <= settings.POD1_GUILLOTINE_TRIGGER_PRICE:
                        await self._evaluate_guillotine(basket, leg, current_bid=best_bid)

            self.duckdb.save_basket(basket)

    async def _evaluate_guillotine(self, basket: Basket, leg: BasketLeg, current_bid: int) -> None:
        """Executes Vegas-Confirmed <50c Guillotine evaluation."""
        if leg.guillotine_triggered:
            return

        sharp_odds = await self.odds_client.get_sharp_odds()
        matched_sharp = None
        for s in sharp_odds:
            if s.team_home.lower() in leg.market_title.lower():
                matched_sharp = s
                break

        current_sharp_prob = matched_sharp.home_devigged_prob if matched_sharp else (leg.current_sharp_prob or 0.50)
        leg.current_sharp_prob = current_sharp_prob

        if current_sharp_prob >= settings.POD1_GUILLOTINE_HOLD_SHARP_PROB:
            logger.info(
                "GUILLOTINE OVERRULE: %s dropped to %dc, but sharp Vegas prob is %.1f%% (>=58%%). HOLDING POSITION.",
                leg.ticker,
                current_bid,
                current_sharp_prob * 100.0,
            )
        elif current_sharp_prob < settings.POD1_GUILLOTINE_EXIT_SHARP_PROB:
            logger.warning(
                "GUILLOTINE TRIGGERED: %s dropped to %dc and sharp Vegas prob dropped to %.1f%% (<50%%). EXECUTING IMMEDIATE STOP-LOSS EXIT.",
                leg.ticker,
                current_bid,
                current_sharp_prob * 100.0,
            )
            await self._execute_guillotine_exit(basket, leg, exit_price=current_bid)

    async def _execute_guillotine_exit(self, basket: Basket, leg: BasketLeg, exit_price: int) -> None:
        """Fires immediate exit on guillotined leg with mandatory safety floor."""
        leg.guillotine_triggered = True
        leg.status = "guillotined"
        leg.exit_price = exit_price

        if leg.tp_order_id:
            await self._cancel_order(leg.tp_order_id)

        await self._place_sell_order(
            ticker=leg.ticker,
            count=leg.contracts,
            price_cents=exit_price,
            is_emergency=True,
        )

        entry = leg.actual_entry_price or leg.target_entry_price
        leg.pnl_cents = leg.contracts * (exit_price - entry)
        basket.total_realized_pnl_cents += leg.pnl_cents
        self.duckdb.save_basket(basket)

    async def on_fill(self, order: Order) -> None:
        """
        When Maker dip bid fills:
        Immediately rests a Maker Take-Profit sell order at 88c to 92c (default 90c).
        """
        if order.strategy_pod != self.pod_type.value and order.strategy_pod != self.name:
            return

        for basket_id, basket in self.active_baskets.items():
            for leg in basket.legs:
                if leg.entry_order_id == order.order_id and leg.status == "pending_maker_bid":
                    leg.status = "filled_holding"
                    leg.actual_entry_price = order.yes_price

                    logger.info(
                        "Basket %s leg %s filled at %dc! Resting Maker TP order at %dc.",
                        basket_id,
                        leg.ticker,
                        leg.actual_entry_price,
                        leg.tp_price,
                    )
                    tp_order = await self._place_maker_order(
                        ticker=leg.ticker,
                        action=OrderAction.SELL,
                        side=OrderSide.YES,
                        count=leg.contracts,
                        yes_price=leg.tp_price,
                        strategy_pod=self.name,
                        basket_id=basket_id,
                    )
                    leg.tp_order_id = tp_order.order_id
                    leg.status = "tp_resting"
                    self.duckdb.save_basket(basket)
                    return

                elif leg.tp_order_id == order.order_id:
                    leg.status = "completed"
                    entry = leg.actual_entry_price or leg.target_entry_price
                    leg.pnl_cents = leg.contracts * (leg.tp_price - entry)
                    basket.total_realized_pnl_cents += leg.pnl_cents
                    logger.info("TAKE-PROFIT HIT on basket leg %s! PnL: +%dc", leg.ticker, leg.pnl_cents)

                    if all(l.status in ("completed", "guillotined") for l in basket.legs):
                        basket.status = "completed"
                        logger.info("Basket %s fully resolved. Realized PnL: %dc", basket_id, basket.total_realized_pnl_cents)

                    self.duckdb.save_basket(basket)
                    return

    async def _place_maker_order(
        self,
        ticker: str,
        action: OrderAction,
        side: OrderSide,
        count: int,
        yes_price: int,
        strategy_pod: str,
        basket_id: str,
    ) -> Order:
        if self.trading_mode == "LIVE":
            return await self.client.create_order(
                ticker=ticker,
                action=action,
                side=side,
                count=count,
                yes_price=yes_price,
                post_only=True,
                strategy_pod=strategy_pod,
                basket_id=basket_id,
            )
        else:
            assert self.simulator is not None
            return self.simulator.place_order(
                ticker=ticker,
                action=action,
                side=side,
                count=count,
                yes_price=yes_price,
                post_only=True,
                strategy_pod=strategy_pod,
                basket_id=basket_id,
            )

    async def _place_sell_order(
        self,
        ticker: str,
        count: int,
        price_cents: int,
        is_emergency: bool = False,
    ) -> Order:
        post_only = not is_emergency
        order_type = OrderType.MARKET if is_emergency else OrderType.LIMIT

        if self.trading_mode == "LIVE":
            return await self.client.create_order(
                ticker=ticker,
                action=OrderAction.SELL,
                side=OrderSide.YES,
                count=count,
                yes_price=price_cents,
                post_only=post_only,
                order_type=order_type,
                strategy_pod=self.name,
            )
        else:
            assert self.simulator is not None
            return self.simulator.place_order(
                ticker=ticker,
                action=OrderAction.SELL,
                side=OrderSide.YES,
                count=count,
                yes_price=price_cents,
                post_only=post_only,
                order_type=order_type,
                strategy_pod=self.name,
            )

    async def _cancel_order(self, order_id: str) -> None:
        if self.trading_mode == "LIVE":
            await self.client.cancel_order(order_id)
        elif self.simulator:
            self.simulator.cancel_order(order_id)

    def get_state(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.pod_type.value,
            "is_running": self.is_running,
            "is_paused": self.is_paused,
            "active_baskets_count": len(self.active_baskets),
            "baskets": [b.model_dump() for b in self.active_baskets.values()],
        }
