# 🖥️ CLaaS — The Spot Market for GPU Compute (local MVP)

A fully local MVP of **Compute-Liquidity-as-a-Service**: a marketplace where
GPU-hours are bought and sold on a live central limit order book. Node
providers list supply, AI teams bid for demand, and a matching engine clears
trades in real time — with a polished web frontend.

> **Honest framing:** the marketplace layer (order book, matching, price
> discovery, web UI) is real code; the participants and liquidity are
> simulated bots. No real compute, payments, or accounts are involved.

```
┌──────────────────────────┐  relative fetch: /orderbook /history …  ┌─────────────────────────────┐
│  Browser — web app       │ ───────────────────────────────────────► │  api.py (FastAPI, 1 process) │
│  static/index.html       │ ◄─────────────────────────────────────── │  serves /  +  /static/*     │
│  styles.css · app.js     │              JSON over HTTP              │  engine.py  — CLOB matching │
│  (vanilla, no build)     │                                         │  (partial fills, spot price)│
└──────────────────────────┘                                         │              ▲              │
                                                                     │ simulators.py (asyncio bots)│
      optional power-user view: streamlit run dashboard.py           │  9 providers + 8 startups   │
      (legacy trading desk) — same API                              └─────────────────────────────┘
```

## Quickstart

### Option A — the launcher (easiest, handles everything)

```bash
./run.sh                # auto-creates .venv, installs deps, starts the server
PORT=9000 ./run.sh      # custom port
```

First run creates a project-local virtual environment (`.venv`) and installs
dependencies — this sidesteps macOS Homebrew's "externally-managed-environment"
error automatically. Later runs start instantly.

### Option B — manual, with a virtual environment (macOS Homebrew Python)

```bash
python3 -m venv .venv            # one time: create the venv
source .venv/bin/activate        # every new terminal: activate it
python -m pip install -r requirements.txt   # one time (or after requirement changes)
uvicorn api:app --host 0.0.0.0 --port 8000
```

(`deactivate` leaves the venv; re-run `source .venv/bin/activate` in each new
terminal. Never use `--break-system-packages` — a venv is the clean fix.)

### Option C — systems where pip works directly (most Linux setups)

```bash
python3 -m pip install -r requirements.txt
uvicorn api:app --host 0.0.0.0 --port 8000
```

Then open **http://localhost:8000** — that's the whole platform: web UI, API,
matching engine, and simulator bots in a single process.

Optional legacy view (separate terminal): `streamlit run dashboard.py`

> Run a **single uvicorn worker** (state is in-memory). `--reload` works for
> development but wipes the book on every code edit.

## What's on the page

| Section | What it does |
|---|---|
| Live ticker | Spot price + tick delta, sparkline, best bid/ask, spread, session volumes |
| Trading floor | Top-6 order book ladder, price chart (last 60 trades), scrolling trade tape |
| Demand curve | The simulated 24h demand cycle (1 trading day = 3 real minutes) with a "now" marker |
| Place an order | **Request compute** (bid) and **Offer compute** (ask) forms — cross the spread to fill instantly, or rest on the book |
| Your orders | Live tracker: fills so far, average fill price, resting remainder — persists in your browser |
| How it works / FAQ | Plain-language market mechanics + honest notes on what's simulated |

## Market semantics

| Concept | Value |
|---|---|
| Commodity | 1 GPU-hour (one GPU for one hour) |
| Prices | USD per GPU-hour, cents precision |
| Matching | Continuous, price-time priority; `bid ≥ ask` executes at the **maker's** price |
| Partial fills | Incoming orders sweep multiple resting levels; remainders rest on the book |
| Spot price | Last executed trade price |
| Demand curve | Simulated 24h day compressed into 3 real minutes (night lull → morning rush → afternoon peak) |

## API

| Endpoint | Purpose |
|---|---|
| `POST /ask` | Submit supply (provider, hardware, price, quantity) |
| `POST /bid` | Submit demand (startup, price, quantity) |
| `GET /orderbook?depth=6` | Best bids/asks, spot price, spread, session stats |
| `GET /history?limit=60` | Executed trades (newest first) + spot price |
| `GET /order/{id}` | Live status of a submitted order (fills, avg price, remainder) |
| `GET /market` | Simulated clock, demand multiplier, hardware reference prices |
| `GET /health`, `GET /about` | Liveness / service banner |
| `GET /docs` | Interactive Swagger reference |

Quick test:

```bash
curl -s -X POST localhost:8000/bid -H 'Content-Type: application/json' \
     -d '{"startup": "My AI Startup", "price": 2.50, "quantity": 100}'
curl -s "localhost:8000/orderbook?depth=5"
curl -s localhost:8000/history?limit=5
```

## Files

| File | Role |
|---|---|
| `engine.py` | CLOB: Pydantic `Ask`/`Bid`/`Trade`, heap-based matching, partial fills, spot price, order-status tracking, optional Supabase mirror (orders + fills persisted to Postgres; matching stays in memory) |
| `api.py` | FastAPI routing layer + static web app + simulator lifecycle |
| `simulators.py` | Asyncio bots: 9 providers quoting every 2–5s; 8 startups on a time-of-day demand curve |
| `static/index.html` `styles.css` `app.js` | The web frontend — vanilla HTML/CSS/JS, no build step, no CDNs. v3 "commodity desk" terminal: monospace data, hairline rules, zero gradients/radius/shadows |
| `dashboard.py` | Optional Streamlit power-desk (legacy view of the same engine) |
| `web/` | Next.js (App Router) + Tailwind + shadcn/ui trading terminal — reads `orders`/`trades` from Supabase Postgres with realtime subscriptions; demo mode without credentials (see `web/README.md`) |
| `ROADMAP.md` | Strategy: architecture, phase gates with exit criteria, unit economics, security & legal checklists, risk register |
| `db/migrations/001_core_schema.sql` | Production schema (profiles, wallets+ledger, nodes, orders, trades, assignments) with full RLS — for a fresh Supabase project |
| `agent/claas_agent.py` | Supplier node agent v0 — GPU detection, register, heartbeat, job listener (execution deliberately not implemented; see ROADMAP §6) |

## Environment knobs

| Variable | Default | Effect |
|---|---|---|
| `RUN_SIMULATORS` | `1` | `0` boots an empty, manual-only market |
| `LOG_LEVEL` | `INFO` | `DEBUG` also logs every resting order / bot tick |
| `SUPABASE_URL` | unset | Set with `SUPABASE_KEY` to mirror all orders/fills to Postgres (the Next.js terminal reads from there). Unset = pure in-memory mode. |
| `SUPABASE_KEY` | unset | **Use the service_role key** (server-side only, e.g. in `.env` — loaded by `./run.sh`, gitignored). The anon key technically works if RLS allows writes, but then anyone holding the public anon key can write your tables. |
| `CLAAS_DB_BOOTSTRAP` | read-only | `cancel` wipes all open orders at boot; `restore` **rebuilds the in-memory book from the DB** (memory becomes a cache of Postgres — restarts lose nothing); default seeds only the spot price. |

Optional columns: the mirror auto-detects your schema. Add `hardware` to `orders`
or `buyer`/`seller`/`hardware` to `trades` and they'll be populated automatically:

```sql
alter table public.trades add column if not exists buyer text, add column if not exists seller text;
```

## Real-market price anchor

`scripts/real_prices.py` pulls **live GPU prices from free public APIs** —
the Azure Retail Prices API (keyless, no account) and the Vast.ai peer
marketplace — and compares them against the simulated market:

```bash
python3 scripts/real_prices.py          # stdlib only, no dependencies
```

The simulator's hardware-tier pricing is calibrated against these real
numbers (see `RESEARCH.md` §3). Optional: `export VAST_API_KEY=...` (free
Vast.ai account) for reliable access to their API.

## Pushing to GitHub

The repo ships with `.gitignore` (venv, secrets, agent configs) and an MIT
`LICENSE`. First push:

```bash
git init
git add .
git commit -m "CLaaS MVP: simulated GPU spot market (CLOB + web terminal)"
git branch -M main
git remote add origin git@github.com:<your-username>/claas.git
git push -u origin main
```

Then switch on GitHub's free security tooling: **Settings → Code security →
secret scanning, Dependabot alerts, CodeQL**.

## From MVP → production (the honest checklist)

The marketplace layer is the *easy* 20%. A real CLaaS also needs:

1. **Execution** (the hard part): containerized workloads, scheduling, GPU
   drivers/CUDA, networking, data transfer in/out, verified disk wipes.
2. **Trust**: escrowed payments, KYC, fraud prevention, SLAs, disputes.
3. **Economics**: 5–15% take rate vs ~3% payment processing; volume first.
4. **Cold start**: two-sided chicken-and-egg — aggregate one side first.
5. **Legal**: ToS/liability, data privacy, export controls on high-end GPUs.
