"""
24/7 Autonomous Crypto Quant Roller (Kraken Pro Spot + Hyperliquid Wallet Perps + Coinbase Institutional CVD).
100% Isolated from the Kalshi Sports Machine bankroll.

Features:
- Sends Mac Primary Heartbeat so GitHub Actions 24/7 Cloud Failover takes over automatically if Mac sleeps/shuts down.
- Polls live Hyperliquid & Coinbase Institutional order books, CVD, and hourly funding rates every 20 seconds.
- Runs Pod 1 (3-Leg Maker Baskets + Smart CVD Guillotine), Pod 2 (Liquidation Wick Catcher + Green-Out),
  and Pod 3 (Ethena-Grade Triple-Yield Staking + Funding + Basis Lock with Yield Rotator & <50ms Leg-Sync).
- Saves state to `data/crypto_quant_memory.json` for the `/crypto` dashboard and mobile alerts.
"""

import argparse
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
import httpx

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.crypto.orchestrator import CryptoEngineOrchestrator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] Crypto24x7Roller: %(message)s",
)
logger = logging.getLogger("Crypto24x7Roller")

CRYPTO_MEMORY_PATH = Path("data/crypto_quant_memory.json")
HEARTBEAT_TOPIC = "crypto-greg-hb-3fc522ce"


class AutonomousCryptoRoller:
    """24/7 Autonomous Execution & Shadow Loop for the Crypto Quant Machine."""

    def __init__(self):
        self.engine = CryptoEngineOrchestrator()
        self.wallet_address = os.getenv("HYPERLIQUID_WALLET_ADDRESS", "").strip()
        self.agent_private_key = os.getenv("HYPERLIQUID_AGENT_PRIVATE_KEY", "").strip()
        self.kraken_api_key = os.getenv("KRAKEN_API_KEY", "").strip()
        self.kraken_api_secret = os.getenv("KRAKEN_API_SECRET", "").strip()

        # Automatically switch to LIVE mode once Trade-Only keys are present and enabled
        self.is_live_configured = bool(
            (self.wallet_address and self.agent_private_key)
            or (self.kraken_api_key and self.kraken_api_secret)
        )
        self.cycle_count = 0

    async def send_mac_heartbeat(self) -> None:
        """Broadcasts Mac heartbeat so GitHub Cloud Failover knows local primary is awake."""
        if os.getenv("GITHUB_ACTIONS") == "true":
            return
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                await client.post(
                    f"https://ntfy.sh/{HEARTBEAT_TOPIC}",
                    content="mac-crypto-online",
                )
        except Exception:
            pass

    async def run_single_cycle(self) -> dict:
        self.cycle_count += 1
        await self.send_mac_heartbeat()

        snapshot = await self.engine.get_dashboard_snapshot("SOL")

        # 1. Check if Pod 2 has any bounce positions ready for automatic Green-Out
        greened = self.engine.trigger_green_out_all()
        if greened > 0:
            logger.info("💰 Pod 2 Auto Green-Out locked guaranteed profit on %d wick position(s)!", greened)

        # 2. Check if Pod 3 Yield Rotator found a higher persistent Triple-Yield APY coin
        rankings = snapshot.get("yield_rotator_rankings", [])
        current_locks = snapshot.get("pod3_funding_tail_lock", {}).get("locked_pairs", [])
        if rankings and current_locks:
            best_available_apy = rankings[0]["total_combined_apy"]
            lowest_locked_apy = min(p["annualized_funding_apy"] for p in current_locks)
            if best_available_apy > lowest_locked_apy + 2.0:
                rot = await self.engine.trigger_yield_rotation()
                if rot.get("rotated"):
                    logger.info(
                        "🚀 Pod 3 Speedboat Rotator: Shifted %s -> %s at %.2f%% Combined APY (%s)",
                        rot.get("exited_symbol"),
                        rot.get("entered_symbol"),
                        rot.get("new_combined_apy", 0.0),
                        rot.get("leg_sync_guard"),
                    )
                snapshot = await self.engine.get_dashboard_snapshot("SOL")

        # 3. Persist snapshot to `data/crypto_quant_memory.json`
        CRYPTO_MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "cycle_count": self.cycle_count,
            "execution_ready": self.is_live_configured,
            "snapshot": snapshot,
        }
        CRYPTO_MEMORY_PATH.write_text(json.dumps(payload, indent=2))

        b = snapshot["bankroll"]
        p3 = snapshot["pod3_funding_tail_lock"]
        logger.info(
            "Cycle #%d | Mode: %s | Equity: $%.2f (+$%.2f / +%.2f%%) | Fees Saved: +$%.2f | Pod 3 Lock APY: %.2f%%",
            self.cycle_count,
            "LIVE_KEYS_READY" if self.is_live_configured else "PAPER_SHADOW",
            b["current_equity_usd"],
            b["total_pnl_usd"],
            b["total_return_pct"],
            b["maker_fees_and_rebates_saved_usd"],
            p3["average_annualized_apy"],
        )
        return snapshot

    async def run_loop(self, duration_seconds: int = 0, interval_seconds: int = 20) -> None:
        start_ts = time.time()
        logger.info(
            "⚡ Starting 24/7 Autonomous Crypto Quant Roller (Interval: %ds, Duration: %s)",
            interval_seconds,
            f"{duration_seconds}s" if duration_seconds > 0 else "INFINITE_24_7",
        )
        while True:
            try:
                await self.run_single_cycle()
            except Exception as exc:
                logger.error("Error in crypto cycle: %s", exc)

            if duration_seconds > 0 and (time.time() - start_ts) >= duration_seconds:
                logger.info("Completed scheduled burst duration (%ds).", duration_seconds)
                break
            await asyncio.sleep(interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="24/7 Autonomous Crypto Quant Roller")
    parser.add_argument("--duration", type=int, default=0, help="Run duration in seconds (0 = infinite 24/7)")
    parser.add_argument("--interval", type=int, default=20, help="Scan interval in seconds")
    args = parser.parse_args()

    roller = AutonomousCryptoRoller()
    asyncio.run(roller.run_loop(duration_seconds=args.duration, interval_seconds=args.interval))


if __name__ == "__main__":
    main()
