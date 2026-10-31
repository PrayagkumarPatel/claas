# CLaaS — Research Dossier

*Compiled 2026-10-30. Companion to the README — the "why" behind the roadmap.*

---

## TL;DR

1. **The field is crowded but nobody owns "financialized compute."** Existing
   GPU marketplaces are *listing sites* (Vast.ai, RunPod) or *auctions*
   (Akash). A continuous limit-order book with a live spot price is a genuinely
   different lens — and it's the part we've already built.
2. **Free, real market data exists.** Azure's Retail Prices API is keyless and
   free; Vast.ai's marketplace API works keyless from residential IPs. Our
   `scripts/real_prices.py` already pulls both and the simulator is now
   calibrated against them.
3. **Signup/safety can be done for $0** with Supabase (auth + Postgres) and
   GitHub's free security tooling — no self-rolled passwords, ever.
4. **Don't rewrite in Rust yet.** The attack surface today is auth/secrets/
   deployment, not Python. Port the engine only when latency actually matters
   (and study the MIT-licensed Rust order books listed below first).
5. **The real moat is execution + trust, not the book.** Every competitor's
   hard problem is running strangers' workloads safely. Plan for that, don't
   build it yet.

---

## 1. Prior art — who's built GPU marketplaces

| Platform | Model | Economics | Lesson for CLaaS |
|---|---|---|---|
| **Vast.ai** | P2P marketplace, hosts set prices, deep consumer-GPU supply | ~10–15% fee, hosts keep ~85% | Trust scores + cheapest-hour positioning wins demand; reputation systems matter from day one |
| **RunPod** | Marketplace + serverless endpoints, strong DX | ~15–20% fee, hosts keep ~75% | Developer experience (SDKs, templates, one-click) is a moat |
| **Salad / SaladCloud** | Consumer idle GPUs via desktop app, workload placement handled for you | ~20–25% effective cut | The long tail of supply exists and will show up for zero effort |
| **Akash Network** | Open-source decentralized cloud (Cosmos); tenants post container manifests, **providers bid** (reverse auction), AKT token | Protocol fees | Closest philosophical cousin — but auction-based, not a CLOB. Fully open code to study |
| **io.net** | DePIN aggregation of datacenter + consumer GPUs | Token incentives | Token subsidies can buy supply-side cold start (with regulatory baggage) |
| **Golem** | Oldest open-source compute marketplace (requestor/provider matching) | GPL-3.0 code | The idea is 8+ years old; execution is what's hard |
| **Render Network** | GPU jobs marketplace for rendering (one of the first "GPU marketplace" ideas) | Token | Vertical-first (rendering) worked to bootstrap |
| **TensorDock, Lium, Hyperstack** | Smaller peer marketplaces | — | Validates continuous new entrants; no dominant design yet |
| **CoreWeave, Lambda, Crusoe, Nebius** | "Neoclouds" — purpose-built GPU clouds | Enterprise contracts | CoreWeave famously started as an ETH-mining operation — our "crypto farm" simulator persona is historically accurate |
| **DigitalOcean, GCP, AWS, Azure, Baseten** | Hyperscalers + managed inference | List + spot pricing | Azure's spot H100s at $1.29–2.04/GPU-hr vs $6.98–11.06 on-demand show how volatile idle-capacity pricing already is |

**The gap we sit in:** everyone above is either a *listing* (pick a machine,
pay the ask) or an *auction* (post a job, get bids). Nobody offers a
**continuous spot market with visible depth, resting orders, and partial
fills** — the trading-terminal experience. That's CLaaS's differentiation,
and it's cheap to demo (already built) even though it's expensive to operate
for real.

---

## 2. Open-source projects to study (and license notes)

| Repo | Language | License | Why study it |
|---|---|---|---|
| [akash-network/node](https://github.com/akash-network/node) + [Akash docs](https://akash.network/development/welcome/) | Go | Open source (verify LICENSE before copying) | Full production marketplace: bidding, lease lifecycle, provider attribution, payments on-chain |
| [golemfactory](https://github.com/shopglobal/golem) | Python/YaRN | **GPL-3.0** | Demand/supply matching + payment rails. ⚠️ GPL is viral — study it, don't paste it into this MIT repo |
| [joaquinbejar/OrderBook-rs](https://github.com/joaquinbejar/OrderBook-rs) | Rust | MIT | What a production matching engine looks like: lock-free, self-trade prevention, fees, journaling, order-state tracking |
| [JOSIAHTHEPROGRAMMER/ironbook](https://github.com/JOSIAHTHEPROGRAMMER/ironbook) | Rust | MIT / Apache-2.0 dual | **Integer tick prices instead of floats** — the correctness upgrade path for our `engine.py` (we currently round floats to cents; real exchanges use scaled integers) |
| [maslowalex/order_book](https://github.com/maslowalex/order_book) | Rust | MIT | Clean educational matcher: FIFO vs pro-rata allocation, time-in-force (IOC/FOK/GTC), stop orders — a menu of our v2 features |

**License hygiene for this repo (MIT):** MIT/Apache-2.0 code can be borrowed
with attribution; GPL-3.0 code must stay out (or the borrowed part gets
relicensed). When in doubt: read the LICENSE, or just link and learn.

---

## 3. Free real-market price APIs (verified 2026-10-30)

### Azure Retail Prices API — the workhorse ✅
- **Free, keyless, no account.** Docs:
  https://learn.microsoft.com/azure/cost-management-billing/manage/retail-prices-azure-api
- Example (H100 offers in East US, live-verified):
  ```
  GET https://prices.azure.com/api/retail/prices?$filter=serviceName eq 'Virtual Machines'
      and priceType eq 'Consumption' and armRegionName eq 'eastus' and contains(skuName,'H100')
  ```
- Verified snapshot (Linux, per GPU-hour): H100 spot **$1.29–$2.04**,
  H100 on-demand **$6.98–$11.06**, T4 **$0.15–$0.53**.

### Vast.ai bundles API — the peer market ⚠️ works, with caveats
- Docs: https://vast.ai/developers/api and
  https://docs.vast.ai/api-reference/search/search-offers
- POST `https://console.vast.ai/api/v0/bundles` with top-level JSON filters
  (`{"limit":50,"type":"on-demand","gpu_name":{"eq":"RTX 4090"},"order":[["dph_total","asc"]]}`).
- Keyless queries generally work; their WAF intermittently 403s datacenter
  IPs (happened to us mid-testing). A **free account API key**
  (`export VAST_API_KEY=...`) makes it reliable — our script supports both.
- Verified snapshot: RTX 4090 median ask **$0.47/GPU-hr** (n=50 offers),
  cheapest $0.13.

### Also on the menu
- **AWS Bulk Price List API** — keyless JSON, but files are enormous; not
  worth it until needed. Spot *history* requires an AWS account.
- **Apify scrapers** (freemium) — normalized Vast/RunPod/Lambda datasets if
  we ever want a zero-maintenance pipeline.

### What we built with this
`scripts/real_prices.py` (stdlib-only) pulls both sources, prints the table
above, and compares it to the running simulation. The simulator's hardware
tiers are now **calibrated to these numbers** (see `simulators.py` header) —
the demo's reference prices are no longer invented.

---

## 4. Signup & safety — phased, all free

### Phase 0 — repo hygiene (do today, costs nothing)
- `.gitignore` excludes `.venv/`, `.env`, agent configs (`.claude/`, `.cursor/`
  …). **Never** commit secrets; if one leaks, rotate it — history is forever.
- On GitHub (free for public repos, cheap for private): enable **secret
  scanning**, **Dependabot alerts**, and **CodeQL** in Settings → Code security.
- `pip-audit` in CI once you have CI.
- App-level: rate-limit the API (`slowapi`), keep Pydantic validation
  (already in), keep output escaping (already in the web UI), tighten CORS
  from `*` to your real domain when deployed, serve over HTTPS (free TLS via
  Render/Fly/Caddy).

### Phase 1 — real accounts, still $0
- **Supabase free tier** (50k MAU, 500 MB Postgres): Google/GitHub OAuth +
  magic-link email. FastAPI verifies the Supabase JWT on every order and
  binds it to a verified user id — replacing the free-text "desk name" with
  real identity. Postgres then persists orders (the bridge from our
  in-memory book to durability).
- Rule: **never roll your own passwords or crypto.** Delegate to Supabase/
  Auth0/Clerk and verify their tokens server-side.

### Phase 2 — money (only after demand is proven)
- **Stripe Connect**: buyers pay the platform, providers get payouts; funds
  can be held (escrow-style) until workload completion. Stripe handles PCI
  and most KYC — you never touch raw cards.
- Lawyer-reviewed ToS/liability, privacy policy, and — critically —
  **export-control screening** before brokering H100-class GPUs
  (US ECCN 3A090.a territory). Get counsel before real high-end listings.

---

## 5. Languages & "safer code" — the honest answer

Python is not the current risk. The risk surface is auth, secrets, and
deployment — none of which a language swap fixes.

- **Now:** stay Python; add `mypy`, keep `pytest`, run `pip-audit`.
- **Frontend:** vanilla JS is fine to a few thousand lines; when it grows,
  TypeScript + Vite (types catch a whole bug class).
- **Engine, later:** if latency/throughput ever matters, port `engine.py` to
  Rust behind a PyO3 binding (or Go). The reference repos in §2 show the
  production patterns: integer ticks, lock-free price levels, self-trade
  prevention, sequenced journals. That's a rewrite for a proven need —
  premature today.

---

## 6. Recommended next milestone

**"Reality-anchored demo + public repo + accounts-ready skeleton."**

1. Push to GitHub (commands in README §"Pushing to GitHub").
2. Run `python3 scripts/real_prices.py` — screenshot the table; it's a great
   pitch artifact ("our simulated market vs. real market data").
3. Wire the real prices into `GET /market` (cache a 5-minute refresh) and add
   a "SIM vs REAL" badge to the web UI.
4. Supabase auth spike: one OAuth button, JWT verified by FastAPI.
5. The concierge pilot: 3–5 real humans trading *something* (campus GPU
   hours, club credits) through this interface.
