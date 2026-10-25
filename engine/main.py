"""Kalshi Quant Multi-Strategy Engine Orchestrator and CLI.

Entry point for launching the quant engine, running background pods,
evaluating devigged lines, and serving the API dashboard.
"""

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Optional

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import typer
import uvicorn
from rich.console import Console
from rich.table import Table

from engine.api.server import app, set_engine_instance
from engine.config import Environment, TradingMode, settings
from engine.core.auth import KalshiAuthSigner
from engine.core.math_engine import MathEngine
from engine.exchange.kalshi_client import KalshiClient
from engine.exchange.simulator import KalshiSimulator
from engine.feeds.espn_hidden_api import ESPNHiddenApiClient
from engine.feeds.odds_api import OddsApiClient
from engine.feeds.open_meteo import OpenMeteoClient
from engine.notifications.telegram_bot import TelegramBotManager
from engine.pods.maker_momentum_basket import MakerMomentumBasketPod
from engine.pods.sharp_value_green_out import SharpValueGreenOutPod
from engine.pods.tail_lock_harvester import TailLockHarvesterPod
from engine.storage.bigquery_grader import BigQueryGrader
from engine.storage.duckdb_manager import DuckDBManager
from engine.storage.firebase_sync import FirebaseSyncManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("KalshiQuantEngine")
console = Console()
cli = typer.Typer(help="Kalshi Quant Multi-Strategy Trading Engine CLI")


class EngineOrchestrator:
    """Coordinates execution across all strategy pods, exchange connections, and storage."""

    def __init__(self, mode: Optional[TradingMode] = None):
        self.mode = mode or settings.TRADING_MODE
        self.is_running = False
        self.is_paused = False

        # 1. Auth Signer
        private_key = None
        key_id = settings.KALSHI_KEY_ID
        if settings.KALSHI_PRIVATE_KEY:
            private_key = settings.KALSHI_PRIVATE_KEY
        elif settings.KALSHI_PRIVATE_KEY_PATH and Path(settings.KALSHI_PRIVATE_KEY_PATH).exists():
            private_key = settings.KALSHI_PRIVATE_KEY_PATH
        else:
            # Generate local RSA key for paper/demo execution if not yet configured
            logger.info("Generating local ephemeral RSA key pair for PAPER execution.")
            priv, _ = KalshiAuthSigner.generate_key_pair(2048)
            private_key = priv

        self.signer = KalshiAuthSigner(key_id=key_id, private_key=private_key)

        # 2. Exchange & Simulator
        self.client = KalshiClient(signer=self.signer)
        self.simulator = KalshiSimulator() if self.mode == TradingMode.PAPER else None

        # 3. Storage & Analytics
        self.duckdb = DuckDBManager()
        self.firebase = FirebaseSyncManager()
        self.grader = BigQueryGrader()

        # 4. External Feeds
        self.odds_client = OddsApiClient()
        self.espn_client = ESPNHiddenApiClient()
        self.weather_client = OpenMeteoClient()

        # 5. Strategy Pods
        self.pod1 = MakerMomentumBasketPod(
            client=self.client,
            simulator=self.simulator,
            duckdb=self.duckdb,
            odds_client=self.odds_client,
            trading_mode=self.mode.value,
        )
        self.pod2 = SharpValueGreenOutPod(
            client=self.client,
            simulator=self.simulator,
            duckdb=self.duckdb,
            odds_client=self.odds_client,
            trading_mode=self.mode.value,
        )
        self.pod3 = TailLockHarvesterPod(
            client=self.client,
            simulator=self.simulator,
            duckdb=self.duckdb,
            espn_client=self.espn_client,
            trading_mode=self.mode.value,
        )

        # 6. Telegram Bot & Actions
        self.telegram = TelegramBotManager()
        self._setup_telegram_callbacks()

        # Inject into FastAPI server
        set_engine_instance(self)

    def _setup_telegram_callbacks(self) -> None:
        async def on_status():
            summary = self.get_status_summary()
            return f"Status: Mode={summary['trading_mode']}, Value=${summary['portfolio_value_cents']/100.0:.2f}"

        async def on_pause():
            self.pause()
            return "Engine paused across all pods."

        async def on_resume():
            self.resume()
            return "Engine resumed."

        async def on_green_out():
            count = await self.trigger_green_out_all()
            return f"Triggered green out on {count} positions."

        self.telegram.register_action("action_status", on_status)
        self.telegram.register_action("action_pause", on_pause)
        self.telegram.register_action("action_resume", on_resume)
        self.telegram.register_action("action_green_out_all", on_green_out)

    async def start(self) -> None:
        """Starts all pods and background scanner loop."""
        self.is_running = True
        logger.info("Initializing Kalshi Quant Multi-Strategy Engine [Mode: %s]...", self.mode.value)
        await self.pod1.start()
        await self.pod2.start()
        await self.pod3.start()

        # Initial Telegram status notification
        await self.telegram.send_status_alert(
            f"🚀 *Kalshi Quant Engine Online*\n"
            f"• *Mode:* `{self.mode.value}`\n"
            f"• *Pod 1:* Maker Momentum Baskets (`68¢-76¢`)\n"
            f"• *Pod 2:* Sharp Value Green Out (`+16¢ gain`)\n"
            f"• *Pod 3:* 4th Quarter Tail-Lock (`>= 99.2%`)\n"
            f"• *Maker Fees:* `$0.00`"
        )

    async def stop(self) -> None:
        self.is_running = False
        await self.pod1.stop()
        await self.pod2.stop()
        await self.pod3.stop()
        await self.client.close()
        self.duckdb.close()
        logger.info("Kalshi Quant Engine stopped.")

    def pause(self) -> None:
        self.is_paused = True
        self.pod1.pause()
        self.pod2.pause()
        self.pod3.pause()
        logger.info("Engine PAUSED.")

    def resume(self) -> None:
        self.is_paused = False
        self.pod1.resume()
        self.pod2.resume()
        self.pod3.resume()
        logger.info("Engine RESUMED.")

    async def run_scan_cycle(self) -> dict[str, Any]:
        """Runs one complete scan cycle across all 3 strategy pods."""
        if self.is_paused or not self.is_running:
            return {"status": "paused"}

        pod1_opps = await self.pod1.scan_opportunities()
        pod2_opps = await self.pod2.scan_opportunities()
        pod3_opps = await self.pod3.scan_opportunities()

        # Sync state to Firebase
        summary = self.get_status_summary()
        await self.firebase.sync_state("engine_state", summary)

        return {
            "pod1_momentum_baskets_created": len(pod1_opps),
            "pod2_sharp_value_entries": len(pod2_opps),
            "pod3_tail_lock_harvests": len(pod3_opps),
        }

    async def trigger_green_out_all(self) -> int:
        """Emergency or manual green-out trigger across all eligible Pod 2 positions."""
        count = 0
        for ticker, pos in list(self.pod2.active_positions.items()):
            if not pos.get("is_greened_out"):
                current_bid = pos.get("current_price", pos["entry_price"])
                await self.pod2._execute_midgame_green_out(ticker, pos, current_bid=current_bid)
                count += 1
        return count

    def get_status_summary(self) -> dict[str, Any]:
        sim_data = self.simulator.get_balance() if self.simulator else {"cash_balance_cents": 0, "portfolio_value_cents": 0}
        return {
            "status": "paused" if self.is_paused else ("running" if self.is_running else "stopped"),
            "trading_mode": self.mode.value,
            "kalshi_env": settings.KALSHI_ENV.value,
            "cash_balance_cents": sim_data.get("cash_balance_cents", 0),
            "portfolio_value_cents": sim_data.get("portfolio_value_cents", 0),
            "resting_orders_count": sim_data.get("resting_orders_count", 0),
            "maker_fees_saved_cents": self.simulator.total_maker_fees_saved_cents if self.simulator else 0,
            "taker_fees_paid_cents": self.simulator.total_taker_fees_paid_cents if self.simulator else 0,
            "pod1_baskets_count": len(self.pod1.active_baskets),
            "pod2_positions_count": len(self.pod2.active_positions),
            "pod3_harvests_count": len(self.pod3.active_harvests),
        }

    def get_performance_grades(self) -> dict[str, Any]:
        """Runs nightly self-learning Brier Score and CLV grader across pods."""
        # Simulated sample trades for demonstration
        sample_trades = [
            {"predicted_prob": 0.74, "outcome": "win", "entry_price_cents": 71, "closing_sharp_prob": 0.75, "pnl_cents": 190},
            {"predicted_prob": 0.70, "outcome": "win", "entry_price_cents": 69, "closing_sharp_prob": 0.73, "pnl_cents": 210},
            {"predicted_prob": 0.68, "outcome": "loss", "entry_price_cents": 67, "closing_sharp_prob": 0.65, "pnl_cents": -670},
            {"predicted_prob": 0.96, "outcome": "win", "entry_price_cents": 95, "closing_sharp_prob": 0.99, "pnl_cents": 250},
        ]
        return {
            "pod1_grade": self.grader.grade_pod_performance("maker_momentum_basket", sample_trades[:2]),
            "pod2_grade": self.grader.grade_pod_performance("sharp_value_green_out", [sample_trades[2]]),
            "pod3_grade": self.grader.grade_pod_performance("tail_lock_harvester", [sample_trades[3]]),
        }


# -----------------------------------------------------------------------------
# CLI Commands
# -----------------------------------------------------------------------------
@cli.command()
def start(
    mode: str = typer.Option("PAPER", "--mode", "-m", help="Trading mode: PAPER or LIVE"),
    port: int = typer.Option(8000, "--port", "-p", help="FastAPI Server port"),
    scan_interval: int = typer.Option(15, "--interval", "-i", help="Scan loop interval in seconds"),
):
    """Starts the full Kalshi Quant Engine and dashboard API server."""
    console.print(f"[bold green]Starting Kalshi Quant Engine in {mode.upper()} mode on port {port}...[/bold green]")
    orchestrator = EngineOrchestrator(mode=TradingMode(mode.upper()))

    async def main_loop():
        await orchestrator.start()
        # Launch background scanner loop
        async def background_scanner():
            while orchestrator.is_running:
                try:
                    await orchestrator.run_scan_cycle()
                except Exception as err:
                    logger.error("Scan error: %s", err)
                await asyncio.sleep(scan_interval)

        asyncio.create_task(background_scanner())

        # Start FastAPI dashboard server
        config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
        server = uvicorn.Server(config)
        await server.serve()

    asyncio.run(main_loop())


@cli.command()
def scan():
    """Runs a single scan cycle across all 3 strategy pods and displays table."""
    async def run_single_scan():
        orch = EngineOrchestrator(mode=TradingMode.PAPER)
        await orch.start()
        res = await orch.run_scan_cycle()
        summary = orch.get_status_summary()

        table = Table(title="Kalshi Quant Strategy Pod Scan Results")
        table.add_column("Strategy Pod", style="cyan")
        table.add_column("Price Zone", style="magenta")
        table.add_column("Action / Rule", style="yellow")
        table.add_column("Active Items", style="green")

        table.add_row("Pod 1: Maker Momentum", "68¢ - 76¢", "Resting Dip Bid + 90¢ TP + Guillotine", str(summary["pod1_baskets_count"]))
        table.add_row("Pod 2: Sharp Value", "38¢ - 58¢", "+16¢ Mid-Game Equal Green-Out", str(summary["pod2_positions_count"]))
        table.add_row("Pod 3: Tail-Lock Harvester", "95¢ - 97¢", "ESPN Clock & Possession >= 99.2%", str(summary["pod3_harvests_count"]))

        console.print(table)
        await orch.stop()

    asyncio.run(run_single_scan())


@cli.command()
def devig(
    home_odds: float = typer.Argument(..., help="Decimal or American odds for Home (e.g. 1.50 or -200)"),
    away_odds: float = typer.Argument(..., help="Decimal or American odds for Away (e.g. 2.70 or +170)"),
):
    """Strips sportsbook vig using Shin and Multiplicative methods."""
    p_shin_h, p_shin_a = MathEngine.devig_two_way(home_odds, away_odds, method="shin")
    p_mult_h, p_mult_a = MathEngine.devig_two_way(home_odds, away_odds, method="multiplicative")

    table = Table(title="Sharp Vegas De-Vigging (Pinnacle/Circa)")
    table.add_column("Method", style="cyan")
    table.add_column("Home Implied Prob", style="green")
    table.add_column("Away Implied Prob", style="green")
    table.add_column("Sum Guarantee", style="yellow")

    table.add_row("Shin (Insider Model)", f"{p_shin_h * 100:.2f}%", f"{p_shin_a * 100:.2f}%", f"{(p_shin_h + p_shin_a) * 100:.2f}%")
    table.add_row("Multiplicative (Proportional)", f"{p_mult_h * 100:.2f}%", f"{p_mult_a * 100:.2f}%", f"{(p_mult_h + p_mult_a) * 100:.2f}%")

    console.print(table)


@cli.command()
def generate_keys(
    output_dir: str = typer.Option(".", "--output", "-o", help="Directory to save keys"),
):
    """Generates RSA-PSS API key pair for Kalshi."""
    priv, pub = KalshiAuthSigner.generate_key_pair(2048)
    pem_priv = KalshiAuthSigner.export_private_key_pem(priv)
    pem_pub = KalshiAuthSigner.export_public_key_pem(pub)

    priv_path = Path(output_dir) / "kalshi_private_key.pem"
    pub_path = Path(output_dir) / "kalshi_public_key.pem"

    priv_path.write_text(pem_priv)
    pub_path.write_text(pem_pub)

    console.print(f"[bold green]RSA Keys successfully generated:[/bold green]")
    console.print(f"• Private Key: {priv_path.resolve()}")
    console.print(f"• Public Key:  {pub_path.resolve()}")
    console.print("Upload the public key to Kalshi Account API Settings.")


if __name__ == "__main__":
    cli()
