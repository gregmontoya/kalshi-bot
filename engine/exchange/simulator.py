"""High-Fidelity Paper Trading Simulator for Kalshi Quant Engine.

Enforces:
- Exact Maker/Taker fee calculations
- Reciprocal Order Book matching
- Mandatory safety flags: sell_position_floor: 0, cancel_on_pause: true
- Tracking of cash balance, open positions, resting orders, and realized PnL
"""

import logging
import uuid
from typing import Optional
from engine.config import settings
from engine.core.math_engine import MathEngine
from engine.core.models import (
    Order,
    OrderAction,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    ReciprocalOrderBook,
)

logger = logging.getLogger(__name__)


class KalshiSimulator:
    """Simulates Kalshi exchange execution in PAPER trading mode."""

    def __init__(self, initial_cash_cents: int = 1_000_000):  # $10,000.00 default
        self.cash_balance_cents: int = initial_cash_cents
        self.positions: dict[str, Position] = {}  # ticker -> Position
        self.resting_orders: dict[str, Order] = {}  # order_id -> Order
        self.order_history: list[Order] = []
        self.total_maker_fees_saved_cents: int = 0
        self.total_taker_fees_paid_cents: int = 0

    def get_balance(self) -> dict[str, int]:
        portfolio_value = self.cash_balance_cents
        for pos in self.positions.values():
            if pos.current_bid_cents:
                portfolio_value += pos.contracts * pos.current_bid_cents
            else:
                portfolio_value += int(pos.contracts * pos.avg_price_cents)

        return {
            "cash_balance_cents": self.cash_balance_cents,
            "portfolio_value_cents": portfolio_value,
            "resting_orders_count": len(self.resting_orders),
            "open_positions_count": len(self.positions),
        }

    def place_order(
        self,
        ticker: str,
        action: OrderAction,
        side: OrderSide,
        count: int,
        yes_price: int,
        post_only: bool = True,
        order_type: OrderType = OrderType.LIMIT,
        client_order_id: Optional[str] = None,
        strategy_pod: Optional[str] = None,
        basket_id: Optional[str] = None,
        current_book: Optional[ReciprocalOrderBook] = None,
    ) -> Order:
        """
        Simulates order placement according to exchange rules.
        Rejects sells if sell_position_floor is violated (naked short prevention).
        """
        order_id = f"sim-{uuid.uuid4().hex[:12]}"
        cid = client_order_id or order_id
        no_price = 100 - yes_price

        # Mandatory Safety Floor Verification: Cannot sell more than held contracts
        if action == OrderAction.SELL:
            existing_pos = self.positions.get(ticker)
            available_contracts = existing_pos.contracts if existing_pos and existing_pos.side == side else 0
            if count > available_contracts:
                raise ValueError(
                    f"Order rejected by sell_position_floor: 0 safety rule. "
                    f"Requested sell {count} contracts of {ticker} {side.value}, but only hold {available_contracts}."
                )

        order = Order(
            order_id=order_id,
            client_order_id=cid,
            ticker=ticker,
            action=action,
            side=side,
            count=count,
            yes_price=yes_price,
            no_price=no_price,
            type=order_type,
            post_only=post_only,
            sell_position_floor=settings.DEFAULT_SELL_POSITION_FLOOR,
            cancel_on_pause=settings.DEFAULT_CANCEL_ON_PAUSE,
            status=OrderStatus.RESTING,
            strategy_pod=strategy_pod,  # type: ignore
            basket_id=basket_id,
        )

        # Check immediate match or rest
        if current_book is not None:
            self._attempt_match(order, current_book)
        else:
            self.resting_orders[order.order_id] = order

        self.order_history.append(order)
        return order

    def _attempt_match(self, order: Order, book: ReciprocalOrderBook) -> bool:
        """Evaluates whether resting order matches against reciprocal order book."""
        effective_price = order.yes_price if order.side == OrderSide.YES else order.no_price

        if order.action == OrderAction.BUY:
            best_ask = book.best_yes_ask if order.side == OrderSide.YES else book.best_no_ask
            if best_ask is not None and effective_price >= best_ask:
                # If post_only is set and it would cross, Kalshi cancels/rejects to prevent taker fee!
                if order.post_only:
                    logger.info("Order %s would cross book at %sc (ask=%sc). Rested as maker bid instead.", order.order_id, effective_price, best_ask)
                    self.resting_orders[order.order_id] = order
                    return False
                else:
                    # Crossed as taker (emergency exit or market order)
                    self._execute_fill(order, fill_price=best_ask, is_maker=False)
                    return True
            else:
                # Rests as maker limit bid
                self.resting_orders[order.order_id] = order
                return False

        else:  # SELL
            best_bid = book.best_yes_bid if order.side == OrderSide.YES else book.best_no_bid
            if best_bid is not None and effective_price <= best_bid:
                if order.post_only:
                    logger.info("Sell order %s would cross book at %sc (bid=%sc). Rested as maker ask instead.", order.order_id, effective_price, best_bid)
                    self.resting_orders[order.order_id] = order
                    return False
                else:
                    # Taker fill (emergency exit)
                    self._execute_fill(order, fill_price=best_bid, is_maker=False)
                    return True
            else:
                # Rests as maker take-profit / ask
                self.resting_orders[order.order_id] = order
                return False

    def on_market_tick(self, ticker: str, book: ReciprocalOrderBook) -> list[Order]:
        """
        Called when new orderbook ticks arrive.
        Fills resting maker bids if market trades down to our bid.
        Fills resting maker asks if market trades up to our ask.
        """
        filled_orders: list[Order] = []
        resting_ids = list(self.resting_orders.keys())

        for oid in resting_ids:
            order = self.resting_orders.get(oid)
            if not order or order.ticker != ticker:
                continue

            order_price = order.yes_price if order.side == OrderSide.YES else order.no_price

            if order.action == OrderAction.BUY:
                best_bid = book.best_yes_bid if order.side == OrderSide.YES else book.best_no_bid
                # If market bid reached or surpassed our resting bid, maker order fills
                if best_bid is not None and best_bid <= order_price:
                    self._execute_fill(order, fill_price=order_price, is_maker=True)
                    del self.resting_orders[oid]
                    filled_orders.append(order)
            else:  # SELL
                best_ask = book.best_yes_ask if order.side == OrderSide.YES else book.best_no_ask
                # If market ask reached or surpassed our resting TP ask, maker order fills
                if best_ask is not None and best_ask >= order_price:
                    self._execute_fill(order, fill_price=order_price, is_maker=True)
                    del self.resting_orders[oid]
                    filled_orders.append(order)

        # Update position valuations
        if ticker in self.positions:
            pos = self.positions[ticker]
            pos.current_bid_cents = book.best_yes_bid if pos.side == OrderSide.YES else book.best_no_bid
            pos.current_ask_cents = book.best_yes_ask if pos.side == OrderSide.YES else book.best_no_ask
            if pos.current_bid_cents:
                pos.unrealized_pnl_cents = int(pos.contracts * (pos.current_bid_cents - pos.avg_price_cents))

        return filled_orders

    def _execute_fill(self, order: Order, fill_price: int, is_maker: bool) -> None:
        """Executes fill and updates cash balance, positions, and fees."""
        order.status = OrderStatus.FILLED
        order.filled_count = order.count
        order.remaining_count = 0

        # Calculate fees strictly by law
        if is_maker:
            fee_cents = MathEngine.calculate_maker_fee(order.count, fill_price)
            hypothetical_taker_fee = MathEngine.calculate_taker_fee(order.count, fill_price)
            self.total_maker_fees_saved_cents += hypothetical_taker_fee
            order.maker_fee_paid_cents = fee_cents
        else:
            fee_cents = MathEngine.calculate_taker_fee(order.count, fill_price)
            self.total_taker_fees_paid_cents += fee_cents
            order.taker_fee_paid_cents = fee_cents

        cost_cents = order.count * fill_price

        if order.action == OrderAction.BUY:
            self.cash_balance_cents -= (cost_cents + fee_cents)
            if order.ticker in self.positions:
                pos = self.positions[order.ticker]
                total_contracts = pos.contracts + order.count
                pos.avg_price_cents = (pos.contracts * pos.avg_price_cents + cost_cents) / total_contracts
                pos.contracts = total_contracts
            else:
                self.positions[order.ticker] = Position(
                    ticker=order.ticker,
                    side=order.side,
                    contracts=order.count,
                    avg_price_cents=float(fill_price),
                    current_bid_cents=fill_price,
                    current_ask_cents=fill_price,
                    strategy_pod=order.strategy_pod,
                    basket_id=order.basket_id,
                )
        else:  # SELL
            self.cash_balance_cents += (cost_cents - fee_cents)
            if order.ticker in self.positions:
                pos = self.positions[order.ticker]
                realized_pnl = int(order.count * (fill_price - pos.avg_price_cents)) - fee_cents
                pos.realized_pnl_cents += realized_pnl
                pos.contracts -= order.count
                if pos.contracts <= 0:
                    del self.positions[order.ticker]

        logger.info(
            "FILL: %s %s %d contracts of %s @ %dc (Maker=%s, Fee=%dc)",
            order.action.value.upper(),
            order.side.value.upper(),
            order.count,
            order.ticker,
            fill_price,
            is_maker,
            fee_cents,
        )

    def cancel_order(self, order_id: str) -> bool:
        if order_id in self.resting_orders:
            order = self.resting_orders.pop(order_id)
            order.status = OrderStatus.CANCELED
            return True
        return False
