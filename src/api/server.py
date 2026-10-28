"""FastAPI Backend Server for Kalshi Quant Engine Dashboard.

Provides REST & WebSocket endpoints for the Astro.js PWA Dashboard.
"""

import json
import os
from pathlib import Path
from typing import Any, Optional
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from src.config import settings
from src.core.orderbook import KalshiOrderBookEngine
from src.crypto.orchestrator import CryptoEngineOrchestrator

app = FastAPI(
    title="Kalshi & Crypto Quant Multi-Strategy Engine API",
    version="2.0.0",
    description="Quantitative algorithmic trading engine for Kalshi Sports & Standalone Crypto Pods",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

engine_instance: Optional[Any] = None
crypto_engine_instance: CryptoEngineOrchestrator = CryptoEngineOrchestrator()


def set_engine_instance(inst: Any) -> None:
    global engine_instance
    engine_instance = inst


@app.get("/api/status")
async def get_status() -> dict[str, Any]:
    if not engine_instance:
        return {"status": "starting", "trading_mode": settings.TRADING_MODE.value}
    return engine_instance.get_status_summary()


@app.get("/api/pods")
async def get_pods() -> dict[str, Any]:
    if not engine_instance:
        return {}
    return {
        "pod1_momentum_basket": engine_instance.pod1.get_state(),
        "pod2_sharp_value_green_out": engine_instance.pod2.get_state(),
        "pod3_tail_lock_harvester": engine_instance.pod3.get_state(),
    }


@app.get("/api/baskets")
async def get_baskets() -> list[dict[str, Any]]:
    if not engine_instance:
        return []
    baskets = engine_instance.duckdb.get_all_baskets()
    return [b.model_dump() for b in baskets]


@app.get("/api/orderbook/{ticker}")
async def get_orderbook(ticker: str) -> dict[str, Any]:
    if not engine_instance:
        raise HTTPException(status_code=503, detail="Engine not initialized")
    try:
        book = await engine_instance.client.get_orderbook(ticker)
        depth = KalshiOrderBookEngine.get_market_depth(book)
        imbalance = KalshiOrderBookEngine.check_whale_tape_imbalance(book)
        return {
            "ticker": ticker,
            "best_yes_bid": book.best_yes_bid,
            "best_no_bid": book.best_no_bid,
            "best_yes_ask": book.best_yes_ask,
            "best_no_ask": book.best_no_ask,
            "yes_spread": book.yes_spread,
            "no_spread": book.no_spread,
            "yes_midpoint": book.yes_midpoint,
            "yes_bids": [b.model_dump() for b in book.yes_bids],
            "no_bids": [b.model_dump() for b in book.no_bids],
            "synthesized_yes_asks": [a.model_dump() for a in book.get_synthesized_yes_asks()],
            "synthesized_no_asks": [a.model_dump() for a in book.get_synthesized_no_asks()],
            "depth_metrics": depth,
            "whale_tape": imbalance,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/feeds/sharp")
async def get_sharp_odds() -> list[dict[str, Any]]:
    if not engine_instance:
        return []
    odds = await engine_instance.odds_client.get_sharp_odds()
    return [o.model_dump() for o in odds]


@app.get("/api/feeds/weather")
async def get_weather(stadium: str = "arrowhead") -> dict[str, Any]:
    if not engine_instance:
        return {}
    weather = await engine_instance.weather_client.get_stadium_weather(stadium)
    return weather.model_dump()


@app.get("/api/risk/circuit_breaker")
async def get_circuit_breaker() -> dict[str, Any]:
    if not engine_instance:
        return {}
    cb = engine_instance.circuit_breaker
    return {
        "is_tripped": cb.is_tripped,
        "trip_reason": cb.trip_reason,
        "daily_realized_loss_dollars": cb.daily_realized_loss_cents / 100.0,
        "max_daily_drawdown_dollars": cb.max_daily_drawdown_dollars,
        "max_portfolio_exposure_pct": cb.max_portfolio_exposure_pct * 100.0,
    }


@app.post("/api/actions/pause")
async def pause_engine() -> dict[str, str]:
    if not engine_instance:
        raise HTTPException(status_code=503, detail="Engine not ready")
    engine_instance.pause()
    return {"status": "paused"}


@app.post("/api/actions/resume")
async def resume_engine() -> dict[str, str]:
    if not engine_instance:
        raise HTTPException(status_code=503, detail="Engine not ready")
    engine_instance.resume()
    return {"status": "resumed"}


@app.post("/api/actions/scan")
async def force_scan() -> dict[str, Any]:
    if not engine_instance:
        raise HTTPException(status_code=503, detail="Engine not ready")
    results = await engine_instance.run_scan_cycle()
    return {"status": "success", "results": results}


@app.post("/api/actions/green_out_all")
async def green_out_all() -> dict[str, Any]:
    if not engine_instance:
        raise HTTPException(status_code=503, detail="Engine not ready")
    count = await engine_instance.trigger_green_out_all()
    return {"status": "success", "greened_out_count": count}


@app.get("/api/analytics/grader")
async def get_grader_report() -> dict[str, Any]:
    if not engine_instance:
        return {}
    return engine_instance.get_performance_grades()


@app.get("/api/live-ledger")
async def get_live_ledger() -> dict[str, Any]:
    mem_path = Path(__file__).resolve().parent.parent.parent / "data" / "self_learning_memory.json"
    mem = {}
    if mem_path.exists():
        try:
            mem = json.loads(mem_path.read_text())
        except Exception:
            pass
    hist = mem.get("equity_history", [])
    latest = hist[-1] if hist else {"equity": 26.18, "cash": 12.78, "portfolio": 13.40}
    return {
        "equity": latest.get("equity", 26.18),
        "cash": latest.get("cash", 12.78),
        "portfolio": latest.get("portfolio", 13.40),
        "daily_ledger": mem.get("daily_ledger", {}),
        "equity_history": hist,
    }


@app.get("/tracker")
async def get_tracker_page() -> FileResponse:
    tracker_file = Path(__file__).resolve().parent.parent.parent / "dashboard" / "live_tracker.html"
    return FileResponse(str(tracker_file))


# -------------------------------------------------------------------------
# Standalone Crypto Quant Machine Endpoints (100% Isolated Bankroll)
# -------------------------------------------------------------------------
@app.get("/api/crypto/status")
async def get_crypto_status(symbol: str = "SOL") -> dict[str, Any]:
    return await crypto_engine_instance.get_dashboard_snapshot(orderbook_symbol=symbol)


def _verify_mobile_pin(x_quant_pin: Optional[str]) -> None:
    """
    Stolen-Phone Protection Guard:
    If DASHBOARD_ACTION_PIN is configured in .env / Cloud Secrets, all state-modifying
    actions require the matching PIN header so an unlocked stolen phone cannot trigger trades.
    """
    required_pin = os.getenv("DASHBOARD_ACTION_PIN", "").strip()
    if required_pin and x_quant_pin != required_pin:
        raise HTTPException(
            status_code=403,
            detail="Stolen-Phone Protection Active: Invalid or missing Security PIN.",
        )


@app.post("/api/crypto/actions/green_out_all")
async def crypto_green_out_all(
    x_quant_pin: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    _verify_mobile_pin(x_quant_pin)
    count = crypto_engine_instance.trigger_green_out_all()
    snapshot = await crypto_engine_instance.get_dashboard_snapshot()
    return {
        "status": "success",
        "greened_out_count": count,
        "snapshot": snapshot,
    }


@app.post("/api/crypto/actions/rotate_yield")
async def crypto_rotate_yield(
    x_quant_pin: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    _verify_mobile_pin(x_quant_pin)
    rotation = await crypto_engine_instance.trigger_yield_rotation()
    snapshot = await crypto_engine_instance.get_dashboard_snapshot()
    return {
        "status": "success",
        "rotation": rotation,
        "snapshot": snapshot,
    }


@app.get("/crypto")
async def get_crypto_dashboard_page() -> FileResponse:
    crypto_file = Path(__file__).resolve().parent.parent.parent / "dashboard" / "crypto_machine.html"
    return FileResponse(str(crypto_file))


# Mount compiled Astro PWA dashboard if available
dist_path = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if dist_path.exists():
    app.mount("/", StaticFiles(directory=str(dist_path), html=True), name="frontend")
