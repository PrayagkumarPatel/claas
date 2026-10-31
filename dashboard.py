"""
dashboard.py
============
The CLaaS Trading Desk — a Streamlit frontend over the FastAPI matching
engine. It polls the engine every second and renders:

  * a spot-price metric (+ best bid / ask, spread, session volumes),
  * the order book: a top-5 depth ladder (plotly) plus an exact table,
  * a price tape and a scrolling ledger of recently executed trades,
  * manual bid / ask forms so you can trade against the simulator bots.

The live sections re-render through st.fragment(run_every=1.0) (Streamlit
>= 1.37), which refreshes only the market panel without wiping the input
forms below it.
"""

from __future__ import annotations

import functools
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

# ---------------------------------------------------------------------------
# Page setup
# ---------------------------------------------------------------------------

st.set_page_config(page_title="CLaaS Trading Desk", page_icon="🖥️", layout="wide")

BOOK_DEPTH = 5        # top-N levels per side shown in the ladder
LEDGER_ROWS = 15      # trades visible in the scrolling ledger
HARDWARE_OPTIONS = ["RTX 4090", "L40S", "A100 80GB", "H100 SXM", "H200"]
FRAGMENTS_SUPPORTED = hasattr(st, "fragment")

st.markdown(
    """
    <style>
      .block-container {padding-top: 1.1rem; padding-bottom: 1rem; max-width: 1500px;}
      [data-testid="stMetric"] {
          border: 1px solid #e3e7ee; border-radius: 10px;
          padding: 10px 14px; background: #fafbfd;
      }
      [data-testid="stMetricValue"] {font-size: 1.05rem;}
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def live(run_every: float = 1.0):
    """
    Decorator marking a UI section as live: it re-renders every second via
    st.fragment without rerunning the whole script.
    """
    def decorator(fn):
        if FRAGMENTS_SUPPORTED:
            return st.fragment(run_every=run_every)(fn)

        @functools.wraps(fn)
        def _plain(*args, **kwargs):
            return fn(*args, **kwargs)

        return _plain

    return decorator


def money(x: Optional[float]) -> str:
    """Format a price as $X.XX ('—' when there is no data yet)."""
    return "—" if x is None else f"${x:,.2f}"


def api_get(base: str, path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """GET json from the engine; return None if unreachable."""
    try:
        resp = requests.get(f"{base}{path}", params=params, timeout=2.5)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException:
        return None


def submit_order(base: str, path: str, body: Dict[str, Any]) -> None:
    """POST a manual order to the engine and render the outcome under the form."""
    try:
        resp = requests.post(f"{base}{path}", json=body, timeout=3.0)
    except requests.RequestException as exc:
        st.error(f"Engine unreachable: {exc}")
        return
    if resp.status_code >= 400:
        st.error(f"Rejected ({resp.status_code}): {resp.text}")
        return

    res = resp.json()
    trades: List[Dict[str, Any]] = res.get("trades") or []
    filled = sum(t["quantity"] for t in trades)
    if trades:
        vwap = sum(t["price"] * t["quantity"] for t in trades) / filled
        st.success(
            f"**{res['result'].replace('_', ' ').title()}** — {filled:,} GPU-hrs "
            f"filled at avg **${vwap:,.2f}** across {len(trades)} trade(s)."
        )
    else:
        st.info("Order is **resting** on the book — no crossing counterparty yet.")
    if res.get("remaining_quantity"):
        st.info(
            f"{res['remaining_quantity']:,} GPU-hrs remain resting at "
            f"${body['price']:,.2f} — watch them fill on the ladder above."
        )


# ---------------------------------------------------------------------------
# Chart / table builders
# ---------------------------------------------------------------------------

def ladder_fig(book: Dict[str, Any]) -> go.Figure:
    """Top-of-book depth ladder: green bid bars below the spread, red ask bars above."""
    bids, asks = book["bids"], book["asks"]
    fig = go.Figure()

    if bids:
        fig.add_bar(
            y=[b["price"] for b in bids],
            x=[b["quantity"] for b in bids],
            orientation="h",
            name=f"Bids ({len(bids)})",
            marker_color="#0f9d76",
            hovertemplate="%{customdata[0]}<br>%{x:,} GPU-hrs @ $%{y:.2f}<extra>Bid</extra>",
            customdata=[[b["startup"]] for b in bids],
        )
    if asks:
        fig.add_bar(
            y=[a["price"] for a in asks],
            x=[a["quantity"] for a in asks],
            orientation="h",
            name=f"Asks ({len(asks)})",
            marker_color="#d64550",
            hovertemplate="%{customdata[0]} · %{customdata[1]}<br>%{x:,} GPU-hrs @ $%{y:.2f}<extra>Ask</extra>",
            customdata=[[a["provider"], a["hardware"]] for a in asks],
        )

    if book.get("spread") is not None and bids and asks:
        mid = (book["best_bid"] + book["best_ask"]) / 2
        fig.add_hline(y=mid, line_dash="dot", line_color="#8a94a6",
                      annotation_text=f"spread ${book['spread']:.2f}",
                      annotation_position="right")

    fig.update_layout(
        template="plotly_white",
        height=300,
        margin=dict(l=8, r=8, t=26, b=8),
        bargap=0.28,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        xaxis_title="Quantity (GPU-hours)",
        yaxis_title="Price ($/GPU-hr)",
        yaxis_autorange="reversed",   # asks on top, bids underneath
    )
    return fig


def ladder_df(book: Dict[str, Any]) -> pd.DataFrame:
    """Exact top-5 ladder as a table: bids (left) vs asks (right), level by level."""
    bids, asks = book["bids"], book["asks"]
    rows = []
    for i in range(max(len(bids), len(asks))):
        b = bids[i] if i < len(bids) else None
        a = asks[i] if i < len(asks) else None
        rows.append({
            "Bid Qty":    f"{b['quantity']:,}" if b else "",
            "Bid Price":  f"${b['price']:.2f}" if b else "",
            "Bidder":     b["startup"] if b else "",
            "Provider":   a["provider"] if a else "",
            "Ask Price":  f"${a['price']:.2f}" if a else "",
            "Ask Qty":    f"{a['quantity']:,}" if a else "",
            "Hardware":   a["hardware"] if a else "",
        })
    return pd.DataFrame(rows)


def price_fig(hist: Dict[str, Any]) -> go.Figure:
    """Step-line of recent trade prices (the 'tape')."""
    trades = list(reversed(hist["trades"]))  # oldest -> newest
    fig = go.Figure()
    if trades:
        times = [datetime.fromtimestamp(t["timestamp"]) for t in trades]
        prices = [t["price"] for t in trades]
        fig.add_scatter(
            x=times, y=prices, mode="lines+markers", line_shape="hv",
            line=dict(color="#3563e9", width=1.6),
            marker=dict(size=4),
            hovertemplate="$%{y:.2f} @ %{x|%H:%M:%S}<extra></extra>",
        )
        fig.add_hline(y=prices[-1], line_color="#3563e9", line_dash="dot", opacity=0.6)
    else:
        fig.add_annotation(text="waiting for the first trade…", showarrow=False)
    fig.update_layout(
        template="plotly_white", height=250, margin=dict(l=8, r=8, t=8, b=8),
        yaxis_title="$/GPU-hr", showlegend=False,
    )
    return fig


def trades_df(hist: Dict[str, Any]) -> pd.DataFrame:
    """Scrolling ledger: the most recent trades, newest first."""
    trades = hist["trades"][:LEDGER_ROWS]
    if not trades:
        return pd.DataFrame(columns=["Time", "Price", "GPU-hrs", "Notional",
                                     "Buyer", "Seller", "Hardware"])
    return pd.DataFrame([{
        "Time":     datetime.fromtimestamp(t["timestamp"]).strftime("%H:%M:%S"),
        "Price":    f"${t['price']:.2f}",
        "GPU-hrs":  f"{t['quantity']:,}",
        "Notional": f"${t['price'] * t['quantity']:,.2f}",
        "Buyer":    t["buyer"],
        "Seller":   t["seller"],
        "Hardware": t["hardware"],
    } for t in trades])


# ---------------------------------------------------------------------------
# Sidebar — engine config, market clock & demand curve
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("⚙️ Engine")
    base = st.text_input(
        "Matching engine URL",
        value=os.environ.get("CLAAS_API_URL", "http://localhost:8000"),
    ).rstrip("/")

    if api_get(base, "/health"):
        st.success("Engine online")
    else:
        st.error("Engine unreachable — is `uvicorn api:app --port 8000` running?")

    if not FRAGMENTS_SUPPORTED:
        st.warning("Streamlit ≥ 1.37 needed for 1s auto-refresh; upgrade or use the button.")
        st.button("🔄 Refresh now")

    st.divider()
    st.subheader("🗓 Market clock")
    mkt = api_get(base, "/market")
    if mkt:
        st.markdown(
            f"**{mkt['simulated_clock']}** simulated — "
            f"1 trading day ≈ {int(mkt['real_seconds_per_sim_day'])}s real"
        )
        dem = mkt["demand_multiplier"]
        st.progress(min(dem / 1.4, 1.0), text=f"Demand: {mkt['demand_label']} (×{dem:.2f})")
        st.caption(f"Simulated fair value ≈ {money(mkt['fair_value'])} / GPU-hr")
        with st.expander("Simulated participants"):
            st.markdown("**Providers:** " + ", ".join(mkt["providers"]))
            st.markdown("**Startups:** " + ", ".join(mkt["startups"]))
    else:
        st.caption("Market metadata unavailable.")


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

st.title("🖥️ CLaaS Trading Desk")
st.caption(
    "Compute-Liquidity-as-a-Service · simulated GPU spot market · "
    "prices in **USD per GPU-hour** · refreshes every second"
)


# ---------------------------------------------------------------------------
# Live market panel (auto-refreshes every 1s via st.fragment)
# ---------------------------------------------------------------------------

@live(run_every=1.0)
def market_panel() -> None:
    book = api_get(base, "/orderbook", {"depth": BOOK_DEPTH})
    hist = api_get(base, "/history", {"limit": 60})
    if book is None or hist is None:
        st.info("⏳ Waiting for the matching engine… "
                "start it with `uvicorn api:app --port 8000`.")
        return

    # ---- metrics row -----------------------------------------------------
    spot, prev = book["spot_price"], book["previous_spot_price"]
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric(
        "Spot price (last trade)", money(spot),
        delta=(round(spot - prev, 2) if (spot is not None and prev is not None) else None),
        help="Price of the most recently executed trade — the market's clearing level.",
    )
    c2.metric("Best bid", money(book["best_bid"]))
    c3.metric("Best ask", money(book["best_ask"]))
    c4.metric("Spread", money(book["spread"]))
    c5.metric("Trades (session)", f"{book['total_trades']:,}")
    c6.metric("Volume (GPU-hrs)", f"{book['total_volume']:,}")

    # ---- order book (left) and tape / ledger (right) -----------------------
    left, right = st.columns([3, 2])

    with left:
        st.subheader(f"📒 Order book — top {BOOK_DEPTH}")
        st.plotly_chart(ladder_fig(book), use_container_width=True)
        st.dataframe(ladder_df(book), use_container_width=True,
                     hide_index=True, height=220)

    with right:
        st.subheader("📈 Price tape")
        st.plotly_chart(price_fig(hist), use_container_width=True)
        st.subheader("🧾 Recently executed trades")
        st.dataframe(trades_df(hist), use_container_width=True,
                     hide_index=True, height=220)
        st.caption(f"Notional traded this session: ${book['total_notional']:,.2f}")


market_panel()


# ---------------------------------------------------------------------------
# Manual order entry — trade against the simulator bots
# ---------------------------------------------------------------------------

st.divider()
st.subheader("🎛 Manual order entry")
st.caption(
    "Your orders hit the same matching engine as the bots. A crossing order "
    "fills immediately (partial fills included); otherwise it rests on the "
    "book until a counterparty crosses it."
)

bid_col, ask_col = st.columns(2)

with bid_col:
    with st.form("bid_form", border=True):
        st.markdown("🟢 **Bid — buy compute**")
        bid_name = st.text_input("Startup / desk name", value="My AI Startup", key="bid_name")
        bid_price = st.number_input("Limit price ($/GPU-hr)",
                                    min_value=0.05, max_value=50.0,
                                    value=2.50, step=0.01, key="bid_price")
        bid_qty = st.number_input("Quantity (GPU-hours)",
                                  min_value=1, max_value=100_000,
                                  value=100, step=1, key="bid_qty")
        bid_sent = st.form_submit_button("Submit bid", type="primary")
    if bid_sent:
        submit_order(base, "/bid", {
            "startup": bid_name,
            "price": float(bid_price),
            "quantity": int(bid_qty),
        })

with ask_col:
    with st.form("ask_form", border=True):
        st.markdown("🔴 **Ask — sell compute**")
        ask_name = st.text_input("Provider / desk name", value="My GPU Farm", key="ask_name")
        ask_hw = st.selectbox("Hardware", HARDWARE_OPTIONS, index=3, key="ask_hw")
        ask_price = st.number_input("Limit price ($/GPU-hr)",
                                    min_value=0.05, max_value=50.0,
                                    value=2.50, step=0.01, key="ask_price")
        ask_qty = st.number_input("Quantity (GPU-hours)",
                                  min_value=1, max_value=100_000,
                                  value=50, step=1, key="ask_qty")
        ask_sent = st.form_submit_button("Submit ask", type="primary")
    if ask_sent:
        submit_order(base, "/ask", {
            "provider": ask_name,
            "hardware": ask_hw,
            "price": float(ask_price),
            "quantity": int(ask_qty),
        })
