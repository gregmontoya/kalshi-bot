"""
Continuous Autonomous Live & Worldwide Order Roller.
Upgrades per user strategy:
1. Strictly Immediate / Live Worldwide Plays Only: Sorts all global sports markets by soonest expiration/close time and only enters games happening right now or within the next few hours (never locking cash in tomorrow night's games).
2. Hold Winners to 96c-$1.00 + Trailing Drop Protection:
   - Places Maker Take-Profit at 96c (captures almost the entire $1.00 payout while freeing cash immediately upon game end).
   - Tracks peak bid price for each position: if a winning position (>80c) drops sharply below 64c, or if any position drops <= 48c (Pod 1 Guillotine), fires an immediate defensive exit to protect bankroll.
3. Dynamic Bankroll Compounding: Automatically scales contract size proportionally as total equity grows.
"""

import argparse
import asyncio
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
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

# Comprehensive Worldwide & Domestic Live Sports Series
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
    "KXTTSTARMATCH",
    "KXCS2GAMES",
    "KXLOLGAMES",
    "KXWKBLGAME",
    "KXFIBAGAME",
]


def load_signer() -> KalshiAuthSigner:
    key_id = os.environ.get("KALSHI_KEY_ID") or settings.KALSHI_KEY_ID
    env_pem = os.environ.get("KALSHI_PRIVATE_KEY")
    pem_file = Path("secrets/kalshi_private_key.pem")
    if env_pem and "PRIVATE KEY" in env_pem:
        clean_pem = env_pem.replace("\\n", "\n")
        return KalshiAuthSigner(key_id=key_id, private_key=clean_pem)
    return KalshiAuthSigner(key_id=key_id, private_key=pem_file)


async def run_autonomous_loop(duration_seconds: int = 0):
    signer = load_signer()
    client = KalshiClient(signer=signer, base_url=settings.PROD_REST_URL)
    logger.info("Autonomous Worldwide Live Roller started (96c Harvest + Drop Stop + Soonest-Only).")
    start_ts = time.time()
    peak_bids: dict[str, int] = {}

    while True:
        try:
            positions = await client.get_positions()
            orders_resp = await client.request("GET", "/portfolio/orders", authenticated=True)
            open_orders = [
                o for o in orders_resp.get("orders", []) if o.get("status") == "resting"
            ]

            resting_sell_orders: dict[str, list[dict]] = {}
            resting_sell_counts: dict[str, float] = {}
            resting_buy_tickers: set[str] = set()
            for o in open_orders:
                t = o.get("ticker", "")
                if o.get("action") == "sell":
                    resting_sell_orders.setdefault(t, []).append(o)
                    resting_sell_counts[t] = resting_sell_counts.get(t, 0.0) + float(
                        o.get("remaining_count_fp", "0") or 0
                    )
                elif o.get("action") == "buy":
                    resting_buy_tickers.add(t)

            # 1. Manage Open Positions: Trailing Stop / Guillotine (<48c) + 96c Harvest Queue
            for pos in positions:
                ticker = pos.get("ticker", "")
                pos_fp = float(pos.get("position_fp", "0") or 0)
                if pos_fp < 1.0:
                    continue

                ob = await client.get_orderbook(ticker)
                cur_bid = ob.best_yes_bid or 0
                if cur_bid > peak_bids.get(ticker, 0):
                    peak_bids[ticker] = cur_bid

                # If position is a far-away tomorrow night NFL game (e.g. DALPHI), exit at current ask/bid to free cash for live games
                if "26OCT26DALPHI" in ticker and cur_bid >= 60:
                    for so in resting_sell_orders.get(ticker, []):
                        await client.request(
                            "DELETE", f"/portfolio/events/orders/{so['order_id']}", authenticated=True
                        )
                    exit_cents = max(cur_bid, (ob.best_yes_ask or cur_bid) - 1)
                    logger.info(f"Freeing capital from tomorrow's game {ticker} @ {exit_cents}c")
                    await client.request(
                        "POST",
                        "/portfolio/events/orders",
                        json_data={
                            "ticker": ticker,
                            "side": "ask",
                            "count": f"{int(pos_fp)}.00",
                            "price": f"{exit_cents / 100:.4f}",
                            "time_in_force": "good_till_canceled",
                            "self_trade_prevention_type": "maker",
                            "post_only": True,
                            "client_order_id": str(uuid.uuid4()),
                        },
                        authenticated=True,
                    )
                    continue

                # Check Guillotine (<48c) or Trailing Drop Stop (peaked >=82c, dropped <=64c)
                peaked = peak_bids.get(ticker, cur_bid)
                if (0 < cur_bid <= 48) or (peaked >= 82 and 0 < cur_bid <= 64):
                    logger.warning(
                        f"DROP STOP TRIGGERED on {ticker} (cur_bid={cur_bid}c, peak={peaked}c). Exiting position!"
                    )
                    for so in resting_sell_orders.get(ticker, []):
                        await client.request(
                            "DELETE", f"/portfolio/events/orders/{so['order_id']}", authenticated=True
                        )
                    await client.request(
                        "POST",
                        "/portfolio/events/orders",
                        json_data={
                            "ticker": ticker,
                            "side": "ask",
                            "count": f"{int(pos_fp)}.00",
                            "price": f"{max(1, cur_bid) / 100:.4f}",
                            "time_in_force": "immediate_or_cancel",
                            "client_order_id": str(uuid.uuid4()),
                        },
                        authenticated=True,
                    )
                    continue

                # Ensure 96c Harvest Sell is queued for uncovered contracts
                covered = resting_sell_counts.get(ticker, 0.0)
                uncovered = int(round(pos_fp - covered))
                if uncovered >= 1:
                    count_str = f"{uncovered}.00"
                    tp_price = "0.9600"
                    logger.info(
                        f"Auto-queuing 96c Harvest Sell for {ticker}: {count_str} @ {tp_price}"
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
                        logger.warning(f"Could not place 96c sell for {ticker}: {e}")

            # 2. Dynamic Bankroll Scaling into SOONEST Worldwide Games (64c-78c zone)
            bal = await client.get_balance()
            total_cents = bal.get("balance", 0)
            port_cents = bal.get("portfolio_value", 0)
            equity_cents = total_cents + port_cents
            logger.info(
                f"Heartbeat | Cash: ${total_cents/100:.2f} | Portfolio: ${port_cents/100:.2f} | Total Equity: ${equity_cents/100:.2f}"
            )

            if total_cents >= 250 and len(resting_buy_tickers) < 6:
                candidates = []
                now_utc = datetime.now(timezone.utc)
                for series in WATCH_SERIES:
                    try:
                        res = await client.request(
                            "GET",
                            "/markets",
                            params={"series_ticker": series, "status": "open", "limit": 40},
                        )
                        for m in res.get("markets", []):
                            t = m.get("ticker", "")
                            if "26OCT25" not in t:
                                # Prioritize immediate/live games on current slate first
                                continue
                            if t in resting_buy_tickers or resting_sell_counts.get(t, 0) > 0:
                                continue
                            close_str = m.get("close_time") or m.get("expiration_time") or "2099"
                            candidates.append((close_str, t))
                    except Exception:
                        pass

                # Sort strictly by SOONEST closing/settling game worldwide
                candidates.sort(key=lambda x: x[0])

                for _, t in candidates[:25]:
                    ob = await client.get_orderbook(t)
                    if ob.best_yes_bid and ob.best_yes_ask:
                        if 64 <= ob.best_yes_ask <= 78 and (ob.best_yes_ask - ob.best_yes_bid) <= 4:
                            bid_cents = ob.best_yes_ask - 1
                            price_str = f"{bid_cents / 100:.4f}"
                            # Dynamic bankroll sizing: ~25% of total equity per play
                            target_stake_cents = max(250, int(equity_cents * 0.25))
                            alloc_cents = min(total_cents, target_stake_cents)
                            count_int = int(alloc_cents // bid_cents)
                            if count_int >= 2:
                                logger.info(
                                    f"Auto-deploying Worldwide Maker Bid on {t}: {count_int} contracts @ {price_str}"
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
                                    total_cents -= count_int * bid_cents
                                    if total_cents < 250:
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
    parser.add_argument("--duration", type=int, default=0)
    args = parser.parse_args()
    await run_autonomous_loop(duration_seconds=args.duration)


if __name__ == "__main__":
    asyncio.run(main())
