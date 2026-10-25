"""FastAPI Backend Server for Kalshi Quant Engine Dashboard.

Provides:
- GET /api/status - Engine mode, balances, active pod states
- GET /api/pods - Pod details (Pod 1 Baskets, Pod 2 Positions, Pod 3 Tail Locks)
- GET /api/baskets - DuckDB tracked synthetic baskets
- GET /api/orderbook/{ticker} - Live Reciprocal Order Book (YES/NO bids + synthesized asks)
- GET /api/feeds/sharp - Live Pinnacle & Circa de-vigged lines
- GET /api/feeds/weather - Stadium weather & wind effects
- POST /api/actions/pause - Pause engine
- POST /api/actions/resume - Resume engine
- POST /api/actions/green_out_all - Trigger emergency or mid-game green out
- POST /api/actions/scan - Force trigger a fresh scan across all pods
- GET /api/analytics/grader - Nightly Brier Score & CLV grading report
"""

import asyncio
from pathlib import Path
from typing import Any, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from engine.config import settings
from engine.core.orderbook import KalshiOrderBookEngine

app = FastAPI(
    title="Kalshi Quant Multi-Strategy Engine API",
    version="2.0.0",
    description="Quantitative algorithmic trading engine for Kalshi event contracts",
)

# Enable CORS for Astro.js frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global engine reference (injected on startup)
engine_instance: Optional[Any] = None


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
        return {
            "ticker": ticker,
            "best_yes_bid": book.best_yes_bid,
            "best_no_bid": book.best_no_bid,
            "best_yes_ask": book.best_yes_ask,  # Reciprocal synthesized
            "best_no_ask": book.best_no_ask,    # Reciprocal synthesized
            "yes_spread": book.yes_spread,
            "no_spread": book.no_spread,
            "yes_midpoint": book.yes_midpoint,
            "yes_bids": [b.model_dump() for b in book.yes_bids],
            "no_bids": [b.model_dump() for b in book.no_bids],
            "synthesized_yes_asks": [a.model_dump() for a in book.get_synthesized_yes_asks()],
            "synthesized_no_asks": [a.model_dump() for a in book.get_synthesized_no_asks()],
            "depth_metrics": depth,
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


# Mount compiled Astro PWA dashboard if available
dist_path = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if dist_path.exists():
    app.mount("/", StaticFiles(directory=str(dist_path), html=True), name="frontend")

