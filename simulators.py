"""
simulators.py
=============
Synthetic market participants ("the network nodes").

Two asyncio loops populate the shared OrderBook:

* ``provider_simulator()`` — 9 distinct node providers (crypto farms,
  academic labs, colo facilities, spot-cloud overflow...) quote asks every
  2-5 seconds, with hardware-tiered pricing (RTX 4090 ... H200).

* ``startup_simulator()`` — AI startups bid for compute batches. Their
  willingness-to-pay follows a compressed 24-hour "time-of-day" demand
  curve: one simulated trading day elapses every ``MARKET_DAY_SECONDS``
  of real time (default: 3 minutes), so you can watch demand (and the
  spot price) cycle through the night lull, the morning ramp and the
  afternoon inference peak within a single demo session.

Both loops are started by api.py's FastAPI lifespan handler, so the
matching engine, the API and the simulators all share one process and one
book. Set ``RUN_SIMULATORS=0`` to boot an empty, manual-only market.

All module-level state is touched only from the event loop thread (plus
atomic reads from request handlers), which is safe under the GIL for this
MVP; the OrderBook itself is additionally lock-guarded.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from typing import Any, Dict, List, Tuple

from engine import OrderBook, new_ask, new_bid

logger = logging.getLogger("claas.simulators")

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

MARKET_DAY_SECONDS = 180.0          # one simulated 24h trading day per 3 real minutes
PROVIDER_QUOTE_DELAY = (2.0, 5.0)   # spec: a new ask every 2-5 seconds
STARTUP_BID_DELAY = (1.5, 4.0)      # startups bid a bit more often than providers quote
PROVIDER_CROSS_PROB = 0.18          # chance a provider prices to cross the book (impatient seller)
BID_CROSS_PROB = 0.15               # chance a startup submits an aggressive "urgent run" bid
# Pricing calibration — anchored to REAL market data via scripts/real_prices.py
# (checked 2026-10-30: Vast.ai RTX 4090 median ask ≈ $0.47/GPU-hr · Azure H100
# spot $1.29–2.04 · Azure H100 on-demand $6.98–11.06 · peer-market H100 ≈ $2+).
# The fair value tracks an H100-class GPU-hour; other tiers are priced as
# fractions of it. Real-world ratios are steeper (~4x from 4090 to H100); we
# compress them ~2x so the single fungible "spot price" stays representative
# of a blended GPU-hour instead of always printing the cheapest tier.
FAIR_VALUE_START = 2.20             # $/GPU-hr around which the whole market oscillates
FAIR_VALUE_MIN, FAIR_VALUE_MAX = 1.55, 3.10

# Hardware tiers: price multiplier vs the market's drifting fair value.
# (The MVP treats compute as a fungible commodity — bids match any hardware
# at the right price; the tier just shifts where a provider can afford to quote.)
HARDWARE_TIERS: Dict[str, float] = {
    "RTX 4090": 0.45,   # ≈ $1.00  (real Vast.ai median: $0.47)
    "L40S": 0.62,       # ≈ $1.35
    "A100 80GB": 0.78,  # ≈ $1.72
    "H100 SXM": 1.00,   # ≈ $2.20  (between cloud spot and on-demand list)
    "H200": 1.22,       # ≈ $2.68
}

# 9 distinct node providers (spec asks for 5-10).
PROVIDERS: List[Dict[str, Any]] = [
    {"name": "HashHarbor Crypto Farm",     "hardware": "RTX 4090",  "qty": (40, 160)},
    {"name": "Dorm-Rig Collective",        "hardware": "RTX 4090",  "qty": (10, 60)},
    {"name": "Sunset Colocation LLC",      "hardware": "L40S",      "qty": (60, 200)},
    {"name": "Midwest GPU Barn",           "hardware": "L40S",      "qty": (30, 120)},
    {"name": "Cascade University HPC",     "hardware": "A100 80GB", "qty": (20, 80)},
    {"name": "Alpine Research Lab",        "hardware": "A100 80GB", "qty": (10, 50)},
    {"name": "Helios Cloud (spot overflow)", "hardware": "H100 SXM", "qty": (8, 48)},
    {"name": "Orbital DC Partners",        "hardware": "H100 SXM",  "qty": (16, 64)},
    {"name": "TitanForge Compute",         "hardware": "H200",      "qty": (4, 24)},
]

# AI startup personas with typical batch sizes (GPU-hours per order).
STARTUPS: List[Dict[str, Any]] = [
    {"name": "MammothML",       "qty": (120, 400)},  # foundation-model lab, huge batches
    {"name": "PromptDynasty",   "qty": (20, 90)},    # consumer chat app, bursty
    {"name": "MirageVision",    "qty": (60, 160)},   # video-gen, steady inference
    {"name": "Oracle Robotics", "qty": (40, 120)},   # sim2real RL training
    {"name": "Babylon Bio",     "qty": (10, 60)},    # protein folding
    {"name": "TinyTune AI",     "qty": (5, 30)},     # finetuning shop, small jobs
    {"name": "Nebula Analytics", "qty": (30, 100)},  # batch embedding ETL
    {"name": "Skunkworks Labs", "qty": (15, 50)},    # grant-funded spinout
]

# ---------------------------------------------------------------------------
# Simulated clock + time-of-day demand curve
# ---------------------------------------------------------------------------

_START_TIME = time.time()   # t=0 of the simulated trading day
_fair_value = FAIR_VALUE_START

# (center hour, width hours, amplitude) — bumps add demand, the dip subtracts.
_DEMAND_BUMPS: List[Tuple[float, float, float]] = [
    (9.5, 1.8, 0.30),   # morning training-run rush
    (15.0, 2.0, 0.26),  # afternoon inference peak
    (20.5, 2.2, 0.14),  # evening consumer traffic
]
_NIGHT_DIP = (3.5, 2.6, 0.28)  # everyone's asleep


def simulated_hour(now: float | None = None) -> float:
    """Map real elapsed time onto a compressed 24h trading day (0.0 - 24.0)."""
    now = time.time() if now is None else now
    return ((now - _START_TIME) % MARKET_DAY_SECONDS) / MARKET_DAY_SECONDS * 24.0


def clock_str(hour: float) -> str:
    """Format a simulated hour as 'HH:MM'."""
    return f"{int(hour) % 24:02d}:{int((hour % 1) * 60):02d}"


def demand_multiplier(hour: float) -> float:
    """
    Time-of-day demand curve: a base of 1.0 plus gaussian bumps (rushed
    mornings, afternoon peaks) minus a late-night dip, clamped to [0.60, 1.40].
    Startups multiply their willingness-to-pay by this factor.
    """
    def bump(center: float, width: float, amp: float) -> float:
        d = abs(hour - center) % 24.0
        d = min(d, 24.0 - d)  # wrap-around distance on the 24h dial
        return amp * math.exp(-(d ** 2) / (2 * width ** 2))

    m = 1.0 + sum(bump(*b) for b in _DEMAND_BUMPS) - bump(*_NIGHT_DIP)
    return max(0.60, min(1.40, m))


def demand_label(multiplier: float) -> str:
    """Bucket the demand multiplier for the dashboard."""
    if multiplier < 0.85:
        return "LOW"
    if multiplier < 1.05:
        return "NORMAL"
    if multiplier < 1.20:
        return "ELEVATED"
    return "PEAK"


def _drift_fair_value() -> None:
    """
    Slow, mean-reverting random walk of the market's shared fair value.
    Both simulator loops nudge it, so the spot price wanders realistically
    instead of orbiting a constant.
    """
    global _fair_value
    _fair_value *= 1.0 + random.gauss(0.0, 0.006) + 0.004 * (FAIR_VALUE_START - _fair_value) / FAIR_VALUE_START
    _fair_value = min(FAIR_VALUE_MAX, max(FAIR_VALUE_MIN, _fair_value))


def market_state() -> Dict[str, Any]:
    """Snapshot of simulator metadata — served by GET /market."""
    hour = simulated_hour()
    dem = demand_multiplier(hour)
    return {
        "simulated_hour": round(hour, 2),
        "simulated_clock": clock_str(hour),
        "real_seconds_per_sim_day": MARKET_DAY_SECONDS,
        "demand_multiplier": round(dem, 3),
        "demand_label": demand_label(dem),
        "fair_value": round(_fair_value, 3),
        # Per-tier reference prices (fair value x hardware multiplier) — shown
        # in the web UI's "sell compute" form as a pricing hint.
        "reference_prices": {hw: round(_fair_value * mult, 2)
                             for hw, mult in HARDWARE_TIERS.items()},
        "providers": [p["name"] for p in PROVIDERS],
        "startups": [s["name"] for s in STARTUPS],
    }


# ---------------------------------------------------------------------------
# Simulation loops
# ---------------------------------------------------------------------------

async def provider_simulator(book: OrderBook) -> None:
    """
    Node-provider loop: every 2-5 seconds a random provider quotes an ask.

    Pricing: ``fair_value * hardware_tier * (1 + premium)``. Most providers
    quote slightly above fair value (they'd rather not sell cheap); a few
    impatient ones undercut enough to cross the best bid and sell instantly.
    """
    logger.info("Provider simulator online: %d node providers, %s",
                len(PROVIDERS), ", ".join(sorted({p['hardware'] for p in PROVIDERS})))
    try:
        while True:
            await asyncio.sleep(random.uniform(*PROVIDER_QUOTE_DELAY))
            try:
                _drift_fair_value()
                provider = random.choice(PROVIDERS)
                tier = HARDWARE_TIERS[provider["hardware"]]

                if random.random() < PROVIDER_CROSS_PROB:
                    # Impatient: price below fair value to cross the book now.
                    edge = abs(random.gauss(0.015, 0.010)) + 0.002
                    price = _fair_value * tier * (1.0 - edge)
                else:
                    # Patient market maker: quote with a small premium.
                    premium = abs(random.gauss(0.035, 0.045)) + 0.002
                    price = _fair_value * tier * (1.0 + premium)

                qty = random.randint(*provider["qty"])
                ask = new_ask(provider["name"], provider["hardware"], price, qty)
                fills = book.submit_ask(ask)
                logger.debug("ASK  %-30s x%4d @ $%5.2f -> %d fill(s)",
                             provider["name"], qty, ask.price, len(fills))
            except asyncio.CancelledError:
                raise
            except Exception:  # never let one bad tick kill the loop
                logger.exception("provider tick failed")
    except asyncio.CancelledError:
        logger.info("Provider simulator shutting down.")
        raise


async def startup_simulator(book: OrderBook) -> None:
    """
    AI-startup loop: every 1.5-4 seconds a random startup bids for a batch.

    Pricing: ``fair_value * demand_multiplier(hour) * (1 - discount)`` —
    i.e. willingness-to-pay follows the simulated time-of-day demand curve.
    With probability ``BID_CROSS_PROB`` a startup has an urgent training run
    and prices aggressively above the curve, sweeping the ask side (which is
    what generates those lovely multi-fill partial-fill cascades).
    """
    logger.info("Startup simulator online: %d AI startups on a %ss simulated day",
                len(STARTUPS), int(MARKET_DAY_SECONDS))
    try:
        while True:
            await asyncio.sleep(random.uniform(*STARTUP_BID_DELAY))
            try:
                _drift_fair_value()
                startup = random.choice(STARTUPS)
                dem = demand_multiplier(simulated_hour())

                if random.random() < BID_CROSS_PROB:
                    # Urgent training run: pay up to get GPUs *now*.
                    edge = abs(random.gauss(0.05, 0.04)) + 0.01
                    price = _fair_value * dem * (1.0 + edge)
                    qty = random.randint(80, 320)
                else:
                    # Bargain hunter: bid slightly under the demand-adjusted value.
                    discount = abs(random.gauss(0.02, 0.025)) + 0.002
                    price = _fair_value * dem * (1.0 - discount)
                    qty = random.randint(*startup["qty"])

                bid = new_bid(startup["name"], price, qty)
                fills = book.submit_bid(bid)
                logger.debug("BID  %-30s x%4d @ $%5.2f -> %d fill(s)",
                             startup["name"], qty, bid.price, len(fills))
            except asyncio.CancelledError:
                raise
            except Exception:  # never let one bad tick kill the loop
                logger.exception("startup tick failed")
    except asyncio.CancelledError:
        logger.info("Startup simulator shutting down.")
        raise
