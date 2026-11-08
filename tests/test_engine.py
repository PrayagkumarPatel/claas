"""
tests/test_engine.py — CLaaS engine v3 regression suite.

Covers the behaviors that previous bugs made sacred:
  * order ids are REAL UUIDs (memory + Postgres share one id space)
  * matching semantics: partial fills, maker-price execution, price-time
    priority, multi-level sweeps
  * order tracking (/order/{id}) across the resting -> partial -> filled life
  * prune eviction cancels the worst-priced tail IN THE DB
  * a failing sink NEVER breaks matching, and self-disables after 5 failures
  * CLAAS_DB_BOOTSTRAP modes: default read-only, 'cancel', 'restore'
    (restored orders are NOT re-matched against each other)

Run from the repo root:   python -m pytest tests/ -v
"""
import uuid as uuidlib

import pytest

from engine import OrderBook, new_ask, new_bid


# ---------------------------------------------------------------------------
# Fake sink — same contract as SupabaseSink, no network, no supabase package.
# ---------------------------------------------------------------------------

class FakeSink:
    """Records every write; optional fail_writes mimics a DB outage
    (never raises into the engine — the sink owns that contract —
    counts consecutive failures, self-disables at 5, like SupabaseSink)."""

    MAX_CONSECUTIVE_FAILURES = 5

    def __init__(self, open_rows=None, last_price=None, fail_writes=False):
        self.enabled = True
        self.fail_writes = fail_writes
        self.inserted = []       # (order_id, side, price, qty)
        self.fills = []          # (trade_id, price, qty, bid_id, ask_id)
        self.canceled = []       # order ids marked canceled (evictions)
        self.stale_cancel_calls = 0
        self.open_rows = open_rows or []
        self.last_price = last_price
        self.consecutive_failures = 0

    # -- write path (with SupabaseSink's never-raise + self-disable contract)
    def _write(self, what, record):
        if not self.enabled:
            return
        if self.fail_writes:
            self.consecutive_failures += 1
            if self.consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                self.enabled = False
            return
        record()

    def insert_order(self, order):
        side = "ask" if hasattr(order, "provider") else "bid"
        self._write("insert order",
                    lambda: self.inserted.append((order.order_id, side, order.price, order.quantity)))

    def record_fill(self, trade, buy, sell):
        self._write("record fill",
                    lambda: self.fills.append((trade.trade_id, trade.price, trade.quantity,
                                               buy.order_id, sell.order_id)))

    def cancel_order(self, order_id):
        self._write("cancel order", lambda: self.canceled.append(order_id))

    # -- read path
    def fetch_last_trade_price(self):
        return self.last_price

    def fetch_open_orders(self, limit=500):
        return self.open_rows

    def cancel_stale_open_orders(self):
        self.stale_cancel_calls += 1
        return self.stale_cancel_calls

    def fetch_trades(self, limit=50):
        return []


# ---------------------------------------------------------------------------
# UUID ids
# ---------------------------------------------------------------------------

def test_order_ids_are_real_uuids():
    book = OrderBook()
    ask = new_ask("Nebula", "H100", 2.00, 50)
    bid = new_bid("Aurora", 2.50, 50)
    book.submit_ask(ask)
    fills = book.submit_bid(bid)

    for oid in (ask.order_id, bid.order_id):
        uuidlib.UUID(oid)  # raises ValueError if not a real UUID
    assert fills, "crossing orders must fill"
    uuidlib.UUID(fills[0].bid_id)
    uuidlib.UUID(fills[0].ask_id)


# ---------------------------------------------------------------------------
# Matching semantics
# ---------------------------------------------------------------------------

def test_partial_fill_and_resting_remainder():
    book = OrderBook()
    ask = new_ask("Nebula", "H100", 2.00, 50)
    book.submit_ask(ask)                       # rests
    bid = new_bid("Aurora", 2.50, 100)
    fills = book.submit_bid(bid)               # crosses: takes 50, rests 50

    assert len(fills) == 1
    assert fills[0].price == 2.00              # maker (resting ask) price
    assert fills[0].quantity == 50
    assert fills[0].aggressor == "bid"
    assert book.spot_price == 2.00

    snap = book.snapshot()
    assert snap["ask_count"] == 0              # ask fully filled
    assert snap["bid_count"] == 1              # bid remainder resting

    st = book.order_status(bid.order_id)
    assert st["status"] == "partially_filled"
    assert st["filled_quantity"] == 50
    assert st["remaining_quantity"] == 50
    assert st["avg_fill_price"] == 2.00


def test_price_time_priority_same_level():
    book = OrderBook()
    older = new_ask("OldHost", "H100", 2.00, 30)
    newer = new_ask("NewHost", "H100", 2.00, 30)
    book.submit_ask(older)
    book.submit_ask(newer)

    fills = book.submit_bid(new_bid("Aurora", 2.50, 20))
    assert len(fills) == 1
    assert fills[0].ask_id == older.order_id   # older order fills first


def test_multi_level_sweep_and_avg_price():
    book = OrderBook()
    book.submit_ask(new_ask("HostA", "H100", 2.00, 30))
    book.submit_ask(new_ask("HostB", "H100", 2.10, 40))
    bid = new_bid("Aurora", 2.50, 100)

    fills = book.submit_bid(bid)
    assert [(f.price, f.quantity) for f in fills] == [(2.00, 30), (2.10, 40)]
    assert bid.quantity == 30                  # remainder rests at 2.50
    assert book.spot_price == 2.10             # last print

    st = book.order_status(bid.order_id)
    assert st["filled_quantity"] == 70
    assert st["avg_fill_price"] == pytest.approx((30 * 2.00 + 40 * 2.10) / 70)

    snap = book.snapshot()
    assert snap["total_volume"] == 70
    assert snap["total_notional"] == pytest.approx(144.00)


def test_maker_price_not_taker_price():
    """Taker crosses up to 2.60 but pays the resting maker's 2.55."""
    book = OrderBook()
    book.submit_ask(new_ask("Nebula", "H100", 2.55, 50))
    fills = book.submit_bid(new_bid("Aurora", 2.60, 50))
    assert len(fills) == 1
    assert fills[0].price == 2.55


def test_no_cross_no_trade():
    book = OrderBook()
    book.submit_ask(new_ask("Nebula", "H100", 2.10, 50))
    fills = book.submit_bid(new_bid("Aurora", 2.05, 50))   # bid below ask
    assert fills == []
    snap = book.snapshot()
    assert snap["spread"] == 0.05
    assert book.spot_price is None            # nothing printed yet


# ---------------------------------------------------------------------------
# Sink mirroring + failure isolation
# ---------------------------------------------------------------------------

def test_sink_records_insert_fill_and_eviction_cancel():
    sink = FakeSink()
    book = OrderBook(max_depth_per_side=2, sink=sink)

    a1 = new_ask("A", "H100", 2.00, 10)
    a2 = new_ask("B", "H100", 2.10, 10)
    a3 = new_ask("C", "H100", 2.20, 10)       # worst price -> evicted at depth 2
    book.submit_ask(a1)
    book.submit_ask(a2)
    book.submit_ask(a3)

    assert len(sink.inserted) == 3            # every submission mirrored
    assert sink.canceled == [a3.order_id]     # eviction -> canceled in DB
    assert book.snapshot()["ask_count"] == 2


def test_failing_sink_never_breaks_matching_and_self_disables():
    sink = FakeSink(fail_writes=True)
    book = OrderBook(sink=sink)

    book.submit_ask(new_ask("A", "H100", 2.00, 50))   # fail 1 (insert)
    book.submit_bid(new_bid("X", 2.00, 100))          # fail 2 (insert), fail 3 (fill)
    assert sink.enabled                          # still trying (3 < 5)
    book.submit_ask(new_ask("B", "H100", 1.50, 60))   # fail 4
    book.submit_bid(new_bid("Y", 1.50, 100))          # fail 5 -> DISABLED

    assert sink.consecutive_failures == 5
    assert sink.enabled is False
    assert book.persistence_active is False

    # matching kept working throughout — in memory. Note the full flow
    # produced THREE prints: X×A @2.00, then bid X's 50-unit remainder was
    # swept by ask B @2.00 (maker), then Y×B @1.50 — the engine is right.
    assert book.spot_price == 1.50
    hist = book.history(10)
    assert len(hist) == 3                       # memory fallback after disable
    assert hist[0]["price"] == 1.50             # newest first


def test_default_bootstrap_is_read_only():
    sink = FakeSink(open_rows=[{"order_id": "x", "maker": "m", "side": "ask",
                                "price": 2.0, "quantity": 5, "timestamp": 1.0}])
    book = OrderBook(sink=sink)                 # env unset (conftest guarantees)
    assert book.snapshot()["ask_count"] == 0    # nothing restored
    assert sink.stale_cancel_calls == 0         # nothing canceled


def test_cancel_bootstrap_wipes_stale_opens(monkeypatch):
    sink = FakeSink()
    monkeypatch.setenv("CLAAS_DB_BOOTSTRAP", "cancel")
    book = OrderBook(sink=sink)
    assert sink.stale_cancel_calls == 1
    assert book.snapshot()["ask_count"] == 0


def test_restore_rebuilds_book_without_rematching(monkeypatch):
    """Restored orders rebuild the heaps but are NOT matched against each
    other — even when the restored book is crossed. New flow matches fine."""
    ask_id, bid_id = str(uuidlib.uuid4()), str(uuidlib.uuid4())
    sink = FakeSink(
        open_rows=[
            {"order_id": ask_id, "maker": "Nebula", "side": "ask",
             "price": 2.20, "quantity": 50, "timestamp": 1000.0},
            {"order_id": bid_id, "maker": "Aurora", "side": "bid",
             "price": 2.30, "quantity": 100, "timestamp": 1001.0},  # crossed!
        ],
        last_price=2.05,
    )
    monkeypatch.setenv("CLAAS_DB_BOOTSTRAP", "restore")
    book = OrderBook(sink=sink)

    snap = book.snapshot()
    assert snap["ask_count"] == 1 and snap["bid_count"] == 1
    assert snap["best_ask"] == 2.20 and snap["best_bid"] == 2.30
    assert book.spot_price == 2.05               # seeded from DB
    assert book.total_trades == 0                # NOT re-matched (crossed but quiet)

    # a new taker sweeps the RESTORED liquidity:
    fills = book.submit_bid(new_bid("Taker", 2.25, 30))
    assert len(fills) == 1
    assert fills[0].price == 2.20 and fills[0].quantity == 30
    assert book.spot_price == 2.20

    st = book.order_status(ask_id)               # restored order is tracked
    assert st["status"] == "partially_filled"
    assert st["filled_quantity"] == 30
    assert st["avg_fill_price"] == 2.20
