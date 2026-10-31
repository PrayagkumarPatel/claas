# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

- **Primary User 1 (Buyers):** AI engineers, ML researchers, and startup founders seeking on-demand GPU batches without long-term cloud contracts or broker sales calls.
- **Primary User 2 (Sellers):** Node providers, university HPC clusters, crypto mining farms, and colocation data centers seeking to monetize idle compute capacity.
- **Primary User 3 (Visitors/Evaluators):** Technical founders, quants, and investors testing the live marketplace mechanics, price discovery, and order book matching in a zero-friction simulated environment.

## Product Purpose

CLaaS (Compute-Liquidity-as-a-Service) is a high-frequency spot marketplace that treats GPU compute (FLOPs) as a liquid, tradeable financial commodity priced in USD per GPU-hour. It solves GPU scarcity and pricing opacity through a transparent Central Limit Order Book (CLOB). Success means visitors immediately grasp that compute can trade like an equity or commodity, and can actively submit bids or asks with immediate, realistic execution.

## Positioning

Unlike conventional cloud providers (AWS, Lambda, RunPod) that use fixed-price retail or opaque spot instances, CLaaS operates as a pure exchange terminal (Bloomberg meets Linear). The spread between buyers and sellers is the only price negotiation. Continuous price-time priority matching, transparent order books, and partial fills replace sales contracts and fixed tiers.

## Operating Context

- Single-page dark-mode web application running as a local high-frequency trading desk.
- Real-time market state driven by in-memory matching engine and autonomous background bots (9 node providers and 8 AI startups on a simulated 24-hour demand cycle).
- No login or wallet required; browser-local order tracking and zero external dependencies.

## Capabilities and Constraints

- **Core Capabilities:**
  - Continuous limit order book matching with price-time FIFO priority.
  - Partial fill execution (orders sweep resting liquidity across multiple price levels; unmatched remainder rests).
  - Real-time spot price tracking, session tick deltas, and session OHLC/VWAP calculations.
  - Interactive top-6 depth ladder, live scrolling trade tape, 24h demand curve canvas, and browser-tracked order ledger (`/order/{id}`).
  - Quick-pricing order shortcuts (`Pay best ask`, `Outbid +1¢`, `Sell @ best bid`, `Undercut −1¢`).
- **Technical Constraints:**
  - Zero external CDN dependencies, web fonts, or CSS/JS frameworks (100% self-contained, offline-ready).
  - Single process, single port (`:8000`) FastAPI backend serving the HTML/CSS/JS frontend.
  - Must preserve all HTML element IDs and API contracts expected by `app.js` and `api.py`.
  - Frontend only: engine, API routing, and simulators remain untouched.

## Brand Commitments

- **Tone & Persona:** Professional quantitative exchange terminal. Precise, confident, restrained, dark-mode first. Serious financial mechanics without hype or crypto-bro noise.
- **North Star:** "This is a live market, and you can act in it now."
- **Commodity Unit:** GPU-hr (1 GPU allocated for 1 hour).
- **Supported Tiers:** RTX 4090, L40S, A100 80GB, H100 SXM, H200.

## Evidence on Hand

- Fully operational CLOB engine in `engine.py` with verified unit tests for multi-level sweeps and partial fills.
- Dual-loop simulator bots in `simulators.py` running an accelerated 3-minute 24h demand cycle.
- Functional FastAPI endpoints: `POST /bid`, `POST /ask`, `GET /orderbook`, `GET /history`, `GET /order/{id}`, `GET /market`, `GET /health`.

## Product Principles

1. **Live and Actionable Over Static Explanation:** Market prices, depth charts, and trade tapes animate and tick in real time so visitors immediately feel market pulse and can transact with one click.
2. **Terminal Precision and Density:** Information hierarchy mirrors institutional trading software — high data density, aligned columns, tabular figures, and clear visual separation without clutter.
3. **Transparent Honesty:** Proudly disclose the simulated liquidity and real mechanics; no fake hardware orchestration or misleading guarantees.
4. **Resilient Simplicity:** Zero dependencies, fast local loading, and robust keyboard/accessibility affordances.

## Accessibility & Inclusion

- Full keyboard navigability with visible `:focus-visible` focus rings.
- Explicit `role="img"` and `aria-label` tags on canvas visualizers.
- Strict support for `prefers-reduced-motion` to disable animations, transitions, and marquees for sensitive users.
- High-contrast text compliance against dark terminal surfaces.
