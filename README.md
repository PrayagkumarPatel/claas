# 🖥️ CLaaS — Compute-Liquidity-as-a-Service (Local MVP)

A high-frequency spot marketplace simulation for GPU compute (FLOPs / GPU-hours):
node providers (asks) and AI startups (bids) trade GPU-hours on a price/time-priority
central limit order book (CLOB).

**Single-process architecture:** FastAPI serves the core matching engine, background
simulator bots, OpenAPI documentation, and the full dark-mode responsive web trading floor
at `http://localhost:8000`.

```
┌────────────────────────────────────────────────────────┐
│               Unified Port :8000 Process               │
│                                                        │
│  GET /  ──► static/index.html (Web Trading Floor)      │
│  API    ──► /bid, /ask, /orderbook, /history, /order/  │
│  Docs   ──► /docs (OpenAPI Swagger)                    │
│                                                        │
│  engine.py     ── Central Limit Order Book (CLOB)      │
│  simulators.py ── 9 Providers + 8 Startups (24h curve) │
└────────────────────────────────────────────────────────┘
            ▲
            │ (optional power-user desk)
┌───────────────────────┐
│     dashboard.py      │
│   (Streamlit Desk)    │
│  localhost:8501       │
└───────────────────────┘
```

## Quickstart

```bash
cd /Users/prayagkumarpatel/.gemini/antigravity/scratch/claas_mvp
python3 -m pip install -r requirements.txt
```

### Run the Web Platform (One Command, One Port)

```bash
uvicorn api:app --host 0.0.0.0 --port 8000
```

Open your browser to:
- **Web App Trading Floor:** [http://localhost:8000](http://localhost:8000)
- **Interactive API Docs:** [http://localhost:8000/docs](http://localhost:8000/docs)

### Optional: Run the Streamlit Power-User Desk

In a second terminal:
```bash
streamlit run dashboard.py
```
Desk available at: [http://localhost:8501](http://localhost:8501)

---

## File Structure

| File | What it is |
|---|---|
| `static/index.html` | Full marketplace page — hero with live ticker, trading floor, order forms, how-it-works, FAQ |
| `static/styles.css` | Dark trading-terminal design, fully responsive, zero external dependencies |
| `static/app.js` | 1.5s live polling, canvas price chart + sparkline + 24h demand curve, order ladder, trade tape, order tracking |
| `engine.py` | Price-time CLOB, partial fills, maker pricing, and order status indexing (`GET /order/{id}`) |
| `api.py` | Single entry point: serves API + static web app at `/`, manages simulator bots via lifespan |
| `simulators.py` | 9 node providers + 8 AI startups on a compressed 24h demand cycle |
| `dashboard.py` | Optional Streamlit analytics desk |
