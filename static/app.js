/* ==========================================================================
   CLaaS — Exchange Web Client Logic
   Vanilla JavaScript, zero external dependencies.
   Communicates with the FastAPI matching engine over relative URLs:
     - Navigation spot & session delta mirror
     - Hero market ticker & session stats (Open, High, Low, VWAP)
     - Interactive top-6 order book depth ladder
     - Real-time trade tape
     - Canvas trade price step chart & hero sparkline
     - Canvas 24-hour demand curve visualizer
     - Order entry forms (Buy & Sell) with quick-pricing buttons
     - Client-side order tracking ledger (/order/{id})
   ========================================================================== */
"use strict";

/* ---------- Utility Helpers ---------- */
const $ = (id) => document.getElementById(id);

const esc = (s) =>
  String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[c]));

const money = (x) =>
  x == null ? "—" : "$" + Number(x).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const qty = (x) => Number(x).toLocaleString("en-US");

const time = (ts) => new Date(ts * 1000).toTimeString().slice(0, 8);

const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* Resilient localStorage wrapper (handles sandboxed iframe constraints) */
const store = {
  get(k) {
    try {
      return JSON.parse(localStorage.getItem(k));
    } catch (e) {
      return null;
    }
  },
  set(k, v) {
    try {
      localStorage.setItem(k, JSON.stringify(v));
    } catch (e) {
      /* In-memory fallback */
    }
  },
};

/* ---------- Application State ---------- */
const state = {
  book: null,
  hist: null,
  market: null,
  orders: [],
  seen: new Set(),
  lastSpot: null,
  tick: 0,
  hour: 12,
};

const POLL_MS = 1500;
const ORDERS_CAP = 12;

/* ---------- Toast Notification Dispatcher ---------- */
function toast(kind, html) {
  const box = $("toasts");
  if (!box) return;
  const t = document.createElement("div");
  t.className = "toast " + kind;
  t.innerHTML = html;
  box.appendChild(t);
  const kill = () => {
    t.remove();
  };
  t.addEventListener("click", kill);
  setTimeout(kill, 5000);
  while (box.children.length > 4) {
    box.firstChild.remove();
  }
}

/* ---------- Canvas Display Preparer ---------- */
function prepCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || canvas.width / dpr;
  const h = canvas.clientHeight || canvas.height / dpr;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}

/* ---------- API Data Fetching ---------- */
async function getJson(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

function setConn(on) {
  const pill = $("connPill");
  if (!pill) return;
  pill.classList.toggle("off", !on);
  const textEl = $("connText");
  if (textEl) {
    textEl.textContent = on ? "LIVE · SIMULATED" : "RECONNECTING…";
  }
}

async function poll() {
  try {
    const [book, hist, market] = await Promise.all([
      getJson("/orderbook?depth=6"),
      getJson("/history?limit=500"),
      getJson("/market"),
    ]);
    state.book = book;
    state.hist = hist;
    state.market = market;
    setConn(true);
    renderAll();
  } catch (e) {
    setConn(false);
  }
}

function renderAll() {
  renderTicker();
  renderLadder();
  renderTape();
  drawPriceChart();
  drawSpark();
  renderDemand();
  renderFormHelpers();
  if (++state.tick % 2 === 0) {
    refreshOrders();
  }
}

/* ---------- Market Ticker & Session Statistics ---------- */
function renderTicker() {
  const b = state.book, h = state.hist;
  if (!b) return;

  const spot = b.spot_price;
  const priceEl = $("spotPrice");
  if (priceEl && spot != null) {
    priceEl.textContent = money(spot);
    if (state.lastSpot != null && spot !== state.lastSpot) {
      priceEl.classList.remove("up", "down");
      void priceEl.offsetWidth;
      priceEl.classList.add(spot > state.lastSpot ? "up" : "down");
      setTimeout(() => priceEl.classList.remove("up", "down"), 800);
    }
  }
  state.lastSpot = spot;

  // Tick Delta
  const d = $("spotDelta");
  const prev = h ? h.previous_spot_price : null;
  if (d) {
    if (spot != null && prev != null && spot !== prev) {
      const diff = spot - prev;
      d.textContent = (diff > 0 ? "+$" : "-$") + Math.abs(diff).toFixed(2);
      d.className = "delta chip mono " + (diff > 0 ? "up" : "down");
    } else {
      d.textContent = spot == null ? "Awaiting trade" : "±$0.00";
      d.className = "delta chip mono flat";
    }
  }

  // Session Stats (Open, High, Low, VWAP)
  const trades = (h && h.trades) || [];
  let open = null, high = null, low = null;
  if (trades.length) {
    open = trades[trades.length - 1].price;
    high = Math.max(...trades.map((t) => t.price));
    low = Math.min(...trades.map((t) => t.price));
  }
  const vwap = h && h.total_volume ? h.total_notional / h.total_volume : null;

  if ($("bestBid")) $("bestBid").textContent = money(b.best_bid);
  if ($("bestAsk")) $("bestAsk").textContent = money(b.best_ask);
  if ($("spread")) $("spread").textContent = b.spread != null ? money(b.spread) : "—";
  if ($("openP")) $("openP").textContent = money(open);
  if ($("highP")) $("highP").textContent = money(high);
  if ($("lowP")) $("lowP").textContent = money(low);
  if ($("vwapP")) $("vwapP").textContent = vwap != null ? money(vwap) : "—";
  if ($("volTrades")) $("volTrades").textContent = qty(b.total_trades);
  if ($("volQty")) $("volQty").textContent = qty(b.total_volume) + " h";
  if ($("volNotional")) $("volNotional").textContent = "$" + Number(b.total_notional).toLocaleString("en-US");

  // Nav Price Mirror
  if ($("navSpot")) $("navSpot").textContent = spot != null ? money(spot) : "$0.00";
  const nd = $("navDelta");
  if (nd) {
    if (spot != null && open != null && open > 0) {
      const pct = ((spot - open) / open) * 100;
      nd.textContent = (pct >= 0 ? "+" : "") + pct.toFixed(2) + "%";
      nd.className = "nav-delta " + (pct > 0 ? "up" : pct < 0 ? "down" : "");
    } else {
      nd.textContent = "0.00%";
      nd.className = "nav-delta";
    }
  }
}

/* ---------- Order Book Ladder ---------- */
function lobRow(o, side, maxQ) {
  const w = Math.max(4, Math.round((o.quantity / maxQ) * 100));
  const who = side === "ask" ? o.provider : o.startup;
  return (
    '<div class="lob-row ' + side + '" title="' + esc(who) + (o.hardware ? " · " + esc(o.hardware) : "") + '">' +
      '<span class="bar" style="width:' + w + '%"></span>' +
      '<span class="px mono">' + money(o.price) + "</span>" +
      '<span class="q mono">' + qty(o.quantity) + "</span>" +
      '<span class="who">' + esc(who) + "</span>" +
      '<span class="hw">' + esc(o.hardware || "Standard") + "</span>" +
    "</div>"
  );
}

function renderLadder() {
  const b = state.book;
  if (!b) return;
  const asks = (b.asks || []).slice(0, 6);
  const bids = (b.bids || []).slice(0, 6);
  const maxQ = Math.max(1, ...asks.map((a) => a.quantity), ...bids.map((x) => x.quantity));

  const askEl = $("askRows");
  const bidEl = $("bidRows");
  const spreadEl = $("spreadRow");

  if (askEl) {
    askEl.innerHTML = asks.length
      ? asks.slice().reverse().map((a) => lobRow(a, "ask", maxQ)).join("")
      : '<p class="empty">No resting asks</p>';
  }
  if (bidEl) {
    bidEl.innerHTML = bids.length
      ? bids.map((x) => lobRow(x, "bid", maxQ)).join("")
      : '<p class="empty">No resting bids</p>';
  }
  if (spreadEl) {
    spreadEl.textContent = b.spread != null ? "SPREAD " + money(b.spread) : "AWAITING LIQUIDITY";
  }
}

/* ---------- Trade Tape ---------- */
function renderTape() {
  const trades = (state.hist && state.hist.trades) || [];
  const tapeEl = $("tapeRows");
  if (!tapeEl) return;

  if (!trades.length) {
    tapeEl.innerHTML = '<p class="empty">Waiting for initial trade execution…</p>';
    return;
  }
  tapeEl.innerHTML = trades.slice(0, 14).map((t) => {
    return (
      '<div class="tape-row ' + (t.aggressor === "bid" ? "up" : "down") + '">' +
        '<span class="t mono">' + time(t.timestamp) + "</span>" +
        '<span class="q mono">' + qty(t.quantity) + "</span>" +
        '<span class="px mono">' + money(t.price) + "</span>" +
        '<span class="who">' + esc(t.buyer) + " ← " + esc(t.seller) + "</span>" +
        '<span class="hw">' + esc(t.hardware || "") + "</span>" +
      "</div>"
    );
  }).join("");
}

/* ---------- Canvas Visualizations ---------- */
function drawSpark() {
  const canvas = $("spark");
  if (!canvas) return;
  const { ctx, w, h } = prepCanvas(canvas);
  const pts = ((state.hist && state.hist.trades) || []).slice().reverse().map((t) => t.price);
  if (pts.length < 2) return;

  const min = Math.min(...pts), max = Math.max(...pts);
  const span = max - min < 0.05 ? 0.05 : max - min;
  const X = (i) => (i / (pts.length - 1)) * (w - 4) + 2;
  const Y = (p) => 6 + (1 - (p - min) / span) * (h - 14);

  ctx.beginPath();
  pts.forEach((p, i) => (i ? ctx.lineTo(X(i), Y(p)) : ctx.moveTo(X(i), Y(p))));
  ctx.strokeStyle = "#4d7cff";
  ctx.lineWidth = 1.8;
  ctx.lineJoin = "round";
  ctx.stroke();

  ctx.lineTo(X(pts.length - 1), h);
  ctx.lineTo(X(0), h);
  ctx.closePath();
  ctx.fillStyle = "rgba(77, 124, 255, 0.08)";
  ctx.fill();
}

function drawPriceChart() {
  const canvas = $("priceChart");
  if (!canvas) return;
  const { ctx, w, h } = prepCanvas(canvas);
  const trades = ((state.hist && state.hist.trades) || []).slice().reverse().slice(-60);
  const pad = { l: 46, r: 14, t: 14, b: 22 };

  ctx.font = "10px ui-monospace, Menlo, monospace";

  if (trades.length < 2) {
    ctx.fillStyle = "#8596b6";
    ctx.textAlign = "center";
    ctx.fillText("Waiting for trades to plot price step line…", w / 2, h / 2);
    return;
  }

  const ps = trades.map((t) => t.price);
  let min = Math.min(...ps), max = Math.max(...ps);
  if (max - min < 0.2) {
    const mid = (max + min) / 2;
    min = mid - 0.1;
    max = mid + 0.1;
  }
  const X = (i) => pad.l + (i / (trades.length - 1)) * (w - pad.l - pad.r);
  const Y = (p) => pad.t + (1 - (p - min) / (max - min)) * (h - pad.t - pad.b);

  // Grid lines
  ctx.textAlign = "right";
  for (let i = 0; i <= 4; i++) {
    const p = min + ((max - min) * i) / 4;
    const y = Y(p);
    ctx.strokeStyle = "rgba(255, 255, 255, 0.06)";
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(w - pad.r, y);
    ctx.stroke();
    ctx.fillStyle = "#8596b6";
    ctx.fillText("$" + p.toFixed(2), pad.l - 6, y + 3);
  }

  // Time labels
  ctx.textAlign = "center";
  [0, Math.floor(trades.length / 2), trades.length - 1].forEach((i) => {
    ctx.fillText(time(trades[i].timestamp), Math.min(Math.max(X(i), 26), w - 26), h - 6);
  });

  // Step Line
  ctx.beginPath();
  trades.forEach((t, i) => {
    if (!i) {
      ctx.moveTo(X(i), Y(t.price));
      return;
    }
    ctx.lineTo(X(i), Y(trades[i - 1].price));
    ctx.lineTo(X(i), Y(t.price));
  });
  ctx.strokeStyle = "#4d7cff";
  ctx.lineWidth = 1.8;
  ctx.lineJoin = "round";
  ctx.stroke();

  // Last Price Line
  const lastP = trades[trades.length - 1].price;
  const ly = Y(lastP);
  ctx.setLineDash([4, 4]);
  ctx.strokeStyle = "#f5b027";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(pad.l, ly);
  ctx.lineTo(w - pad.r, ly);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = "#f5b027";
  ctx.textAlign = "right";
  ctx.fillText("$" + lastP.toFixed(2), w - pad.r - 2, ly - 5);
}

function demandMultiplier(h) {
  const bumps = [[9.5, 1.8, 0.3], [15.0, 2.0, 0.26], [20.5, 2.2, 0.14]];
  const dip = [3.5, 2.6, 0.28];
  const g = (c, wd, amp) => {
    let d = Math.abs(h - c) % 24;
    d = Math.min(d, 24 - d);
    return amp * Math.exp(-(d * d) / (2 * wd * wd));
  };
  let m = 1 + bumps.reduce((s, b) => s + g(...b), 0) - g(...dip);
  return Math.max(0.6, Math.min(1.4, m));
}

function drawDemandCurve() {
  const canvas = $("demandChart");
  if (!canvas) return;
  const { ctx, w, h } = prepCanvas(canvas);
  const pad = { l: 6, r: 6, t: 10, b: 16 };
  const lo = 0.55, hi = 1.45;
  const X = (hr) => pad.l + (hr / 24) * (w - pad.l - pad.r);
  const Y = (m) => pad.t + (1 - (m - lo) / (hi - lo)) * (h - pad.t - pad.b);

  ctx.font = "9.5px ui-monospace, Menlo, monospace";
  ctx.fillStyle = "#8596b6";
  ctx.textAlign = "center";
  [0, 6, 12, 18, 24].forEach((hr) => {
    ctx.fillText(String(hr).padStart(2, "0") + "h", Math.min(Math.max(X(hr), 12), w - 12), h - 3);
  });

  ctx.beginPath();
  for (let hr = 0; hr <= 24.001; hr += 0.25) {
    const x = X(hr), y = Y(demandMultiplier(hr));
    hr ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  }
  ctx.strokeStyle = "#8b5cf6";
  ctx.lineWidth = 1.8;
  ctx.stroke();

  ctx.lineTo(X(24), h - pad.b);
  ctx.lineTo(X(0), h - pad.b);
  ctx.closePath();
  ctx.fillStyle = "rgba(139, 92, 246, 0.08)";
  ctx.fill();

  // Time Marker
  const hr = state.hour % 24;
  const y = Y(demandMultiplier(hr));
  ctx.strokeStyle = "rgba(77, 124, 255, 0.9)";
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.moveTo(X(hr), pad.t);
  ctx.lineTo(X(hr), h - pad.b);
  ctx.stroke();
  ctx.beginPath();
  ctx.arc(X(hr), y, 4, 0, Math.PI * 2);
  ctx.fillStyle = "#4d7cff";
  ctx.fill();
}

function renderDemand() {
  const m = state.market;
  if (!m) return;
  state.hour = m.simulated_hour;
  if ($("simClock")) $("simClock").textContent = m.simulated_clock;
  if ($("demandClock")) $("demandClock").textContent = m.simulated_clock;
  if ($("demandLabel")) $("demandLabel").textContent = m.demand_label;
  if ($("demandMult")) $("demandMult").textContent = "(×" + Number(m.demand_multiplier).toFixed(2) + ")";
  if ($("fairValue")) $("fairValue").textContent = money(m.fair_value);
  drawDemandCurve();
}

/* ---------- Form Helpers & Estimates ---------- */
function updEst() {
  const bp = parseFloat($("bidPrice") ? $("bidPrice").value : 0);
  const bq = parseInt($("bidQty") ? $("bidQty").value : 0, 10);
  const ap = parseFloat($("askPrice") ? $("askPrice").value : 0);
  const aq = parseInt($("askQty") ? $("askQty").value : 0, 10);
  if ($("bidEst")) $("bidEst").textContent = bp > 0 && bq > 0 ? money(bp * bq) : "—";
  if ($("askEst")) $("askEst").textContent = ap > 0 && aq > 0 ? money(ap * aq) : "—";
}

function renderFormHelpers() {
  const b = state.book, m = state.market;
  if (!b) return;

  const bidHint = $("bidHint");
  if (bidHint) {
    bidHint.innerHTML = b.best_ask != null
      ? "Best ask: <b class='mono'>" + money(b.best_ask) + "</b>. Bid at or above this to fill immediately."
      : "No resting asks. Your bid will rest at the top of the buy book.";
  }

  const askHint = $("askHint");
  if (askHint) {
    askHint.innerHTML = b.best_bid != null
      ? "Best bid: <b class='mono'>" + money(b.best_bid) + "</b>. Ask at or below this to sell immediately."
      : "No resting bids. Your ask will rest at the top of the sell book.";
  }

  if (m && m.reference_prices && $("askHw") && $("hwRef")) {
    const hw = $("askHw").value;
    const ref = m.reference_prices[hw];
    $("hwRef").textContent = ref != null
      ? "Hardware baseline for " + hw + ": ≈ " + money(ref) + " / GPU-hr"
      : "Hardware baseline: —";
  }
}

/* ---------- Order Submission ---------- */
function detailMsg(d) {
  if (Array.isArray(d)) return d.map((x) => x.msg).join("; ");
  return typeof d === "string" ? d : JSON.stringify(d);
}

function showResult(el, kind, html) {
  if (!el) return;
  el.className = "result show " + kind;
  el.innerHTML = html;
}

function renderOrderResult(el, j) {
  const trades = j.trades || [];
  const order = j.order;
  if (trades.length) {
    const filled = trades.reduce((s, t) => s + t.quantity, 0);
    const vwap = trades.reduce((s, t) => s + t.price * t.quantity, 0) / filled;
    const fillsTxt = trades.slice(0, 3).map((t) => qty(t.quantity) + " @ " + money(t.price)).join(" · ")
      + (trades.length > 3 ? " · +" + (trades.length - 3) + " more" : "");
    showResult(el, "ok",
      "<b>" + qty(filled) + " of " + qty(order.original_quantity) + " GPU-hrs filled</b> at average price <b class='mono'>" + money(vwap) + "</b>" +
      "<br><span class='fills'>" + fillsTxt + "</span>" +
      "<br><span class='oid'>Order " + esc(order.order_id) + " · tracked in ledger below</span>");
    toast("ok", "<b>✓ " + qty(filled) + " GPU-hrs filled</b> at avg " + money(vwap));
  } else {
    showResult(el, "rest",
      "<b>Resting on order book</b>: " + qty(order.original_quantity) + " GPU-hrs at limit price " + money(order.price) +
      ". No crossing order currently available. Watch it fill in the ledger below.");
    toast("info", "<b>Order resting</b> at " + money(order.price));
  }
}

async function submitOrder(side, body, resultEl) {
  const btn = $(side + "Submit");
  const label = side === "bid" ? "Submit Buy Order" : "Submit Sell Order";
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Routing order…";
  }
  try {
    const r = await fetch("/" + side, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    let j = null;
    try { j = await r.json(); } catch (e) { /* non-json */ }
    if (!r.ok) {
      showResult(resultEl, "error", "<b>Order Rejected (" + r.status + ")</b>: " + esc(j && j.detail ? detailMsg(j.detail) : "Invalid parameters."));
      toast("error", "<b>Order rejected</b> (" + r.status + ")");
      return;
    }
    renderOrderResult(resultEl, j);
    trackOrder(j.order.order_id);
    refreshOrders();
  } catch (e) {
    showResult(resultEl, "error", "<b>Matching Engine Unreachable</b>. Ensure backend is running.");
    toast("error", "<b>Backend unreachable</b>");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = label;
    }
  }
}

/* ---------- Order Tracking Ledger ---------- */
function persistOrders() {
  store.set("claas.orders", state.orders);
}

function trackOrder(id) {
  state.orders = [{ id }, ...state.orders.filter((o) => o.id !== id)].slice(0, ORDERS_CAP);
  persistOrders();
}

function orderRow(s) {
  const pct = s.original_quantity ? Math.round((100 * s.filled_quantity) / s.original_quantity) : 0;
  return (
    "<tr>" +
      '<td class="mono oid">' + esc(s.order_id) + "</td>" +
      '<td><span class="side-chip ' + s.side + '">' + s.side.toUpperCase() + "</span></td>" +
      '<td class="who">' + esc(s.party) + (s.hardware ? ' <span class="hw">' + esc(s.hardware) + "</span>" : "") + "</td>" +
      '<td class="mono">' + money(s.price) + "</td>" +
      "<td>" +
        '<span class="fillbar"><span style="width:' + pct + '%"></span></span>' +
        '<span class="mono filln">' + qty(s.filled_quantity) + " / " + qty(s.original_quantity) + "</span>" +
      "</td>" +
      '<td class="mono">' + (s.avg_fill_price != null ? money(s.avg_fill_price) : "—") + "</td>" +
      '<td><span class="status ' + s.status + '">' + s.status.replace("_", " ") + "</span></td>" +
      '<td><button class="x" data-oid="' + esc(s.order_id) + '" title="Remove from view" aria-label="Remove order ' + esc(s.order_id) + '">×</button></td>' +
    "</tr>"
  );
}

async function refreshOrders() {
  const list = state.orders.slice(0, ORDERS_CAP);
  const bodyEl = $("ordersBody");
  const emptyEl = $("ordersEmpty");
  if (!bodyEl || !emptyEl) return;

  if (!list.length) {
    bodyEl.innerHTML = "";
    emptyEl.style.display = "block";
    return;
  }
  const results = await Promise.all(list.map((o) =>
    getJson("/order/" + encodeURIComponent(o.id)).catch(() => null)));

  const dead = list.filter((o, i) => !results[i]).map((o) => o.id);
  if (dead.length) {
    state.orders = state.orders.filter((o) => !dead.includes(o.id));
    persistOrders();
  }
  const live = results.filter(Boolean);
  if (!live.length) {
    bodyEl.innerHTML = "";
    emptyEl.style.display = "block";
    return;
  }
  emptyEl.style.display = "none";
  bodyEl.innerHTML = live.map(orderRow).join("");
}

/* ---------- Navigation Menu Control ---------- */
function initNav() {
  const burger = $("burger"), menu = $("mobileMenu");
  if (!burger || !menu) return;
  burger.addEventListener("click", () => {
    const open = menu.hidden;
    menu.hidden = !open;
    burger.setAttribute("aria-expanded", String(open));
  });
  menu.querySelectorAll("a").forEach((a) => {
    a.addEventListener("click", () => {
      menu.hidden = true;
      burger.setAttribute("aria-expanded", "false");
    });
  });
}

/* ---------- Initialization ---------- */
function init() {
  state.orders = store.get("claas.orders") || [];
  initNav();

  // Buy Form
  const bidForm = $("bidForm");
  if (bidForm) {
    bidForm.addEventListener("submit", (e) => {
      e.preventDefault();
      const price = parseFloat($("bidPrice").value);
      const quantity = parseInt($("bidQty").value, 10);
      if (!(price > 0) || !(quantity > 0)) {
        showResult($("bidResult"), "error", "Price and quantity must be positive numbers.");
        return;
      }
      submitOrder("bid", {
        startup: $("bidName").value.trim() || "Anonymous Startup",
        price,
        quantity,
      }, $("bidResult"));
    });
  }

  // Sell Form
  const askForm = $("askForm");
  if (askForm) {
    askForm.addEventListener("submit", (e) => {
      e.preventDefault();
      const price = parseFloat($("askPrice").value);
      const quantity = parseInt($("askQty").value, 10);
      if (!(price > 0) || !(quantity > 0)) {
        showResult($("askResult"), "error", "Price and quantity must be positive numbers.");
        return;
      }
      submitOrder("ask", {
        provider: $("askName").value.trim() || "Anonymous Provider",
        hardware: $("askHw").value,
        price,
        quantity,
      }, $("askResult"));
    });
  }

  // Quick Pricing Buttons
  if ($("bidPayAsk")) {
    $("bidPayAsk").addEventListener("click", () => {
      if (state.book && state.book.best_ask != null) {
        $("bidPrice").value = state.book.best_ask.toFixed(2);
        updEst();
      }
    });
  }
  if ($("bidOutbid")) {
    $("bidOutbid").addEventListener("click", () => {
      const bb = state.book && state.book.best_bid;
      $("bidPrice").value = (bb != null ? bb + 0.01 : 2.50).toFixed(2);
      updEst();
    });
  }
  if ($("askMatchBid")) {
    $("askMatchBid").addEventListener("click", () => {
      if (state.book && state.book.best_bid != null) {
        $("askPrice").value = state.book.best_bid.toFixed(2);
        updEst();
      }
    });
  }
  if ($("askUndercut")) {
    $("askUndercut").addEventListener("click", () => {
      const ba = state.book && state.book.best_ask;
      $("askPrice").value = (ba != null ? Math.max(0.05, ba - 0.01) : 2.50).toFixed(2);
      updEst();
    });
  }

  // Live estimate inputs
  ["bidPrice", "bidQty", "askPrice", "askQty"].forEach((id) => {
    const el = $(id);
    if (el) el.addEventListener("input", updEst);
  });
  if ($("askHw")) $("askHw").addEventListener("change", renderFormHelpers);

  // Remove order button delegation
  const ordersBody = $("ordersBody");
  if (ordersBody) {
    ordersBody.addEventListener("click", (e) => {
      const btn = e.target.closest(".x");
      if (!btn) return;
      state.orders = state.orders.filter((o) => o.id !== btn.dataset.oid);
      persistOrders();
      refreshOrders();
    });
  }

  window.addEventListener("resize", () => {
    drawSpark();
    drawPriceChart();
    drawDemandCurve();
  });

  updEst();
  refreshOrders();
  poll();
  setInterval(poll, POLL_MS);
}

document.addEventListener("DOMContentLoaded", init);
