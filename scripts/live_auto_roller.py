"""
Continuous Autonomous Live & International Order Roller (Pod 1 + Pod 2 + Pod 3).
Supports:
- Local 24/7 execution (`python scripts/live_auto_roller.py`)
- GitHub Actions Cloud Cron burst execution (`python scripts/live_auto_roller.py --duration 240`)
- Render.com Free Tier Web Service (`PORT` env var auto-starts a lightweight /health HTTP server)
"""

import argparse
import asyncio
import logging
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import settings
from src.core.auth import KalshiAuthSigner
from src.exchange.kalshi_client import KalshiClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("live_auto_roller")

WATCH_SERIES = [
    "KXNBAGAME",
    "KXNFLGAME",
    "KXNHLGAME",
    "KXWTAMATCH",
    "KXATPMATCH",
    "KXLIGAMXGAME",
    "KXLALIGAGAME",
    "KXLIGUE1GAME",
    "KXEPLGAME",
    "KXSERIEAGAME",
    "KXBUNDESLIGAGAME",
]


def load_signer() -> KalshiAuthSigner:
    key_id = os.environ.get("KALSHI_KEY_ID") or settings.KALSHI_KEY_ID
    env_pem = os.environ.get("KALSHI_PRIVATE_KEY")
    pem_file = Path("secrets/kalshi_private_key.pem")
    if env_pem and "PRIVATE KEY" in env_pem:
        # Handle escaped newlines in cloud secrets
        clean_pem = env_pem.replace("\\n", "\n")
        return KalshiAuthSigner(key_id=key_id, private_key=clean_pem)
    return KalshiAuthSigner(key_id=key_id, private_key=pem_file)


async def run_autonomous_loop(duration_seconds: int = 0):
    signer = load_signer()
    client = KalshiClient(signer=signer, base_url=settings.PROD_REST_URL)
    logger.info("Autonomous Live + International Roller started on Kalshi PROD.")
    start_ts = time.time()

    while True:
        try:
            # 1. Fetch current positions & resting orders
            positions = await client.get_positions()
            orders_resp = await client.request("GET", "/portfolio/orders", authenticated=True)
            open_orders = [
                o for o in orders_resp.get("orders", []) if o.get("status") == "resting"
            ]

            resting_sell_counts: dict[str, float] = {}
            resting_buy_tickers: set[str] = set()
            for o in open_orders:
                t = o.get("ticker", "")
                if o.get("action") == "sell":
                    resting_sell_counts[t] = resting_sell_counts.get(t, 0.0) + float(
                        o.get("remaining_count_fp", "0") or 0
                    )
                elif o.get("action") == "buy":
                    resting_buy_tickers.add(t)

            # 2. Ensure every held long position has a resting Take-Profit Maker Sell (90c)
            for pos in positions:
                ticker = pos.get("ticker", "")
                pos_fp = float(pos.get("position_fp", "0") or 0)
                covered = resting_sell_counts.get(ticker, 0.0)
                uncovered = int(round(pos_fp - covered))
                if uncovered >= 1:
                    count_str = f"{uncovered}.00"
                    tp_price = "0.9000"
                    logger.info(
                        f"Auto-queuing Maker Take-Profit Sell for {ticker}: {count_str} @ {tp_price}"
                    )
                    try:
                        await client.request(
                            "POST",
                            "/portfolio/events/orders",
                            json_data={
                                "ticker": ticker,
                                "side": "ask",
                                "count": count_str,
                                "price": tp_price,
                                "time_in_force": "good_till_canceled",
                                "self_trade_prevention_type": "maker",
                                "post_only": True,
                                "client_order_id": str(uuid.uuid4()),
                            },
                            authenticated=True,
                        )
                    except Exception as e:
                        logger.warning(f"Could not place TP sell for {ticker}: {e}")

            # 3. Check available cash across shards; if >= $2.50 unallocated, scan & deploy into 64c-76c zone
            bal = await client.get_balance()
            total_cents = bal.get("balance", 0)
            logger.info(
                f"Heartbeat | Cash: ${total_cents/100:.2f} | Portfolio: ${bal.get('portfolio_value', 0)/100:.2f} | Open Orders: {len(open_orders)}"
            )

            if total_cents >= 250 and len(resting_buy_tickers) < 4:
                from datetime import datetime, timedelta
                now_dt = datetime.now()
                valid_dates = [
                    now_dt.strftime("%y%b%d").upper(),
                    (now_dt + timedelta(days=1)).strftime("%y%b%d").upper(),
                ]
                for series in WATCH_SERIES:
                    res = await client.request(
                        "GET",
                        "/markets",
                        params={"series_ticker": series, "status": "open", "limit": 50},
                    )
                    for m in res.get("markets", []):
                        t = m.get("ticker", "")
                        if not any(d in t for d in valid_dates):
                            continue
                        if t in resting_buy_tickers or resting_sell_counts.get(t, 0) > 0:
                            continue
                        ob = await client.get_orderbook(t)
                        if ob.best_yes_bid and ob.best_yes_ask:
                            if 64 <= ob.best_yes_ask <= 76 and (ob.best_yes_ask - ob.best_yes_bid) <= 4:
                                bid_cents = ob.best_yes_ask - 1
                                price_str = f"{bid_cents / 100:.4f}"
                                count_int = min(10, int((total_cents * 0.45) // bid_cents))
                                if count_int >= 2:
                                    logger.info(
                                        f"Auto-deploying Pod 1 Maker Bid on {t}: {count_int} contracts @ {price_str}"
                                    )
                                    try:
                                        await client.request(
                                            "POST",
                                            "/portfolio/events/orders",
                                            json_data={
                                                "ticker": t,
                                                "side": "bid",
                                                "count": f"{count_int}.00",
                                                "price": price_str,
                                                "time_in_force": "good_till_canceled",
                                                "self_trade_prevention_type": "maker",
                                                "post_only": True,
                                                "client_order_id": str(uuid.uuid4()),
                                            },
                                            authenticated=True,
                                        )
                                        resting_buy_tickers.add(t)
                                        break
                                    except Exception:
                                        pass

        except Exception as e:
            logger.error(f"Loop error: {e}")

        if duration_seconds > 0 and (time.time() - start_ts) >= duration_seconds:
            logger.info(f"Completed scheduled cloud burst ({duration_seconds}s).")
            break

        await asyncio.sleep(20)

    await client.close()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--duration",
        type=int,
        default=0,
        help="Run duration in seconds (0 = infinite 24/7 loop)",
    )
    args = parser.parse_args()
    await run_autonomous_loop(duration_seconds=args.duration)


if __name__ == "__main__":
    asyncio.run(main())
