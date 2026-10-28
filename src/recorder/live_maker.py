"""Hard-fenced $50 Micro-Live Maker executor.

Five non-negotiable safety invariants:
  1. Cash floor: never place an order if (Kalshi cash balance - order cost) < CASH_FLOOR_DOLLARS ($308.72),
     nor if active live_orders cost + order cost > MAX_BANKROLL_DOLLARS ($50.00).
  2. Per-game cap: at most 1 order per event_ticker ever, and at most MAX_PER_GAME_DOLLARS ($10.00) per game.
  3. Maker-only: post_only=True at a resting bid strictly below Kalshi's current ask ($0.00 maker fee),
     only when (de-vigged Pinnacle fair - maker_price) >= MIN_MAKER_EDGE (0.020).
  4. Pre-game auto-cancel: hardware exchange expiration_ts = commence_time - 120s and cancel_on_pause=True,
     so Kalshi's exchange cancels any unfilled bid 2 minutes before tip-off.
  5. Hold to settlement: never places mid-game taker stop-losses or re-entries.
"""

from __future__ import annotations

import base64
import logging
import math
import os
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import serialization

KALSHI_TRADE_BASE = "https://api.elections.kalshi.com/trade-api/v2"
CASH_FLOOR_DOLLARS = 308.72      # $358.72 starting cash - $50.00 max sandbox allocation
MAX_BANKROLL_DOLLARS = 50.00     # Total max committed across the $50 sandbox
MAX_PER_GAME_DOLLARS = 10.00     # Max $10 per game (up to 5 games)
MIN_MAKER_EDGE = 0.020           # Require >= 2.0c Maker edge over de-vigged Pinnacle
MIN_PRICE = 0.35                 # Avoid low-liquidity tail longshots
MAX_PRICE = 0.88                 # Cap max price at 88c
CANCEL_BEFORE_START_S = 120      # Exchange-enforced expiration 2 minutes before tip-off

log = logging.getLogger("live_maker")


class LiveMaker:
    def __init__(self, key_id: str, private_key_pem: bytes, cash_floor: float = CASH_FLOOR_DOLLARS):
        self.key_id = key_id
        self.pk = serialization.load_pem_private_key(private_key_pem, password=None)
        self.cash_floor = cash_floor

    @classmethod
    def from_env(cls) -> LiveMaker | None:
        if os.environ.get("LIVE_MAKER_ENABLED", "").lower() not in ("1", "true", "yes"):
            return None
        key_id = (os.environ.get("KALSHI_API_KEY_ID") or os.environ.get("KALSHI_KEY_ID") or "").strip()
        pem_env = os.environ.get("KALSHI_PRIVATE_KEY_PEM", "").strip()
        pem_path = Path(os.environ.get("KALSHI_PRIVATE_KEY_PATH", "secrets/kalshi_private_key.pem"))
        if pem_env:
            pem_bytes = pem_env.replace("\\n", "\n").encode()
        elif pem_path.exists():
            pem_bytes = pem_path.read_bytes()
        else:
            log.warning("LIVE_MAKER_ENABLED is set, but no private key found; staying in PAPER mode")
            return None
        if not key_id:
            log.warning("LIVE_MAKER_ENABLED is set, but KALSHI_API_KEY_ID missing; staying in PAPER mode")
            return None
        floor = float(os.environ.get("LIVE_CASH_FLOOR_DOLLARS", str(CASH_FLOOR_DOLLARS)))
        return cls(key_id, pem_bytes, cash_floor=floor)

    def _headers(self, method: str, path: str) -> dict[str, str]:
        clean_path = urlsplit(path).path
        ts = str(int(time.time() * 1000))
        msg = f"{ts}{method.upper()}{clean_path}".encode()
        sig = base64.b64encode(self.pk.sign(msg)).decode()
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "Content-Type": "application/json",
        }

    async def get_Portfolio_state(self, client: httpx.AsyncClient) -> tuple[float, set[str]]:
        """Return (available_cash_dollars, set_of_tickers_already_in_orders_or_positions)."""
        p_bal = "/trade-api/v2/portfolio/balance"
        r_bal = await client.get(f"https://api.elections.kalshi.com{p_bal}", headers=self._headers("GET", p_bal))
        r_bal.raise_for_status()
        bal_data = r_bal.json()
        cash = float(bal_data.get("balance", 0)) / 100.0

        active_tickers: set[str] = set()
        p_ord = "/trade-api/v2/portfolio/orders"
        r_ord = await client.get(
            f"https://api.elections.kalshi.com{p_ord}",
            params={"status": "resting"},
            headers=self._headers("GET", p_ord),
        )
        r_ord.raise_for_status()
        for o in r_ord.json().get("orders", []):
            if o.get("ticker"):
                active_tickers.add(o["ticker"])

        p_pos = "/trade-api/v2/portfolio/positions"
        r_pos = await client.get(
            f"https://api.elections.kalshi.com{p_pos}",
            params={"count_filter": "position"},
            headers=self._headers("GET", p_pos),
        )
        r_pos.raise_for_status()
        for mp in r_pos.json().get("market_positions", []):
            if mp.get("ticker") and float(mp.get("position_fp", mp.get("position", 0)) or 0) != 0:
                active_tickers.add(mp["ticker"])

        return cash, active_tickers

    @staticmethod
    def size_order(maker_price: float, ask: float, fair: float, cash_available: float,
                   cash_floor: float = CASH_FLOOR_DOLLARS,
                   db_committed: float = 0.0) -> tuple[int, str]:
        """Pure sizing & guardrail check. Returns (contracts, reason_if_zero)."""
        if not (MIN_PRICE <= maker_price <= MAX_PRICE):
            return 0, f"price {maker_price:.2f} outside [{MIN_PRICE:.2f}, {MAX_PRICE:.2f}]"
        if maker_price >= ask:
            return 0, f"maker_price {maker_price:.2f} >= ask {ask:.2f} (would cross spread)"
        edge = fair - maker_price
        if edge < MIN_MAKER_EDGE - 1e-9:
            return 0, f"edge {edge:+.3f} < {MIN_MAKER_EDGE:.3f}"
        room_floor = cash_available - cash_floor
        room_cap = MAX_BANKROLL_DOLLARS - db_committed
        budget = min(MAX_PER_GAME_DOLLARS, room_floor, room_cap)
        if budget < maker_price:
            return 0, f"insufficient sandbox budget (cash={cash_available:.2f}, floor={cash_floor:.2f}, committed={db_committed:.2f})"
        contracts = int(math.floor((budget + 1e-9) / maker_price))
        if contracts < 1:
            return 0, "contracts < 1"
        return contracts, "ok"

    async def place_resting_buy(self, client: httpx.AsyncClient, conn, ticker: str, event_ticker: str,
                                series: str, commence_iso: str, commence_epoch: int,
                                maker_price: float, ask: float, fair: float, now_iso: str) -> dict | None:
        existing = conn.execute("SELECT 1 FROM live_orders WHERE event_ticker=?", (event_ticker,)).fetchone()
        if existing:
            return None

        db_committed = float(conn.execute(
            "SELECT COALESCE(SUM(contracts * maker_price), 0.0) FROM live_orders WHERE status IN ('RESTING','FILLED')"
        ).fetchone()[0] or 0.0)

        cash, active_tickers = await self.get_Portfolio_state(client)
        if any(tk.startswith(event_ticker) for tk in active_tickers):
            log.info("LiveMaker skip %s: event already active on exchange", event_ticker)
            return None

        contracts, reason = self.size_order(maker_price, ask, fair, cash, self.cash_floor, db_committed)
        if contracts < 1:
            log.info("LiveMaker skip %s: %s", ticker, reason)
            return None

        exp_ts = int(commence_epoch) - CANCEL_BEFORE_START_S
        if exp_ts <= int(time.time()) + 60:
            return None

        # Kalshi Create Order V2 (POST /portfolio/events/orders): side "bid" == buy YES.
        payload = {
            "ticker": ticker,
            "side": "bid",
            "count": f"{contracts}.00",
            "price": f"{maker_price:.4f}",
            "time_in_force": "good_till_canceled",
            "expiration_time": exp_ts,
            "post_only": True,
            "cancel_order_on_pause": True,
            "self_trade_prevention_type": "maker",
            "client_order_id": f"mk50-{uuid.uuid4().hex[:16]}",
        }
        path = "/trade-api/v2/portfolio/events/orders"
        r = await client.post(
            f"https://api.elections.kalshi.com{path}",
            json=payload,
            headers=self._headers("POST", path),
        )
        if r.status_code not in (200, 201):
            log.error("LiveMaker order rejected %s (%s): %s", ticker, r.status_code, r.text[:200])
            return None
        order = r.json()
        order = order.get("order", order)
        oid = order.get("order_id", "")
        filled = float(order.get("fill_count", 0) or 0)
        status = "FILLED" if filled >= contracts else "RESTING"
        conn.execute(
            """INSERT INTO live_orders
               (placed_ts, order_id, ticker, event_ticker, series, commence_time,
                contracts, maker_price, fair_at_place, edge_at_place, expiration_ts, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now_iso, oid, ticker, event_ticker, series, commence_iso,
             contracts, maker_price, fair, fair - maker_price, exp_ts, status),
        )
        conn.commit()
        log.info("LiveMaker PLACED %s: %d cts @ $%.2f ($%.2f cost, fair=%.3f, edge=+%.3f, cash_left=$%.2f)",
                 ticker, contracts, maker_price, contracts * maker_price, fair, fair - maker_price,
                 cash - contracts * maker_price)
        return order
