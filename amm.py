"""
amm.py — AMM liquidity backstop for empty books (EXPERIMENT, not yet wired)
===========================================================================
Adopted from the LiquidityProtocol/AMM-Simulator teardown (COMPETITIVE_INTEL
§3): the cold-start killer for any CLOB is the empty book — first users see a
blank page and bounce. Uniswap solved this for tokens with an automated
market maker: price comes from pool reserves instead of resting orders.

Plan (Phase 2–3 experiment, OFF by default):
  * The platform seeds one AMMPool per hardware tier with GPU-hour inventory
    + USD at a price anchored to real market data (scripts/real_prices.py).
  * When a taker would rest against an EMPTY opposite side, the engine can
    fill the remainder against the pool (fee + slippage) instead — instant
    fills from day one, no empty-book death spiral.
  * As real liquidity grows, the book outcompetes the pool on price; an
    arbitrageur bot keeps the two aligned (the AMM-Simulator's agent design).

This module is the pool math only — pure, dependency-free, unit-tested.
engine.py does NOT call it yet; that wiring lands behind a flag after the
concierge test validates demand (ROADMAP §11).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Tuple


@dataclass
class AMMPool:
    """
    Constant-product pool: x = GPU-hours inventory, y = USD reserves.

    spot price = y / x  (USD per GPU-hour). Buying hours drains x and adds
    y (price slides UP); selling hours adds x and drains y (price slides
    DOWN). A fee (bps, charged on top for buys / off the top for sells) makes
    the pool the counterparty of last resort — book prices inside the pool's
    fee band always win, which is exactly the backstop behavior we want.
    """

    gpu_hours: float
    usd: float
    fee_bps: int = 30                      # 0.30% — Uniswap V2-ish
    _k: float = field(init=False)

    def __post_init__(self) -> None:
        if self.gpu_hours <= 0 or self.usd <= 0:
            raise ValueError("reserves must be positive")
        if not 0 <= self.fee_bps < 10_000:
            raise ValueError("fee_bps must be in [0, 10000)")
        self._k = self.gpu_hours * self.usd

    # -- reads ------------------------------------------------------------

    @property
    def spot_price(self) -> float:
        """USD per GPU-hour at current reserves."""
        return self.usd / self.gpu_hours

    def quote_buy(self, hours: float) -> float:
        """USD cost (fee included) to buy `hours` out of the pool."""
        self._check_hours_in(hours, self.gpu_hours)
        new_x = self.gpu_hours - hours
        gross = self._k / new_x - self.usd
        return gross * (1 + self.fee_bps / 10_000)

    def quote_sell(self, hours: float) -> float:
        """USD received (fee included) for selling `hours` into the pool."""
        self._check_hours_in(hours)
        new_x = self.gpu_hours + hours
        gross = self.usd - self._k / new_x
        return gross * (1 - self.fee_bps / 10_000)

    # -- writes -----------------------------------------------------------

    def execute_buy(self, hours: float) -> float:
        """Buy GPU-hours from the pool; mutates reserves; returns USD paid."""
        cost = self.quote_buy(hours)
        new_x = self.gpu_hours - hours
        self.usd += cost
        self.gpu_hours = new_x
        return cost

    def execute_sell(self, hours: float) -> float:
        """Sell GPU-hours into the pool; mutates reserves; returns USD got."""
        proceeds = self.quote_sell(hours)
        self.usd -= proceeds
        self.gpu_hours += hours
        return proceeds

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _check_hours_in(hours: float, max_hours: float | None = None) -> None:
        if hours <= 0:
            raise ValueError("hours must be positive")
        if max_hours is not None and hours >= max_hours:
            raise ValueError("buy would drain the pool — buy less")
