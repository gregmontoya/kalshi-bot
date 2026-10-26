"""Institutional Circuit Breakers and Capital Preservation Rules.

Enforces:
- Daily Drawdown Cap: auto-halts bot when daily loss exceeds threshold ($500.00 default)
- Maximum Portfolio Exposure Cap (35% default)
- Non-negotiable sell_position_floor: 0 (prevents accidental naked shorting)
- Emergency kill switch
"""

import logging
from typing import Any, Optional
from src.config import settings

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """Manages risk limits, daily drawdown tripwires, and order safety rules."""

    def __init__(
        self,
        max_daily_drawdown_dollars: float = 500.00,
        max_portfolio_exposure_pct: float = 0.35,
    ):
        self.max_daily_drawdown_dollars = max_daily_drawdown_dollars
        self.max_portfolio_exposure_pct = max_portfolio_exposure_pct
        self.is_tripped = False
        self.trip_reason: Optional[str] = None
        self.daily_realized_loss_cents = 0

    def check_order_safety(
        self,
        action: str,
        contracts: int,
        held_contracts: int,
        total_cash_cents: int,
        order_cost_cents: int,
    ) -> tuple[bool, Optional[str]]:
        """
        Validates order against circuit breaker limits and safety rules.
        """
        if self.is_tripped:
            return False, f"Circuit Breaker TRIPPED: {self.trip_reason}. All new entries halted."

        # Non-negotiable Law: sell_position_floor: 0
        if action.lower() == "sell" and contracts > held_contracts:
            return False, (
                f"REJECTED by sell_position_floor: 0 rule. "
                f"Attempted to sell {contracts} contracts, but only hold {held_contracts}."
            )

        # Portfolio Exposure Cap
        if action.lower() == "buy" and total_cash_cents > 0:
            exposure_pct = order_cost_cents / total_cash_cents
            if exposure_pct > self.max_portfolio_exposure_pct:
                return False, (
                    f"Order exceeds max exposure cap: {exposure_pct * 100:.1f}% > "
                    f"{self.max_portfolio_exposure_pct * 100:.1f}% allowed."
                )

        return True, None

    def record_pnl(self, pnl_cents: int) -> None:
        """Tracks daily PnL and trips breaker if daily loss crosses cap."""
        if pnl_cents < 0:
            self.daily_realized_loss_cents += abs(pnl_cents)
            loss_dollars = self.daily_realized_loss_cents / 100.0

            if loss_dollars >= self.max_daily_drawdown_dollars:
                self.is_tripped = True
                self.trip_reason = f"Daily drawdown cap hit: -${loss_dollars:.2f} >= -${self.max_daily_drawdown_dollars:.2f}"
                logger.critical("CIRCUIT BREAKER ACTIVATED: %s", self.trip_reason)

    def manual_kill_switch(self, reason: str = "Manual emergency halt") -> None:
        """Immediately trips circuit breaker."""
        self.is_tripped = True
        self.trip_reason = reason
        logger.warning("MANUAL KILL SWITCH TRIGGERED: %s", reason)

    def reset(self) -> None:
        """Resets circuit breaker for new trading day."""
        self.is_tripped = False
        self.trip_reason = None
        self.daily_realized_loss_cents = 0
        logger.info("Circuit breaker reset.")
