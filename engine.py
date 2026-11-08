"""
engine.py — v3
==============
The core of the CLaaS platform: a price/time-priority **Central Limit Order
Book (CLOB)** that turns GPU compute into a liquid, tradeable commodity —
now with an optional **Supabase mirror** for durability and realtime UIs.

Architecture (why this shape)
-----------------------------
Matching stays IN MEMORY (heaps, partial fills, price-time priority — the
implementation that passed the full regression suite). A single writer
(the engine process) mirrors every event to Supabase Postgres:

    order submitted  ->  INSERT into orders        (engine-generated UUID id)
    fill executed    ->  UPDATE both order legs    (quantity + status)
                         INSERT into trades        (real ids on both sides)
    order evicted    ->  UPDATE status='canceled'

This keeps matching fast and correct (no network round-trips inside the
matching loop, no read-modify-write races) while the Next.js terminal reads
live data straight from Postgres with realtime subscriptions.

Env vars (server-side only — never expose these to a browser):

    SUPABASE_URL           project URL, e.g. https://xyz.supabase.co
    SUPABASE_KEY           key used by the engine. Prefer the service_role
                           secret (Settings -> API) since the engine is the
                           single trusted writer; the anon key also works if
                           your RLS allows it, but then ANYONE holding the
                           public anon key can write to your tables.
    CLAAS_DB_BOOTSTRAP     'cancel' to cancel all open orders at boot
                           (cleans up stale/crossed books left by crashes).
                           Default: read-only bootstrap (seed spot price).

If SUPABASE_URL / SUPABASE_KEY are unset (or the supabase package is
missing), the engine runs purely in memory — identical to the original MVP.

Market semantics (unchanged from v1/v2)
---------------------------------------
* Commodity unit : 1 "GPU-hour" = one GPU allocated for one hour.
* Prices         : USD per GPU-hour, cents precision.
* Matching       : continuous; incoming orders sweep opposite resting
                   liquidity while prices cross; trades print at the maker's
                   price; unmatched remainders rest on the book.
* Spot price     : last executed trade price (seeded from the DB on boot
                   when the mirror is active).

Order ids are real UUIDs (str(uuid.uuid4())) so the same id is valid in
memory AND in the Postgres uuid columns — trades always reference real rows.
"""

from __future__ import annotations

import heapq
import logging
import os
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Union

from pydantic import BaseModel, Field

logger = logging.getLogger("claas.engine")

__all__ = ["Ask", "Bid", "Trade", "OrderBook", "new_ask", "new_bid",
           "make_sink_from_env", "get_supabase_client", "submit_order"]

# ---------------------------------------------------------------------------
# Optional Supabase dependency
# ---------------------------------------------------------------------------

try:  # pragma: no cover - exercised only when supabase-py is installed
    from supabase import Client, create_client
    _HAS_SUPABASE = True
except ImportError:  # pragma: no cover
    Client = None  # type: ignore[assignment, misc]
    create_client = None  # type: ignore[assignment]
    _HAS_SUPABASE = False


# ---------------------------------------------------------------------------
# Data models (Pydantic) — unchanged shapes, UUID order ids
# ---------------------------------------------------------------------------

class Ask(BaseModel):
    """Sell-side liquidity: a node provider offering GPU compute."""

    order_id: str = Field(description="UUID shared by memory + Postgres.")
    client_id: Optional[str] = Field(default=None, description="Idempotency key from the client; duplicate submits are ignored.")
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

    order_id: str = Field(description="UUID shared by memory + Postgres.")
    client_id: Optional[str] = Field(default=None, description="Idempotency key from the client; duplicate submits are ignored.")
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

def _uid() -> str:
    """A real UUID4 string — valid both in memory and in Postgres uuid columns."""
    return str(uuid.uuid4())


def new_ask(provider: str, hardware: str, price: float, quantity: int,
            client_id: Optional[str] = None) -> Ask:
    """Build a validated Ask with id + timestamp. Prices are rounded to cents."""
    return Ask(
        order_id=_uid(),
        client_id=client_id,
        provider=provider,
        hardware=hardware,
        price=round(float(price), 2),
        quantity=int(quantity),
        original_quantity=int(quantity),
        timestamp=time.time(),
    )


def new_bid(startup: str, price: float, quantity: int,
            client_id: Optional[str] = None) -> Bid:
    """Build a validated Bid with id + timestamp. Prices are rounded to cents."""
    return Bid(
        order_id=_uid(),
        client_id=client_id,
        startup=startup,
        price=round(float(price), 2),
        quantity=int(quantity),
        original_quantity=int(quantity),
        timestamp=time.time(),
    )


# ---------------------------------------------------------------------------
# Supabase mirror
# ---------------------------------------------------------------------------

class SupabaseSink:
    """
    Mirrors in-memory book events to Supabase Postgres.

    Design rules:
      * NEVER raises into the matching engine — a DB hiccup must not stop
        trading. Failures are logged; after 5 consecutive failures the sink
        disables itself with a loud error so logs stay readable.
      * Schema-adaptive: on init it probes which columns exist on `orders`
        and `trades` and only writes those (e.g. your `orders` has no
        `hardware` column — it's simply omitted; add columns later and they
        get populated automatically, no code change).
    """

    MAX_CONSECUTIVE_FAILURES = 5

    def __init__(self, url: str, key: str) -> None:
        self.client: Any = create_client(url, key)
        self.enabled = True
        self._consecutive_failures = 0
        self._outbox: Deque = deque(maxlen=500)   # bounded retry buffer
        self.orders_columns: set = set()
        self.trades_columns: set = set()
        self._probe_columns("orders",
                            ["id", "maker", "side", "price", "quantity",
                             "status", "created_at", "hardware"])
        self._probe_columns("trades",
                            ["id", "price", "quantity", "executed_at",
                             "bid_id", "ask_id", "buyer", "seller", "hardware"])
        logger.info("Supabase mirror connected. orders cols=%s trades cols=%s",
                    sorted(self.orders_columns), sorted(self.trades_columns))

    # -- plumbing -----------------------------------------------------------

    def _probe_columns(self, table: str, candidates: List[str]) -> None:
        """A column exists iff selecting it returns HTTP 200 (read-only probe)."""
        found: set = set()
        for col in candidates:
            try:
                self.client.table(table).select(col).limit(1).execute()
                found.add(col)
            except Exception:
                pass
        setattr(self, f"{table}_columns", found)

    def _run(self, what: str, fn) -> None:
        """
        Queue a write and drive the outbox (open-compute-system pattern).

        Writes execute strictly in the order they were generated; a failing
        write stays buffered (bounded) and is retried by the next event or by
        flush_outbox(). A DB outage therefore DELAYS mirroring instead of
        losing events. The sink still disables itself after MAX consecutive
        failed drains to keep logs readable — buffered events survive, and
        flush_outbox() re-enables it once the DB answers again.
        """
        if not self.enabled:
            self._outbox.append((what, fn))
            return
        self._outbox.append((what, fn))
        self._drain_outbox()
        if self._outbox:
            self._consecutive_failures += 1
            logger.warning("Supabase mirror: '%s' not delivered — %d write(s) buffered "
                           "(failed drains %d/%d)",
                           what, len(self._outbox), self._consecutive_failures,
                           self.MAX_CONSECUTIVE_FAILURES)
            if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                self.enabled = False
                logger.error("Supabase mirror DISABLED after %d consecutive failed "
                             "drains — engine continues in memory; %d write(s) stay "
                             "buffered and will flush when the DB recovers.",
                             self._consecutive_failures, len(self._outbox))
        else:
            self._consecutive_failures = 0

    def _drain_outbox(self) -> int:
        """Retry buffered writes in FIFO order; stop at the first failure."""
        drained = 0
        while self._outbox:
            _what, fn = self._outbox[0]
            try:
                fn()
            except Exception:  # noqa: BLE001 — a bad write must never crash the engine
                break
            self._outbox.popleft()
            drained += 1
        if drained:
            logger.info("Supabase mirror: outbox drained %d buffered write(s).", drained)
        return drained

    def flush_outbox(self) -> int:
        """
        Explicit retry (api.py runs this every 30s): attempt a drain,
        re-enabling the sink if it had disabled itself. Returns writes flushed.
        """
        if not self._outbox:
            return 0
        was_disabled = not self.enabled
        self.enabled = True
        self._consecutive_failures = 0
        n = self._drain_outbox()
        if was_disabled and self._outbox:
            self.enabled = False  # DB still down — go back to sleep quietly
        return n

    @property
    def outbox_depth(self) -> int:
        """Writes buffered awaiting retry (observability)."""
        return len(self._outbox)

    # -- events ---------------------------------------------------------------

    def insert_order(self, order: Union[Ask, Bid]) -> None:
        """Persist a newly submitted order (full quantity, status 'open')."""
        row: Dict[str, Any] = {
            "id": order.order_id,
            "maker": order.provider if isinstance(order, Ask) else order.startup,
            "side": "ask" if isinstance(order, Ask) else "bid",
            "price": order.price,
            "quantity": order.quantity,
            "status": "open",
        }
        if isinstance(order, Ask) and "hardware" in self.orders_columns:
            row["hardware"] = order.hardware

        def write():
            self.client.table("orders").insert(row).execute()
        self._run("insert order", write)

    def record_fill(self, trade: Trade, buy: Bid, sell: Ask) -> None:
        """Persist one fill: update both legs, insert the trade row."""

        def leg_row(order: Union[Ask, Bid]) -> Dict[str, Any]:
            remaining = order.quantity
            filled = order.original_quantity - remaining
            status = "filled" if remaining == 0 else ("partial" if filled > 0 else "open")
            return {"quantity": remaining, "status": status}

        def update_buy():
            self.client.table("orders").update(leg_row(buy)).eq("id", buy.order_id).execute()
        def update_sell():
            self.client.table("orders").update(leg_row(sell)).eq("id", sell.order_id).execute()
        self._run("update bid leg", update_buy)
        self._run("update ask leg", update_sell)

        row: Dict[str, Any] = {"price": trade.price, "quantity": trade.quantity}
        cols = self.trades_columns
        if "bid_id" in cols:
            row["bid_id"] = trade.bid_id
        if "ask_id" in cols:
            row["ask_id"] = trade.ask_id
        if "buyer" in cols:
            row["buyer"] = trade.buyer
        if "seller" in cols:
            row["seller"] = trade.seller
        if "hardware" in cols:
            row["hardware"] = trade.hardware
        if "executed_at" in cols:
            row["executed_at"] = datetime.fromtimestamp(
                trade.timestamp, tz=timezone.utc).isoformat()

        def insert_trade():
            self.client.table("trades").insert(row).execute()
        self._run("insert trade", insert_trade)

    def cancel_order(self, order_id: str) -> None:
        """Mark an evicted/expired order canceled."""
        def write():
            self.client.table("orders").update(
                {"status": "canceled"}).eq("id", order_id).execute()
        self._run("cancel order", write)

    # -- bootstrap helpers (read-only unless explicitly asked) ----------------

    def fetch_last_trade_price(self) -> Optional[float]:
        """Most recent trade price from the DB (seeds the in-memory spot price)."""
        if "price" not in self.trades_columns or "executed_at" not in self.trades_columns:
            return None
        try:
            res = (self.client.table("trades").select("price")
                   .order("executed_at", desc=True).limit(1).execute())
            data = res.data or []
            return float(data[0]["price"]) if data else None
        except Exception as exc:
            logger.warning("Supabase mirror: fetch last trade failed (%s)", exc)
            return None

    def fetch_open_orders(self, limit: int = 500) -> List[Dict[str, Any]]:
        """
        All open/partial orders (for CLAAS_DB_BOOTSTRAP=restore). The engine
        rebuilds its in-memory heaps from these rows at boot, so a crash or
        restart loses nothing: Postgres is the source of truth, memory is a
        high-speed cache of it.
        """
        cols = self.orders_columns
        if not {"id", "maker", "side", "price", "quantity"} <= cols:
            return []
        select = "id,maker,side,price,quantity" + (",created_at" if "created_at" in cols else "")
        try:
            res = (self.client.table("orders").select(select)
                   .in_("status", ["open", "partial"])
                   .order("created_at", desc=False).limit(limit).execute())
        except Exception:
            try:  # 'partial' may not be in the status check constraint yet
                res = (self.client.table("orders").select(select)
                       .eq("status", "open")
                       .order("created_at", desc=False).limit(limit).execute())
            except Exception as exc:
                logger.warning("Supabase mirror: fetch open orders failed (%s)", exc)
                return []
        rows = []
        for r in res.data or []:
            ts = r.get("created_at")
            try:
                epoch = datetime.fromisoformat(ts).timestamp() if ts else time.time()
            except (TypeError, ValueError):
                epoch = time.time()
            rows.append({
                "order_id": str(r["id"]),
                "maker": r["maker"],
                "side": r["side"],
                "price": float(r["price"]),
                "quantity": int(r["quantity"]),
                "timestamp": epoch,
            })
        return rows

    def cancel_stale_open_orders(self) -> int:
        """
        Cancel every open/partial order — use at boot to clean books left
        stale by a previous crash/restart (they can leave the book crossed).
        Destructive: returns the number of rows canceled.
        """
        try:
            res = (self.client.table("orders")
                   .update({"status": "canceled"})
                   .in_("status", ["open", "partial"]).execute())
            return len(res.data or [])
        except Exception as exc:
            # 'canceled'/'partial' may not be in the check constraint — try 'open' only.
            try:
                res = (self.client.table("orders")
                       .update({"status": "filled"})
                       .eq("status", "open").execute())
                logger.warning("Supabase mirror: 'canceled' status rejected — "
                               "fell back to marking %d stale opens 'filled' (%s)",
                               len(res.data or []), exc)
                return len(res.data or [])
            except Exception as exc2:
                logger.error("Supabase mirror: stale-order cleanup failed (%s / %s)", exc, exc2)
                return 0

    def fetch_trades(self, limit: int = 50) -> List[Dict]:
        """Recent trades from the DB (newest first), mapped for /history."""
        if not {"price", "executed_at"} <= self.trades_columns:
            return []
        try:
            res = (self.client.table("trades").select("*")
                   .order("executed_at", desc=True).limit(limit).execute())
        except Exception as exc:
            logger.warning("Supabase mirror: fetch trades failed (%s)", exc)
            return []
        out: List[Dict] = []
        for t in res.data or []:
            ts = t.get("executed_at")
            try:  # ISO -> epoch seconds (legacy UIs expect epoch floats)
                epoch = datetime.fromisoformat(ts).timestamp() if ts else time.time()
            except (TypeError, ValueError):
                epoch = time.time()
            out.append({
                "trade_id": str(t.get("id", "")),
                "price": float(t["price"]),
                "quantity": int(t["quantity"]),
                "timestamp": epoch,
                "buyer": t.get("buyer", ""),
                "seller": t.get("seller", ""),
                "hardware": t.get("hardware", ""),
            })
        return out


_default_sink: Optional[SupabaseSink] = None


def make_sink_from_env() -> Optional[SupabaseSink]:
    """
    Build (and cache) the process-wide Supabase mirror from SUPABASE_URL /
    SUPABASE_KEY. Returns None when unconfigured — the engine then runs in
    the original pure-in-memory mode.
    """
    global _default_sink
    if _default_sink is not None:
        return _default_sink
    if not _HAS_SUPABASE:
        logger.info("supabase package not installed -> in-memory engine only.")
        return None
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        logger.info("SUPABASE_URL/SUPABASE_KEY not set -> in-memory engine only.")
        return None
    try:
        _default_sink = SupabaseSink(url, key)
        return _default_sink
    except Exception as exc:
        logger.error("Supabase mirror could not start (%s) -> in-memory engine only.", exc)
        return None


def get_supabase_client():
    """Back-compat accessor: the underlying supabase Client, or None."""
    sink = make_sink_from_env()
    return sink.client if sink else None


def submit_order(maker: str, side: str, price: float, quantity: int) -> Dict:
    """
    Back-compat helper (earlier code imported this). Inserts a resting order
    through the mirror WITHOUT matching it. Prefer OrderBook.submit_bid/ask.
    """
    order = new_bid(maker, price, quantity) if side == "bid" else new_ask(maker, "H100", price, quantity)
    sink = make_sink_from_env()
    if sink:
        sink.insert_order(order)
    return {"id": order.order_id, "maker": maker, "side": side,
            "price": order.price, "quantity": order.quantity, "status": "open"}


# ---------------------------------------------------------------------------
# The order book (in-memory CLOB + optional mirror)
# ---------------------------------------------------------------------------

class OrderBook:
    """
    A continuous-matching central limit order book for GPU compute.

    Usage::

        book = OrderBook(sink=make_sink_from_env())
        fills = book.submit_bid(new_bid("StartupX", 2.50, 100))  # -> [Trade, ...]
        book.snapshot(depth=5)
        book.history(limit=50)          # DB-backed when the mirror is active
        book.order_status("<uuid>")     # live order status
    """

    MAX_TRACKED_ORDERS = 4000

    def __init__(self, max_depth_per_side: int = 60, history_size: int = 500,
                 sink: Optional[SupabaseSink] = None) -> None:
        self.max_depth_per_side = max_depth_per_side
        self._asks: List[tuple] = []   # ( price, ts, id, Ask)  -> cheapest on top
        self._bids: List[tuple] = []   # (-price, ts, id, Bid)  -> highest  on top
        self._trades: Deque[Trade] = deque(maxlen=history_size)
        self._lock = threading.RLock()
        self._sink = sink

        # Order tracking for GET /order/{id}.
        self._order_index: Dict[str, Union[Ask, Bid]] = {}
        self._fills_by_order: Dict[str, List[Trade]] = {}
        self._order_ids: Deque[str] = deque()

        # Idempotency: client-generated key -> order id (double-submits are
        # silently dropped; api.py surfaces them as result='duplicate').
        self._client_ids: Dict[str, str] = {}

        # Session statistics / spot price tracking.
        self._last_trade_price: Optional[float] = None
        self._prev_trade_price: Optional[float] = None
        self.total_trades: int = 0
        self.total_volume: int = 0        # GPU-hours traded (this process)
        self.total_notional: float = 0.0  # USD traded (this process)

        # ---- bootstrap (only when a sink is configured) ---------------------
        if self._sink is not None:
            try:
                mode = os.environ.get("CLAAS_DB_BOOTSTRAP", "").lower()
                if mode == "cancel":
                    n = self._sink.cancel_stale_open_orders()
                    logger.info("Bootstrap: canceled %d stale open order(s).", n)
                elif mode == "restore":
                    # Rebuild the in-memory book from Postgres: memory becomes
                    # a cache of the DB, so restarts lose nothing. (Restored
                    # orders are NOT re-matched against each other.)
                    n = self._restore_from_db()
                    logger.info("Bootstrap: restored %d open order(s) from DB.", n)
                seed = self._sink.fetch_last_trade_price()
                if seed is not None:
                    self._last_trade_price = seed
                    logger.info("Bootstrap: spot price seeded from DB: $%.2f", seed)
            except Exception as exc:
                logger.warning("Bootstrap failed (engine continues): %s", exc)

    @property
    def persistence_active(self) -> bool:
        """True when the Supabase mirror is connected and healthy."""
        return self._sink is not None and self._sink.enabled

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
        Submit sell-side liquidity and run the matching loop (in memory).

        The incoming ask sweeps resting bids while ``bid.price >= ask.price``
        (fills print at the resting bid's price). Any unmatched remainder
        rests on the book. Every event is mirrored to Supabase.
        """
        with self._lock:
            if not self._admit(ask):
                return []                      # duplicate client_id — already processed
            fills: List[Trade] = []
            self._track(ask)
            if self._sink is not None:
                self._sink.insert_order(ask)
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
        Submit buy-side demand and run the matching loop (in memory).

        The incoming bid sweeps resting asks while ``ask.price <= bid.price``
        (fills print at the resting ask's price). Any unmatched remainder
        rests on the book. Every event is mirrored to Supabase.
        """
        with self._lock:
            if not self._admit(bid):
                return []                      # duplicate client_id — already processed
            fills: List[Trade] = []
            self._track(bid)
            if self._sink is not None:
                self._sink.insert_order(bid)
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
        Served from memory — the single writer keeps memory == Postgres.
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
                "persisted": self.persistence_active,
                "bids": bids,   # best (highest price) first
                "asks": asks,   # best (lowest price)  first
            }

    def history(self, limit: int = 50) -> List[Dict]:
        """
        Recent executed trades, newest first. DB-backed when the mirror is
        active; in-memory otherwise. (Kept separate from session stats, which
        always count this process only.)
        """
        if self._sink is not None and self._sink.enabled:
            rows = self._sink.fetch_trades(limit)
            if rows:
                return rows
            # fall through to memory when the DB has nothing (fresh start)
        with self._lock:
            trades = list(self._trades)
        return [t.model_dump() for t in trades[-limit:][::-1]]

    def order_status(self, order_id: str) -> Optional[Dict]:
        """
        Live status of a previously submitted order — used by the web UI's
        "Your orders" panel. Returns None if the id is unknown to this
        process (e.g. submitted before a restart).
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

    def _restore_from_db(self) -> int:
        """
        Rebuild the in-memory heaps from the DB's open orders (called at boot
        when CLAAS_DB_BOOTSTRAP=restore). Orders are tracked so /order/{id}
        keeps working across restarts. Asks get a default hardware tag since
        the orders table (current schema) doesn't store one.
        """
        rows = self._sink.fetch_open_orders()
        for r in rows:
            if r["quantity"] <= 0:
                continue
            if r["side"] == "ask":
                order = Ask(order_id=r["order_id"], provider=r["maker"],
                            hardware="H100", price=r["price"],
                            quantity=r["quantity"],
                            original_quantity=r["quantity"],
                            timestamp=r["timestamp"])
                heapq.heappush(self._asks, (order.price, order.timestamp, order.order_id, order))
                self._track(order)
            else:
                order = Bid(order_id=r["order_id"], startup=r["maker"],
                            price=r["price"], quantity=r["quantity"],
                            original_quantity=r["quantity"],
                            timestamp=r["timestamp"])
                heapq.heappush(self._bids, (-order.price, order.timestamp, order.order_id, order))
                self._track(order)
        return len(self._asks) + len(self._bids)

    def _admit(self, order: Union[Ask, Bid]) -> bool:
        """Idempotency guard: False means this client_id was already submitted."""
        cid = order.client_id
        if not cid:
            return True
        if cid in self._client_ids:
            logger.warning("Duplicate submit ignored (client_id=%s -> order %s)",
                           cid, self._client_ids[cid])
            return False
        self._client_ids[cid] = order.order_id
        while len(self._client_ids) > 2000:
            self._client_ids.pop(next(iter(self._client_ids)))
        return True

    def check_duplicate(self, client_id: Optional[str]) -> Optional[str]:
        """If this client-generated id was used before, return its order id."""
        if not client_id:
            return None
        return self._client_ids.get(client_id)

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
        """Fill ``min(buy.quantity, sell.quantity)`` units at ``price`` and record it."""
        fill = min(buy.quantity, sell.quantity)
        buy.quantity -= fill
        sell.quantity -= fill

        trade = Trade(
            trade_id=f"T-{uuid.uuid4().hex[:8]}",
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
        # Mirror the fill to Postgres (never raises; see SupabaseSink).
        if self._sink is not None:
            self._sink.record_fill(trade, buy, sell)
        logger.info("TRADE  %5d GPU-hr @ $%6.2f | %s <- %s (%s)",
                    fill, price, buy.startup, sell.provider, sell.hardware)
        return trade

    def _prune(self, heap: list) -> None:
        """
        Keep the book tidy: when a side exceeds ``max_depth_per_side``
        resting orders, the *worst-priced* tail is culled (a stand-in for
        real-world order expiry). Evicted orders are marked 'canceled' in
        the DB so the terminal never shows liquidity that isn't really there.
        """
        if len(heap) > self.max_depth_per_side:
            kept = sorted(heap)[: self.max_depth_per_side]
            evicted = [entry for entry in heap if entry not in kept]
            heap[:] = kept
            if self._sink is not None:
                for entry in evicted:
                    self._sink.cancel_order(entry[2])
