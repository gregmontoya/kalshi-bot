"""Abstract Base Strategy Pod Interface."""

from abc import ABC, abstractmethod
from typing import Any
from src.core.models import Order, ReciprocalOrderBook, StrategyPodType


class BaseStrategyPod(ABC):
    """Base class for all strategy pods."""

    def __init__(self, name: str, pod_type: StrategyPodType):
        self.name = name
        self.pod_type = pod_type
        self.is_running = False
        self.is_paused = False

    @abstractmethod
    async def start(self) -> None:
        """Starts background loops and scanners."""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stops pod execution and cancels resting orders."""
        pass

    def pause(self) -> None:
        self.is_paused = True

    def resume(self) -> None:
        self.is_paused = False

    @abstractmethod
    async def scan_opportunities(self) -> list[dict[str, Any]]:
        """Scans markets and sharp feeds for strategy entry signals."""
        pass

    @abstractmethod
    async def on_orderbook_tick(self, ticker: str, book: ReciprocalOrderBook) -> None:
        """Handles live price / book updates."""
        pass

    @abstractmethod
    async def on_fill(self, order: Order) -> None:
        """Handles order fills and triggers follow-up resting orders (e.g. TP)."""
        pass

    @abstractmethod
    def get_state(self) -> dict[str, Any]:
        """Returns pod state for UI dashboard and Telegram status."""
        pass
