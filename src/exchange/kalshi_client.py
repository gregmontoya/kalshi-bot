"""Kalshi v2 REST API Client.

Enforces:
- Kalshi v2 Demo/Prod endpoints
- RSA-PSS request signing (timestamp + METHOD + path)
- Reciprocal Order Book Law
- Maker-First Execution (default resting limit orders)
- Mandatory Order Safety Flags: sell_position_floor: 0, cancel_on_pause: true
- No Native Combos (all combinations must be synthetic baskets)
"""

import logging
import uuid
from typing import Any, Optional
import httpx
from src.config import Environment, settings
from src.core.auth import KalshiAuthSigner
from src.core.models import (
    Market,
    Order,
    OrderAction,
    OrderSide,
    OrderStatus,
    OrderType,
    ReciprocalOrderBook,
)
from src.core.orderbook import KalshiOrderBookEngine

logger = logging.getLogger(__name__)


class KalshiClient:
    """Async client for Kalshi v2 API adhering to all workspace exchange laws."""

    def __init__(
        self,
        signer: Optional[KalshiAuthSigner] = None,
        base_url: Optional[str] = None,
        env: Optional[Environment] = None,
    ):
        self.env = env or settings.KALSHI_ENV
        self.base_url = base_url or (
            settings.PROD_REST_URL if self.env == Environment.PROD else settings.DEMO_REST_URL
        )

        if signer is not None:
            self.signer = signer
        elif settings.KALSHI_KEY_ID:
            key_source = settings.KALSHI_PRIVATE_KEY or settings.KALSHI_PRIVATE_KEY_PATH
            self.signer = KalshiAuthSigner(key_id=settings.KALSHI_KEY_ID, private_key=key_source)
        else:
            self.signer = None

        self._client: Optional[httpx.AsyncClient] = None

    async def get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(10.0, connect=5.0),
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        json_data: Optional[dict[str, Any]] = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        """Performs HTTP request with RSA-PSS authentication headers if required."""
        client = await self.get_client()
        headers = {}

        if authenticated:
            if not self.signer or not self.signer.has_key:
                raise ValueError("Authenticated request requires loaded KalshiAuthSigner RSA credentials")
            # Sign over path without query
            auth_headers = self.signer.get_auth_headers(method=method, path_or_url=path)
            headers.update(auth_headers)

        response = await client.request(
            method=method,
            url=path,
            params=params,
            json=json_data,
            headers=headers,
        )

        if response.status_code >= 400:
            logger.error("Kalshi API error %s: %s", response.status_code, response.text)
            response.raise_for_status()

        return response.json()

    # -------------------------------------------------------------------------
    # Public Market Data Endpoints
    # -------------------------------------------------------------------------
    async def get_markets(
        self,
        status: str = "open",
        series_ticker: Optional[str] = None,
        limit: int = 100,
    ) -> list[Market]:
        """Retrieves list of active markets."""
        params: dict[str, Any] = {"status": status, "limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker

        data = await self.request("GET", "/markets", params=params, authenticated=False)
        markets = []
        for item in data.get("markets", []):
            markets.append(
                Market(
                    ticker=item.get("ticker", ""),
                    title=item.get("title", ""),
                    subtitle=item.get("subtitle"),
                    category=item.get("category", "SPORTS"),
                    status=item.get("status", "open"),
                    yes_bid=item.get("yes_bid"),
                    yes_ask=item.get("yes_ask"),
                    no_bid=item.get("no_bid"),
                    no_ask=item.get("no_ask"),
                    last_price=item.get("last_price"),
                    volume=item.get("volume", 0),
                    open_interest=item.get("open_interest", 0),
                    expiration_time=item.get("expiration_time"),
                )
            )
        return markets

    async def get_orderbook(self, ticker: str) -> ReciprocalOrderBook:
        """
        Retrieves order book enforcing Reciprocal Order Book Law:
        Kalshi only returns yes and no bids. Synthesizes asks.
        """
        path = f"/markets/{ticker}/orderbook"
        data = await self.request("GET", path, authenticated=False)
        return KalshiOrderBookEngine.parse_kalshi_orderbook(ticker=ticker, payload=data)

    # -------------------------------------------------------------------------
    # Portfolio & Orders (Authenticated)
    # -------------------------------------------------------------------------
    async def get_balance(self) -> dict[str, Any]:
        """Retrieves portfolio cash balance in cents."""
        return await self.request("GET", "/portfolio/balance", authenticated=True)

    async def get_positions(self) -> list[dict[str, Any]]:
        """Retrieves active portfolio positions."""
        data = await self.request("GET", "/portfolio/positions", authenticated=True)
        return data.get("market_positions", [])

    async def create_order(
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
    ) -> Order:
        """
        Submits order enforcing all mandatory safety rules:
        - Maker-First: post_only = True by default
        - sell_position_floor: 0 (prevents accidental shorting on sells)
        - cancel_on_pause: true (cancels if market paused on scoring play)
        """
        cid = client_order_id or str(uuid.uuid4())
        no_price = 100 - yes_price

        payload: dict[str, Any] = {
            "ticker": ticker,
            "action": action.value,
            "side": side.value,
            "count": count,
            "type": order_type.value,
            "client_order_id": cid,
            "cancel_on_pause": settings.DEFAULT_CANCEL_ON_PAUSE,
        }

        if post_only:
            payload["post_only"] = True

        if action == OrderAction.SELL:
            # Non-negotiable safety floor to prevent naked shorting
            payload["sell_position_floor"] = settings.DEFAULT_SELL_POSITION_FLOOR

        if side == OrderSide.YES:
            payload["yes_price"] = yes_price
        else:
            payload["no_price"] = no_price

        response = await self.request(
            method="POST",
            path="/portfolio/orders",
            json_data=payload,
            authenticated=True,
        )

        order_data = response.get("order", response)
        return Order(
            order_id=order_data.get("order_id", cid),
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
            status=OrderStatus(order_data.get("status", "resting").lower()),
            strategy_pod=strategy_pod,
            basket_id=basket_id,
        )

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancels a resting order."""
        return await self.request("DELETE", f"/portfolio/orders/{order_id}", authenticated=True)
