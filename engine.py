"""
engine.py
=========
The core of the CLaaS platform: a price/time-priority Central Limit Order
Book (CLOB) that turns GPU compute into a liquid, tradeable commodity.

Market semantics
----------------
* Commodity unit : 1 "GPU-hour" = one GPU allocated for one hour.
* Prices         : USD per GPU-hour, cents precision.
* Asks (supply)  : node providers (crypto farms, academic labs, spot clouds).
* Bids (demand)  : AI startups / researchers that need compute.
* Matching       : continuous. An incoming bid matches the cheapest resting
                   asks while ask.price <= bid.price (mirror image for an
                   incoming ask). Trades print at the resting (maker) price.
* Partial fills  : an incoming order is filled against as many opposite
                   resting orders as its limit allows; the unmatched remainder
                   rests on the book at its limit price. Example: a startup
                   bids for 100 GPUs, a provider only has 50 -> fill 50, and
                   the remaining 50 stay on the book as resting liquidity.
* Spot price     : the price of the most recently executed trade.

Concurrency
-----------
The single in-memory book instance is shared by the FastAPI request handlers
and the asyncio simulator loops (see simulators.py / api.py). Every mutating
operation takes a re-entrant threading.RLock, so the engine stays correct
even if it is later driven from multiple threads.

Order tracking
--------------
Every submitted order (and its fills) is indexed by id for a bounded window,
which powers GET /order/{id} — the web UI's live "your orders" tracker.

Internal representation
-----------------------
Two binary min-heaps of tuples:

    asks: ( price,  timestamp, order_id, Ask)   -> cheapest ask on top
    bids: (-price, timestamp, order_id, Bid)     -> highest  bid on top

Ties on price break on timestamp (FIFO, i.e. strict price-time priority).
Quantities are mutated in place on partial fills; heap keys never depend on
quantity, so partial fills never invalidate heap ordering.
"""

from __future__ import annotations

import heapq
import logging
import threading
import time
import uuid
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple, Union

from pydantic import BaseModel, Field

logger = logging.getLogger("claas.engine")

__all__ = ["Ask", "Bid", "Trade", "OrderBook", "new_ask", "new_bid"]


# ---------------------------------------------------------------------------
# Data models (Pydantic)
# ---------------------------------------------------------------------------

class Ask(BaseModel):
    """Sell-side liquidity: a node provider offering GPU compute."""

    order_id: str = Field(description="Unique order id, e.g. 'A-9f2c1a77'.")
    provider: str = Field(description="Provider / desk name.")
    hardware: str = Field(default="H100", description="GPU tier on offer.")
    price: float = Field(gt=0, description="Limit price, USD per GPU-hour.")
    quantity: int = Field(ge=0, description="Unfilled GPU-hours (shrinks as the order fills).")
    original_quantity: int = Field(ge=1, description="Quantity originally submitted.")
    timestamp: float = Field(description="Epoch seconds at submission.")

    @property
    def is_filled(self) -> bool:
        return self.quantity == 0


class Bid(BaseModel):
    """Buy-side demand: an AI startup requesting GPU compute."""

    order_id: str = Field(description="Unique order id, e.g. 'B-4e1d88c0'.")
    startup: str = Field(description="Startup / desk name.")
    price: float = Field(gt=0, description="Limit price, USD per GPU-hour.")
    quantity: int = Field(ge=0, description="Unfilled GPU-hours (shrinks as the order fills).")
    original_quantity: int = Field(ge=1, description="Quantity originally submitted.")
    timestamp: float = Field(description="Epoch seconds at submission.")

    @property
    def is_filled(self) -> bool:
        return self.quantity == 0


class Trade(BaseModel):
    """An executed match between a Bid and an Ask."""

    trade_id: str
    bid_id: str
    ask_id: str
    price: float = Field(description="Execution price (the resting/maker price).")
    quantity: int = Field(description="GPU-hours exchanged in this fill.")
    buyer: str = Field(description="Startup that bought.")
    seller: str = Field(description="Provider that sold.")
    hardware: str = Field(description="GPU tier delivered.")
    aggressor: str = Field(description="'bid' if the buyer was the taker, else 'ask'.")
    timestamp: float


# ---------------------------------------------------------------------------
# Order factories
# ---------------------------------------------------------------------------

def _oid(prefix: str) -> str:
    """Short, collision-safe order id, e.g. 'A-9f2c1a77'."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def new_ask(provider: str, hardware: str, price: float, quantity: int) -> Ask:
    """Build a validated Ask with id + timestamp. Prices are rounded to cents."""
    return Ask(
        order_id=_oid("A"),
        provider=provider,
        hardware=hardware,
        price=round(float(price), 2),
        quantity=int(quantity),
        original_quantity=int(quantity),
        timestamp=time.time(),
    )


def new_bid(startup: str, price: float, quantity: int) -> Bid:
    """Build a validated Bid with id + timestamp. Prices are rounded to cents."""
    return Bid(
        order_id=_oid("B"),
        startup=startup,
        price=round(float(price), 2),
        quantity=int(quantity),
        original_quantity=int(quantity),
        timestamp=time.time(),
    )


# ---------------------------------------------------------------------------
# The order book
# ---------------------------------------------------------------------------

class OrderBook:
    """
    A continuous-matching central limit order book for GPU compute.

    Usage::

        book = OrderBook()
        fills = book.submit_bid(new_bid("StartupX", 2.50, 100))  # -> [Trade, ...]
        book.snapshot(depth=5)                                   # -> dict for the API/UI
        book.history(limit=50)                                    # newest first
        book.order_status("B-4e1d88c0")                          # -> live order status
    """

    MAX_TRACKED_ORDERS = 4000

    def __init__(self, max_depth_per_side: int = 60, history_size: int = 500) -> None:
        self.max_depth_per_side = max_depth_per_side
        self._asks: List[Tuple[float, float, str, Ask]] = []   # ( price, ts, id, order)
        self._bids: List[Tuple[float, float, str, Bid]] = []   # (-price, ts, id, order)
        self._trades: Deque[Trade] = deque(maxlen=history_size)
        self._lock = threading.RLock()

        # Order tracking: lets clients poll the live status of any order they
        # submitted (fills so far, resting remainder) via GET /order/{id}.
        # Bounded so a long-running simulation can't grow without limit.
        self._order_index: Dict[str, Union[Ask, Bid]] = {}
        self._fills_by_order: Dict[str, List[Trade]] = {}
        self._order_ids: Deque[str] = deque()  # insertion order, for eviction

        # Session statistics / spot price tracking.
        self._last_trade_price: Optional[float] = None
        self._prev_trade_price: Optional[float] = None
        self.total_trades: int = 0
        self.total_volume: int = 0        # GPU-hours traded
        self.total_notional: float = 0.0  # USD traded

    # -- public API --------------------------------------------------------

    @property
    def spot_price(self) -> Optional[float]:
        """Price of the most recent executed trade (None before the first trade)."""
        return self._last_trade_price

    @property
    def previous_spot_price(self) -> Optional[float]:
        """Spot price before the last trade — lets the UI show a tick delta."""
        return self._prev_trade_price

    def submit_ask(self, ask: Ask) -> List[Trade]:
        """
        Submit sell-side liquidity and run the matching loop.

        The incoming ask sweeps resting bids while bid.price >= ask.price
        (each fill prints at the resting bid's price, since the bid is the
        maker). Any unmatched remainder rests on the book at its limit price.

        Returns the list of trades generated (possibly empty).
        """
        with self._lock:
            fills: List[Trade] = []
            self._track(ask)
            while ask.quantity > 0 and self._bids:
                _neg_price, _ts, _oid, bid = self._bids[0]   # best (highest) bid
                if bid.price < ask.price:
                    break                                     # best bid no longer crosses
                fills.append(self._execute(buy=bid, sell=ask, price=bid.price, aggressor="ask"))
                if bid.quantity == 0:
                    heapq.heappop(self._bids)                 # maker fully filled -> remove
            if ask.quantity > 0:
                heapq.heappush(self._asks, (ask.price, ask.timestamp, ask.order_id, ask))
                self._prune(self._asks)
                logger.debug("REST  ask %s x%d @ $%.2f from %s",
                             ask.order_id, ask.quantity, ask.price, ask.provider)
            return fills

    def submit_bid(self, bid: Bid) -> List[Trade]:
        """
        Submit buy-side demand and run the matching loop.

        The incoming bid sweeps resting asks while ask.price <= bid.price
        (each fill prints at the resting ask's price, since the ask is the
        maker). Any unmatched remainder rests on the book at its limit price.

        Returns the list of trades generated (possibly empty).
        """
        with self._lock:
            fills: List[Trade] = []
            self._track(bid)
            while bid.quantity > 0 and self._asks:
                _price, _ts, _oid, ask = self._asks[0]        # best (lowest) ask
                if ask.price > bid.price:
                    break                                     # best ask no longer crosses
                fills.append(self._execute(buy=bid, sell=ask, price=ask.price, aggressor="bid"))
                if ask.quantity == 0:
                    heapq.heappop(self._asks)                 # maker fully filled -> remove
            if bid.quantity > 0:
                heapq.heappush(self._bids, (-bid.price, bid.timestamp, bid.order_id, bid))
                self._prune(self._bids)
                logger.debug("REST  bid %s x%d @ $%.2f from %s",
                             bid.order_id, bid.quantity, bid.price, bid.startup)
            return fills

    def snapshot(self, depth: int = 10) -> Dict:
        """
        Point-in-time view of the book: best `depth` levels per side
        (bids highest-first, asks lowest-first) plus top-of-book stats.
        """
        with self._lock:
            bids = [entry[3].model_dump() for entry in heapq.nsmallest(depth, self._bids)]
            asks = [entry[3].model_dump() for entry in heapq.nsmallest(depth, self._asks)]
            best_bid: Optional[float] = bids[0]["price"] if bids else None
            best_ask: Optional[float] = asks[0]["price"] if asks else None
            return {
                "symbol": "GPU-HR",
                "timestamp": time.time(),
                "spot_price": self._last_trade_price,
                "previous_spot_price": self._prev_trade_price,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": round(best_ask - best_bid, 2)
                          if (best_bid is not None and best_ask is not None) else None,
                "bid_count": len(self._bids),
                "ask_count": len(self._asks),
                "bid_volume": sum(entry[3].quantity for entry in self._bids),
                "ask_volume": sum(entry[3].quantity for entry in self._asks),
                "total_trades": self.total_trades,
                "total_volume": self.total_volume,
                "total_notional": round(self.total_notional, 2),
                "bids": bids,   # best (highest price) first
                "asks": asks,   # best (lowest price)  first
            }

    def history(self, limit: int = 50) -> List[Dict]:
        """Recent executed trades, newest first."""
        with self._lock:
            trades = list(self._trades)
        return [t.model_dump() for t in trades[-limit:][::-1]]

    def order_status(self, order_id: str) -> Optional[Dict]:
        """
        Live status of a previously submitted order — used by the web UI's
        "Your orders" panel. Returns None if the id is unknown (or was
        evicted from the tracking window, or the process restarted).
        """
        with self._lock:
            order = self._order_index.get(order_id)
            if order is None:
                return None
            fills = [t.model_dump() for t in self._fills_by_order.get(order_id, [])]
            live_ids = {entry[2] for entry in self._bids} | {entry[2] for entry in self._asks}
            on_book = order_id in live_ids
            filled_qty = order.original_quantity - order.quantity

            if isinstance(order, Ask):
                side, party, extra = "ask", order.provider, {"hardware": order.hardware}
            else:
                side, party, extra = "bid", order.startup, {}

            if filled_qty == order.original_quantity:
                status = "filled"
            elif on_book:
                status = "resting" if filled_qty == 0 else "partially_filled"
            else:
                # Evicted by the book's depth cap before fully filling.
                status = "expired"

            avg = (sum(t["price"] * t["quantity"] for t in fills) / filled_qty) if filled_qty else None
            return {
                "order_id": order_id,
                "side": side,
                "party": party,
                "price": order.price,
                "original_quantity": order.original_quantity,
                "filled_quantity": filled_qty,
                "remaining_quantity": order.quantity,
                "status": status,
                "avg_fill_price": avg,
                "fills": fills,
                "submitted_at": order.timestamp,
                **extra,
            }

    # -- internals -----------------------------------------------------------

    def _track(self, order: Union[Ask, Bid]) -> None:
        """Index an order (and its future fills) for status lookups."""
        oid = order.order_id
        self._order_index[oid] = order
        self._fills_by_order.setdefault(oid, [])
        self._order_ids.append(oid)
        while len(self._order_ids) > self.MAX_TRACKED_ORDERS:
            old = self._order_ids.popleft()
            self._order_index.pop(old, None)
            self._fills_by_order.pop(old, None)

    def _execute(self, buy: Bid, sell: Ask, price: float, aggressor: str) -> Trade:
        """Fill min(buy.quantity, sell.quantity) units at price and record it."""
        fill = min(buy.quantity, sell.quantity)
        buy.quantity -= fill
        sell.quantity -= fill

        trade = Trade(
            trade_id=_oid("T"),
            bid_id=buy.order_id,
            ask_id=sell.order_id,
            price=price,
            quantity=fill,
            buyer=buy.startup,
            seller=sell.provider,
            hardware=sell.hardware,
            aggressor=aggressor,
            timestamp=time.time(),
        )
        self._trades.append(trade)
        # Record the fill against both participants' order histories (a fill
        # involves a taker and a maker; both may be tracked orders).
        for oid in (buy.order_id, sell.order_id):
            order_fills = self._fills_by_order.get(oid)
            if order_fills is not None:
                order_fills.append(trade)
        self._prev_trade_price = self._last_trade_price
        self._last_trade_price = price            # <- the "spot price" of compute
        self.total_trades += 1
        self.total_volume += fill
        self.total_notional += fill * price
        logger.info("TRADE  %5d GPU-hr @ $%6.2f | %s <- %s (%s)",
                    fill, price, buy.startup, sell.provider, sell.hardware)
        return trade

    def _prune(self, heap: list) -> None:
        """
        Keep the book tidy: when a side exceeds max_depth_per_side
        resting orders, the worst-priced tail is culled.
        """
        if len(heap) > self.max_depth_per_side:
            heap[:] = sorted(heap)[: self.max_depth_per_side]
