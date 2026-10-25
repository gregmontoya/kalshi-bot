"""Kalshi v2 WebSocket Streaming Client.

Streams real-time reciprocal order books, price ticks, and fills.
"""

import asyncio
import json
import logging
from typing import Any, Callable, Coroutine, Optional
import websockets
from engine.config import Environment, settings
from engine.core.auth import KalshiAuthSigner
from engine.core.orderbook import KalshiOrderBookEngine

logger = logging.getLogger(__name__)


class KalshiWebSocketClient:
    """Async WebSocket client connecting to Kalshi v2 WS feed."""

    def __init__(
        self,
        signer: Optional[KalshiAuthSigner] = None,
        ws_url: Optional[str] = None,
        env: Optional[Environment] = None,
    ):
        self.env = env or settings.KALSHI_ENV
        self.ws_url = ws_url or (
            settings.PROD_WS_URL if self.env == Environment.PROD else settings.DEMO_WS_URL
        )
        self.signer = signer
        self._running = False
        self._ws: Optional[websockets.WebSocketClientProtocol] = None  # type: ignore
        self._subscribers: list[Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]]] = []
        self._subscribed_tickers: set[str] = set()

    def add_listener(self, callback: Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]]) -> None:
        self._subscribers.append(callback)

    def subscribe_tickers(self, tickers: list[str]) -> None:
        self._subscribed_tickers.update(tickers)
        if self._ws and not self._ws.closed:
            asyncio.create_task(self._send_subscriptions(tickers))

    async def _send_subscriptions(self, tickers: list[str]) -> None:
        for ticker in tickers:
            msg = {
                "id": 1,
                "cmd": "subscribe",
                "params": {
                    "channels": ["orderbook_delta", "trade", "ticker"],
                    "market_tickers": [ticker],
                },
            }
            if self._ws:
                await self._ws.send(json.dumps(msg))

    async def run(self) -> None:
        """Main WebSocket loop with automatic reconnect."""
        self._running = True
        retry_delay = 1.0

        while self._running:
            try:
                headers = {}
                if self.signer and self.signer.has_key:
                    # RSA-PSS auth headers for ws connection
                    headers = self.signer.get_auth_headers(method="GET", path_or_url="/trade-api/ws/v2")

                logger.info("Connecting to Kalshi WebSocket at %s...", self.ws_url)
                async with websockets.connect(self.ws_url, extra_headers=headers) as ws:
                    self._ws = ws
                    retry_delay = 1.0
                    logger.info("Kalshi WebSocket connected successfully.")

                    if self._subscribed_tickers:
                        await self._send_subscriptions(list(self._subscribed_tickers))

                    async for message in ws:
                        if not self._running:
                            break
                        data = json.loads(message)
                        msg_type = data.get("type", "unknown")
                        for sub in self._subscribers:
                            try:
                                await sub(msg_type, data)
                            except Exception as err:
                                logger.error("Error in WS subscriber callback: %s", err)

            except (websockets.ConnectionClosed, OSError, Exception) as exc:
                if not self._running:
                    break
                logger.warning("Kalshi WebSocket disconnected: %s. Reconnecting in %.1fs...", exc, retry_delay)
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2.0, 30.0)

    async def stop(self) -> None:
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
