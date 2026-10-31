/* ==========================================================================
   CLaaS — web app logic v2
   Vanilla JS, no dependencies. Talks to the FastAPI engine with relative
   URLs (same origin) and renders:
     nav price mirror · hero ticker (animated spot, OHLC, VWAP) · sparkline
     trade marquee · order book ladder · price chart · trade tape ·
     demand curve · order forms (+ toasts) · live "your orders" tracker
   Order tracking persists in localStorage when available (in-memory else).
   ========================================================================== */
"use strict";

/* ---------------- utilities ---------------- */
const $ = (id) => document.getElementById(id);

const esc = (s) =>
  String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const money = (x) =>
  x == null ? "—" : "$" + Number(x).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const qty = (x) => Number(x).toLocaleString("en-US");

const time = (ts) => new Date(ts * 1000).toTimeString().slice(0, 8);

const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* localStorage can throw inside sandboxed iframes — degrade gracefully. */
const store = {
  get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* in-memory only */ } },
};

/* ---------------- state ---------------- */
const state = {
  book: null,        // latest /orderbook snapshot
  hist: null,        // latest /history snapshot (wide window, for OHLC)
  market: null,      // latest /market snapshot
  orders: [],        // [{id}] tracked order ids (newest first)
  seen: new Set(),   // trade ids already rendered (flash-on-new)
  lastSpot: null,    // previous spot price (tick direction)
  lastMqId: null,    // newest trade id currently in the marquee
  tick: 0,
  hour: 12,          // latest simulated hour (demand curve marker)
};

const POLL_MS = 1500;
const ORDERS_CAP = 12;

/* ---------------- animated number (smooth count-up) ---------------- */
function animateNumber(el, to, fmt) {
  const from = parseFloat(el.dataset.v);
  el.classList.remove("loading");
  if (!isFinite(from) || reduceMotion || from === to) {
    el.textContent = fmt(to);
    el.dataset.v = to;
    return;
  }
  cancelAnimationFrame(el._raf || 0);
  const t0 = performance.now(), dur = 450;
  const step = (t) => {
    const k = Math.min(1, (t - t0) / dur);
    const e = 1 - Math.pow(1 - k, 3); // ease-out cubic
    el.textContent = fmt(from + (to - from) * e);
    if (k < 1) el._raf = requestAnimationFrame(step);
    else el.dataset.v = to;
  };
  el._raf = requestAnimationFrame(step);
}

/* ---------------- toasts ---------------- */
function toast(kind, html) {
  const box = $("toasts");
  if (!box) return;
  const t = document.createElement("div");
  t.className = "toast " + kind;
  t.innerHTML = html;
  box.appendChild(t);
  requestAnimationFrame(() => t.classList.add("show"));
  const kill = () => { t.classList.remove("show"); setTimeout(() => t.remove(), 320); };
  t.addEventListener("click", kill);
  setTimeout(kill, 5200);
  // keep the stack tidy
  while (box.children.length > 4) box.firstChild.remove();
}

/* ---------------- canvas helper ---------------- */
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

/* ---------------- data fetching ---------------- */
async function getJson(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

function setConn(on) {
  const pill = $("connPill");
  pill.classList.toggle("off", !on);
  $("connText").innerHTML = on ? "LIVE&nbsp;·&nbsp;SIMULATED" : "RECONNECTING…";
}

async function poll() {
  try {
    const [book, hist, market] = await Promise.all([
      getJson("/orderbook?depth=6"),
      getJson("/history?limit=500"),   // wide window powers O/H/L; tape/chart slice locally
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
  if (++state.tick % 2 === 0) refreshOrders(); // order tracker every ~3s
}

/* ---------------- ticker + session stats ---------------- */
function renderTicker() {
  const b = state.book, h = state.hist;
  if (!b) return;

  const spot = b.spot_price;
  if (spot != null) animateNumber($("spotPrice"), spot, (v) => "$" + v.toFixed(2));

  // flash the big price green/red on a tick
  const priceEl = $("spotPrice");
  if (spot != null && state.lastSpot != null && spot !== state.lastSpot) {
    priceEl.classList.remove("up", "down");
    void priceEl.offsetWidth;
    priceEl.classList.add(spot > state.lastSpot ? "up" : "down");
    setTimeout(() => priceEl.classList.remove("up", "down"), 900);
  }
  state.lastSpot = spot;

  // hero delta chip: vs previous trade
  const d = $("spotDelta");
  const prev = h ? h.previous_spot_price : null;
  if (spot != null && prev != null && spot !== prev) {
    const diff = spot - prev;
    d.textContent = (diff > 0 ? "▲ +" : "▼ ") + diff.toFixed(2);
    d.className = "delta chip mono " + (diff > 0 ? "up" : "down");
  } else {
    d.textContent = spot == null ? "awaiting first trade" : "±0.00";
    d.className = "delta chip mono flat";
  }

  // session stats from the (wide) trade history
  const trades = (h && h.trades) || [];
  let open = null, high = null, low = null;
  if (trades.length) {
    open = trades[trades.length - 1].price;           // oldest in window = session open
    high = Math.max(...trades.map((t) => t.price));
    low = Math.min(...trades.map((t) => t.price));
  }
  const vwap = h && h.total_volume ? h.total_notional / h.total_volume : null;

  $("bestBid").textContent = money(b.best_bid);
  $("bestAsk").textContent = money(b.best_ask);
  $("spread").textContent = b.spread != null ? money(b.spread) : "—";
  $("openP").textContent = money(open);
  $("highP").textContent = money(high);
  $("lowP").textContent = money(low);
  $("vwapP").textContent = vwap != null ? money(vwap) : "—";
  $("volTrades").textContent = qty(b.total_trades);
  $("volQty").textContent = qty(b.total_volume) + " h";
  $("volNotional").textContent = "$" + Number(b.total_notional).toLocaleString("en-US");

  // nav price mirror: spot + % change vs session open (always visible)
  $("navSpot").textContent = spot != null ? "$" + spot.toFixed(2) : "$—";
  const nd = $("navDelta");
  if (spot != null && open != null && open > 0) {
    const pct = ((spot - open) / open) * 100;
    nd.textContent = (pct >= 0 ? "+" : "") + pct.toFixed(2) + "%";
    nd.className = "nav-delta " + (pct > 0 ? "up" : pct < 0 ? "down" : "");
  } else {
    nd.textContent = "—";
    nd.className = "nav-delta";
  }
}

/* ---------------- order book ladder ---------------- */
function lobRow(o, side, maxQ) {
  const w = Math.max(4, Math.round((o.quantity / maxQ) * 100));
  const who = side === "ask" ? o.provider : o.startup;
  return (
    '<div class="lob-row ' + side + '" title="' + esc(who) + (o.hardware ? " · " + esc(o.hardware) : "") + '">' +
      '<span class="bar" style="width:' + w + '%"></span>' +
      '<span class="px mono">' + money(o.price) + "</span>" +
      '<span class="q mono">' + qty(o.quantity) + "</span>" +
      '<span class="who">' + esc(who) + "</span>" +
      '<span class="hw">' + esc(o.hardware || "") + "</span>" +
    "</div>"
  );
}

function renderLadder() {
  const b = state.book;
  if (!b) return;
  const asks = (b.asks || []).slice(0, 6);
  const bids = (b.bids || []).slice(0, 6);
  const maxQ = Math.max(1, ...asks.map((a) => a.quantity), ...bids.map((x) => x.quantity));

  $("askRows").innerHTML = asks.length
    ? asks.slice().reverse().map((a) => lobRow(a, "ask", maxQ)).join("")
    : '<p class="empty">No resting asks</p>';
  $("bidRows").innerHTML = bids.length
    ? bids.map((x) => lobRow(x, "bid", maxQ)).join("")
    : '<p class="empty">No resting bids</p>';
  $("spreadRow").textContent = b.spread != null ? "SPREAD " + money(b.spread) : "AWAITING LIQUIDITY";
}

/* ---------------- trade tape + marquee ---------------- */
function renderTape() {
  const trades = (state.hist && state.hist.trades) || [];
  if (!trades.length) {
    $("tapeRows").innerHTML = '<p class="empty">Waiting for the first trade…</p>';
    return;
  }
  $("tapeRows").innerHTML = trades.slice(0, 14).map((t) => {
    const isNew = !state.seen.has(t.trade_id);
    return (
      '<div class="tape-row ' + (t.aggressor === "bid" ? "up" : "down") + (isNew ? " new" : "") + '">' +
        '<span class="t mono">' + time(t.timestamp) + "</span>" +
        '<span class="q mono">' + qty(t.quantity) + "</span>" +
        '<span class="px mono">' + money(t.price) + "</span>" +
        '<span class="who">' + esc(t.buyer) + " ← " + esc(t.seller) + "</span>" +
        '<span class="hw">' + esc(t.hardware) + "</span>" +
      "</div>"
    );
  }).join("");
  trades.forEach((t) => state.seen.add(t.trade_id));
  if (state.seen.size > 600) state.seen = new Set([...state.seen].slice(-300));
  renderMarquee(trades);
}

/* Rebuild the hero marquee only when a NEW trade arrives, so the CSS
   scroll animation never restarts mid-loop. Two identical halves make
   translateX(-50%) seamless. */
function renderMarquee(trades) {
  const newest = trades[0];
  if (!newest || newest.trade_id === state.lastMqId) return;
  state.lastMqId = newest.trade_id;

  const items = trades.slice(0, 12).map((t) =>
    '<span class="mq-item ' + (t.aggressor === "bid" ? "up" : "down") + '">' +
      '<span class="p">' + money(t.price) + "</span>" +
      "<span>×" + qty(t.quantity) + "</span>" +
      "<span>" + esc(t.buyer) + " ← " + esc(t.seller) + "</span>" +
    "</span>"
  ).join("");
  const half = '<span class="mq-half">' + items + "</span>";
  $("marqueeTrack").innerHTML = half + half;
}

/* ---------------- charts ---------------- */
function drawSpark() {
  const canvas = $("spark");
  const { ctx, w, h } = prepCanvas(canvas);
  const pts = ((state.hist && state.hist.trades) || []).slice().reverse().map((t) => t.price);
  if (pts.length < 2) return;
  const min = Math.min(...pts), max = Math.max(...pts);
  const span = max - min < 0.05 ? 0.05 : max - min;
  const X = (i) => (i / (pts.length - 1)) * (w - 4) + 2;
  const Y = (p) => 6 + (1 - (p - min) / span) * (h - 14);

  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, "rgba(110,168,255,.28)");
  grad.addColorStop(1, "rgba(110,168,255,0)");

  ctx.beginPath();
  pts.forEach((p, i) => (i ? ctx.lineTo(X(i), Y(p)) : ctx.moveTo(X(i), Y(p))));
  ctx.strokeStyle = "#6ea8ff";
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.stroke();
  ctx.lineTo(X(pts.length - 1), h);
  ctx.lineTo(X(0), h);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();
}

function drawPriceChart() {
  const canvas = $("priceChart");
  const { ctx, w, h } = prepCanvas(canvas);
  const trades = ((state.hist && state.hist.trades) || []).slice().reverse().slice(-60);
  const pad = { l: 46, r: 14, t: 14, b: 22 };

  ctx.font = "10px ui-monospace, Menlo, monospace";

  if (trades.length < 2) {
    ctx.fillStyle = "#6b7a99";
    ctx.textAlign = "center";
    ctx.fillText(trades.length ? "waiting for more trades…" : "waiting for the first trade…", w / 2, h / 2);
    return;
  }

  const ps = trades.map((t) => t.price);
  let min = Math.min(...ps), max = Math.max(...ps);
  if (max - min < 0.2) { const mid = (max + min) / 2; min = mid - 0.1; max = mid + 0.1; }
  const X = (i) => pad.l + (i / (trades.length - 1)) * (w - pad.l - pad.r);
  const Y = (p) => pad.t + (1 - (p - min) / (max - min)) * (h - pad.t - pad.b);

  // grid + price labels
  ctx.textAlign = "right";
  for (let i = 0; i <= 4; i++) {
    const p = min + ((max - min) * i) / 4;
    const y = Y(p);
    ctx.strokeStyle = "rgba(255,255,255,.05)";
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
    ctx.fillStyle = "#6b7a99";
    ctx.fillText("$" + p.toFixed(2), pad.l - 7, y + 3);
  }

  // x labels
  ctx.textAlign = "center";
  [0, Math.floor(trades.length / 2), trades.length - 1].forEach((i) => {
    ctx.fillText(time(trades[i].timestamp), Math.min(Math.max(X(i), 26), w - 26), h - 6);
  });

  // step line
  ctx.beginPath();
  trades.forEach((t, i) => {
    if (!i) { ctx.moveTo(X(i), Y(t.price)); return; }
    ctx.lineTo(X(i), Y(trades[i - 1].price));
    ctx.lineTo(X(i), Y(t.price));
  });
  ctx.strokeStyle = "#6ea8ff";
  ctx.lineWidth = 1.8;
  ctx.lineJoin = "round";
  ctx.stroke();

  // soft fill under the line
  ctx.lineTo(X(trades.length - 1), h - pad.b);
  ctx.lineTo(X(0), h - pad.b);
  ctx.closePath();
  const fill = ctx.createLinearGradient(0, pad.t, 0, h - pad.b);
  fill.addColorStop(0, "rgba(110,168,255,.16)");
  fill.addColorStop(1, "rgba(110,168,255,0)");
  ctx.fillStyle = fill;
  ctx.fill();

  // last-price marker + tag
  const lastP = trades[trades.length - 1].price;
  const ly = Y(lastP);
  ctx.setLineDash([4, 4]);
  ctx.strokeStyle = "rgba(251,191,36,.65)";
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(pad.l, ly); ctx.lineTo(w - pad.r, ly); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = "#fbbf24";
  ctx.textAlign = "right";
  ctx.fillText("$" + lastP.toFixed(2), w - pad.r - 2, ly - 5);
}

/* Demand curve: mirror of simulators.demand_multiplier() over a 24h day. */
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
  const { ctx, w, h } = prepCanvas(canvas);
  const pad = { l: 6, r: 6, t: 10, b: 16 };
  const lo = 0.55, hi = 1.45;
  const X = (hr) => pad.l + (hr / 24) * (w - pad.l - pad.r);
  const Y = (m) => pad.t + (1 - (m - lo) / (hi - lo)) * (h - pad.t - pad.b);

  // hour labels
  ctx.font = "9.5px ui-monospace, Menlo, monospace";
  ctx.fillStyle = "#6b7a99";
  ctx.textAlign = "center";
  [0, 6, 12, 18, 24].forEach((hr) => ctx.fillText(String(hr).padStart(2, "0") + "h", Math.min(Math.max(X(hr), 12), w - 12), h - 3));

  // curve + fill
  ctx.beginPath();
  for (let hr = 0; hr <= 24.001; hr += 0.25) {
    const x = X(hr), y = Y(demandMultiplier(hr));
    hr ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  }
  ctx.strokeStyle = "#a78bfa";
  ctx.lineWidth = 2;
  ctx.stroke();
  ctx.lineTo(X(24), h - pad.b);
  ctx.lineTo(X(0), h - pad.b);
  ctx.closePath();
  const fill = ctx.createLinearGradient(0, pad.t, 0, h - pad.b);
  fill.addColorStop(0, "rgba(167,139,250,.22)");
  fill.addColorStop(1, "rgba(167,139,250,0)");
  ctx.fillStyle = fill;
  ctx.fill();

  // "now" marker
  const hr = state.hour % 24;
  const y = Y(demandMultiplier(hr));
  ctx.strokeStyle = "rgba(103,232,249,.8)";
  ctx.lineWidth = 1.5;
  ctx.beginPath(); ctx.moveTo(X(hr), pad.t); ctx.lineTo(X(hr), h - pad.b); ctx.stroke();
  ctx.beginPath(); ctx.arc(X(hr), y, 4.5, 0, Math.PI * 2);
  ctx.fillStyle = "#67e8f9"; ctx.fill();
  ctx.strokeStyle = "#05070d"; ctx.lineWidth = 2; ctx.stroke();
}

function renderDemand() {
  const m = state.market;
  if (!m) return;
  state.hour = m.simulated_hour;
  $("simClock").textContent = m.simulated_clock;
  $("demandClock").textContent = m.simulated_clock;
  $("demandLabel").textContent = m.demand_label;
  $("demandMult").textContent = "(×" + Number(m.demand_multiplier).toFixed(2) + ")";
  $("fairValue").textContent = money(m.fair_value);
  drawDemandCurve();
}

/* ---------------- form helpers ---------------- */
function updEst() {
  const bp = parseFloat($("bidPrice").value), bq = parseInt($("bidQty").value, 10);
  const ap = parseFloat($("askPrice").value), aq = parseInt($("askQty").value, 10);
  $("bidEst").textContent = bp > 0 && bq > 0 ? money(bp * bq) : "—";
  $("askEst").textContent = ap > 0 && aq > 0 ? money(ap * aq) : "—";
}

function renderFormHelpers() {
  const b = state.book, m = state.market;
  if (!b) return;
  $("bidHint").innerHTML = b.best_ask != null
    ? "Best ask <b class='mono'>" + money(b.best_ask) + "</b> — bid at or above it to fill instantly."
    : "No asks resting — your bid will sit at the top of the book.";
  $("askHint").innerHTML = b.best_bid != null
    ? "Best bid <b class='mono'>" + money(b.best_bid) + "</b> — ask at or below it to sell instantly."
    : "No bids resting — your ask will be first on the book.";
  if (m && m.reference_prices) {
    const hw = $("askHw").value;
    const ref = m.reference_prices[hw];
    $("hwRef").textContent = ref != null
      ? "Market reference for " + hw + ": ≈ " + money(ref) + " / GPU-hour (live fair value × tier)"
      : "Market reference: —";
  }
}

/* ---------------- order submission ---------------- */
function detailMsg(d) {
  if (Array.isArray(d)) return d.map((x) => x.msg).join("; ");
  return typeof d === "string" ? d : JSON.stringify(d);
}

function showResult(el, kind, html) {
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
      "<b>" + qty(filled) + " of " + qty(order.original_quantity) + " GPU-hrs filled</b> " +
      "at avg <b class='mono'>" + money(vwap) + "</b>" +
      "<br><span class='fills'>" + fillsTxt + "</span>" +
      "<br><span class='oid'>Order " + esc(order.order_id) + " · tracked below · fee $0.00 (0% MVP)</span>");
    toast("ok", "<b>✓ " + qty(filled) + " GPU-hrs filled</b> at avg " + money(vwap));
  } else {
    showResult(el, "rest",
      "<b>Resting on the book</b> — " + qty(order.original_quantity) + " GPU-hrs at " + money(order.price) +
      ". No crossing counterparty yet; watch it fill in “Your orders”.");
    toast("info", "<b>Order resting</b> at " + money(order.price) + " — watching for fills");
  }
}

async function submitOrder(side, body, resultEl) {
  const btn = $(side + "Submit");
  const label = side === "bid" ? "Place bid" : "Place ask";
  btn.disabled = true;
  btn.textContent = "Submitting…";
  try {
    const r = await fetch("/" + side, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    let j = null;
    try { j = await r.json(); } catch (e) { /* non-json error body */ }
    if (!r.ok) {
      showResult(resultEl, "error", "<b>Rejected (" + r.status + ").</b> " + esc(j && j.detail ? detailMsg(j.detail) : "Invalid order."));
      toast("error", "<b>Order rejected</b> (" + r.status + ")");
      return;
    }
    renderOrderResult(resultEl, j);
    trackOrder(j.order.order_id);
    refreshOrders();
  } catch (e) {
    showResult(resultEl, "error", "<b>Engine unreachable.</b> Is the backend running on this origin?");
    toast("error", "<b>Engine unreachable</b> — is the server running?");
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

/* ---------------- order tracking ---------------- */
function persistOrders() { store.set("claas.orders", state.orders); }

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
        '<span class="mono filln">' + qty(s.filled_quantity) + "/" + qty(s.original_quantity) + "</span>" +
      "</td>" +
      '<td class="mono">' + (s.avg_fill_price != null ? money(s.avg_fill_price) : "—") + "</td>" +
      '<td><span class="status ' + s.status + '">' + s.status.replace("_", " ") + "</span></td>" +
      '<td><button class="x" data-oid="' + esc(s.order_id) + '" title="Stop tracking" aria-label="Stop tracking order ' + esc(s.order_id) + '">×</button></td>' +
    "</tr>"
  );
}

async function refreshOrders() {
  const list = state.orders.slice(0, ORDERS_CAP);
  if (!list.length) {
    $("ordersBody").innerHTML = "";
    $("ordersEmpty").style.display = "block";
    return;
  }
  const results = await Promise.all(list.map((o) =>
    getJson("/order/" + encodeURIComponent(o.id)).catch(() => null)));
  // Drop ids the engine no longer knows (restart / eviction) and re-render.
  const dead = list.filter((o, i) => !results[i]).map((o) => o.id);
  if (dead.length) {
    state.orders = state.orders.filter((o) => !dead.includes(o.id));
    persistOrders();
  }
  const live = results.filter(Boolean);
  if (!live.length) {
    $("ordersBody").innerHTML = "";
    $("ordersEmpty").style.display = "block";
    return;
  }
  $("ordersEmpty").style.display = "none";
  $("ordersBody").innerHTML = live.map(orderRow).join("");
}

/* ---------------- scroll reveal ---------------- */
function initReveal() {
  const els = document.querySelectorAll(".reveal");
  if (reduceMotion || !("IntersectionObserver" in window)) {
    els.forEach((el) => el.classList.add("in"));
    return;
  }
  const io = new IntersectionObserver((entries) => {
    entries.forEach((en) => {
      if (en.isIntersecting) { en.target.classList.add("in"); io.unobserve(en.target); }
    });
  }, { threshold: 0.12 });
  els.forEach((el) => io.observe(el));
}

/* ---------------- mobile nav ---------------- */
function initNav() {
  const burger = $("burger"), menu = $("mobileMenu");
  burger.addEventListener("click", () => {
    const open = menu.hidden;
    menu.hidden = !open;
    burger.setAttribute("aria-expanded", String(open));
  });
  menu.querySelectorAll("a").forEach((a) => a.addEventListener("click", () => {
    menu.hidden = true;
    burger.setAttribute("aria-expanded", "false");
  }));
}

/* ---------------- wiring ---------------- */
function init() {
  state.orders = store.get("claas.orders") || [];
  initNav();
  initReveal();

  // forms
  $("bidForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const price = parseFloat($("bidPrice").value);
    const quantity = parseInt($("bidQty").value, 10);
    if (!(price > 0) || !(quantity > 0)) {
      showResult($("bidResult"), "error", "<b>Invalid order.</b> Price and quantity must be positive numbers.");
      return;
    }
    submitOrder("bid", {
      startup: $("bidName").value.trim() || "Anonymous Startup",
      price, quantity,
    }, $("bidResult"));
  });

  $("askForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const price = parseFloat($("askPrice").value);
    const quantity = parseInt($("askQty").value, 10);
    if (!(price > 0) || !(quantity > 0)) {
      showResult($("askResult"), "error", "<b>Invalid order.</b> Price and quantity must be positive numbers.");
      return;
    }
    submitOrder("ask", {
      provider: $("askName").value.trim() || "Anonymous Provider",
      hardware: $("askHw").value,
      price, quantity,
    }, $("askResult"));
  });

  // quick-pricing buttons
  $("bidPayAsk").addEventListener("click", () => {
    if (state.book && state.book.best_ask != null) { $("bidPrice").value = state.book.best_ask.toFixed(2); updEst(); }
  });
  $("bidOutbid").addEventListener("click", () => {
    const bb = state.book && state.book.best_bid;
    $("bidPrice").value = (bb != null ? bb + 0.01 : 2.5).toFixed(2); updEst();
  });
  $("askMatchBid").addEventListener("click", () => {
    if (state.book && state.book.best_bid != null) { $("askPrice").value = state.book.best_bid.toFixed(2); updEst(); }
  });
  $("askUndercut").addEventListener("click", () => {
    const ba = state.book && state.book.best_ask;
    $("askPrice").value = (ba != null ? ba - 0.01 : 2.5).toFixed(2); updEst();
  });

  // live estimates + hardware reference line
  ["bidPrice", "bidQty", "askPrice", "askQty"].forEach((id) => $(id).addEventListener("input", updEst));
  $("askHw").addEventListener("change", renderFormHelpers);

  // untrack buttons (event delegation)
  $("ordersBody").addEventListener("click", (e) => {
    const btn = e.target.closest(".x");
    if (!btn) return;
    state.orders = state.orders.filter((o) => o.id !== btn.dataset.oid);
    persistOrders();
    refreshOrders();
  });

  // redraw canvases on resize
  window.addEventListener("resize", () => { drawSpark(); drawPriceChart(); drawDemandCurve(); });

  updEst();
  refreshOrders();
  poll();
  setInterval(poll, POLL_MS);
}

document.addEventListener("DOMContentLoaded", init);
