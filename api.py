"""
api.py
======
The FastAPI routing layer over the CLaaS matching engine — it serves BOTH the
HTTP API and the web app's static frontend (static/index.html), so the whole
platform runs as a single process on a single port.

HTTP API:

    POST /ask            submit supply  (a node provider offering compute)
    POST /bid            submit demand  (an AI startup requesting compute)
    GET  /orderbook      current top-of-book, best bids / asks
    GET  /history        executed trades (newest first) + spot price
    GET  /order/{id}     live status of a submitted order (fills, remainder)
    GET  /market         simulator metadata (simulated clock, demand curve, ...)
    GET  /health         liveness probe
    GET  /docs           interactive API reference (Swagger)

Web app:  GET /  ->  static/index.html (the CLaaS marketplace page).

The simulator bots (simulators.py) run as asyncio tasks inside this same
process — started by the lifespan handler below — so engine, API, web
frontend and bots all share one in-memory book.

Run (single worker only — state is in-memory):

    uvicorn api:app --host 0.0.0.0 --port 8000

then open  http://localhost:8000  in your browser.

Environment knobs:

    RUN_SIMULATORS=0   boot an empty book (manual orders only)
    LOG_LEVEL=DEBUG    log every resting order / bot tick
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import simulators
from engine import Ask, Bid, OrderBook, Trade, new_ask, new_bid

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("claas.api")

RUN_SIMULATORS = os.environ.get("RUN_SIMULATORS", "1").lower() not in {"0", "false", "no", "off"}

# The single shared in-memory book — the source of truth for the platform.
book = OrderBook()

# Directory holding the web app's static assets (index.html, styles.css, app.js).
STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start (and gracefully stop) the market simulator bots."""
    sim_task: Optional["asyncio.Future"] = None
    if RUN_SIMULATORS:
        log.info("Booting market simulators (node providers + AI startups)...")
        sim_task = asyncio.gather(
            simulators.provider_simulator(book),
            simulators.startup_simulator(book),
        )
    else:
        log.info("RUN_SIMULATORS=0 -> empty book, manual orders only.")
    try:
        yield
    finally:
        if sim_task is not None:
            sim_task.cancel()
            try:
                await sim_task
            except asyncio.CancelledError:
                pass
            log.info("Simulators stopped.")


app = FastAPI(
    title="CLaaS — Compute-Liquidity-as-a-Service",
    description=(
        "A simulated high-frequency marketplace for GPU compute (FLOPs) built "
        "on a price/time-priority central limit order book. Prices are USD "
        "per GPU-hour; quantities are GPU-hours."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# The Streamlit desk talks to this API server-side; permissive CORS keeps the
# API usable from browsers/tools too.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class AskIn(BaseModel):
    """Payload for POST /ask — supply side."""

    provider: str = Field(default="Manual Desk", max_length=64,
                          description="Provider / desk name.")
    hardware: str = Field(default="H100", max_length=32,
                          description="GPU tier, e.g. 'RTX 4090', 'A100 80GB', 'H100 SXM'.")
    price: float = Field(gt=0, description="Limit price, USD per GPU-hour.")
    quantity: int = Field(gt=0, description="GPU-hours on offer.")


class BidIn(BaseModel):
    """Payload for POST /bid — demand side."""

    startup: str = Field(default="Manual Desk", max_length=64,
                         description="Startup / desk name.")
    price: float = Field(gt=0, description="Limit price, USD per GPU-hour.")
    quantity: int = Field(gt=0, description="GPU-hours requested.")


def _result(remaining: int, trades: List[Trade]) -> str:
    """Classify an order outcome for the response."""
    if trades and remaining == 0:
        return "filled"
    if trades:
        return "partially_filled"
    return "resting"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """Serve the CLaaS web app (single-page frontend, same origin as the API)."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/about", tags=["meta"])
async def about() -> Dict[str, Any]:
    return {
        "service": "CLaaS — Compute-Liquidity-as-a-Service",
        "version": app.version,
        "docs": "/docs",
        "simulators_running": RUN_SIMULATORS,
        "endpoints": ["/ask", "/bid", "/orderbook", "/history", "/order/{id}", "/market", "/health"],
    }


@app.get("/health", tags=["meta"])
async def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/ask", tags=["trading"])
async def submit_ask(payload: AskIn) -> Dict[str, Any]:
    """
    Submit an ask (supply). If it crosses any resting bids it executes
    against them immediately — with partial fills — and any unmatched
    remainder rests on the book.
    """
    ask = new_ask(payload.provider, payload.hardware, payload.price, payload.quantity)
    trades = book.submit_ask(ask)
    return {
        "result": _result(ask.quantity, trades),
        "order": ask,
        "remaining_quantity": ask.quantity,
        "trades": trades,
        "spot_price": book.spot_price,
    }


@app.post("/bid", tags=["trading"])
async def submit_bid(payload: BidIn) -> Dict[str, Any]:
    """
    Submit a bid (demand). If it crosses any resting asks it executes
    against them immediately — with partial fills — and any unmatched
    remainder rests on the book.
    """
    bid = new_bid(payload.startup, payload.price, payload.quantity)
    trades = book.submit_bid(bid)
    return {
        "result": _result(bid.quantity, trades),
        "order": bid,
        "remaining_quantity": bid.quantity,
        "trades": trades,
        "spot_price": book.spot_price,
    }


@app.get("/orderbook", tags=["market-data"])
async def get_orderbook(
    depth: int = Query(default=10, ge=1, le=50, description="Levels per side"),
) -> Dict[str, Any]:
    """Current order book: best `depth` bids/asks, spot price and stats."""
    return book.snapshot(depth=depth)


@app.get("/history", tags=["market-data"])
async def get_history(
    limit: int = Query(default=50, ge=1, le=500, description="Max trades to return"),
) -> Dict[str, Any]:
    """Executed trades (newest first) and the current spot price."""
    trades = book.history(limit=limit)
    return {
        "spot_price": book.spot_price,
        "previous_spot_price": book.previous_spot_price,
        "total_trades": book.total_trades,
        "total_volume": book.total_volume,
        "total_notional": round(book.total_notional, 2),
        "trades": trades,
    }


@app.get("/market", tags=["market-data"])
async def get_market() -> Dict[str, Any]:
    """Simulator metadata: simulated clock, demand curve state, participants."""
    return simulators.market_state()


@app.get("/order/{order_id}", tags=["trading"])
async def get_order(order_id: str) -> Dict[str, Any]:
    """
    Live status of a previously submitted order: how much has filled, at what
    average price, and whether the remainder is still resting on the book.
    Used by the web UI's "Your orders" tracker. 404 if the id is unknown
    (e.g. the engine restarted — in-memory state).
    """
    status = book.order_status(order_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"Unknown order id: {order_id}")
    return status


# ---------------------------------------------------------------------------
# Static frontend — mounted last so API routes keep priority.
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    # `python api.py` also works.
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
