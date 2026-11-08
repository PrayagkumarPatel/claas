# CLaaS — From MVP to Marketplace: The Roadmap

*Written 2026-11-01, the overnight thinking you asked for. Companion files:
`db/migrations/001_core_schema.sql` (production schema + RLS) and
`agent/claas_agent.py` (node agent v0). Read this with coffee.*

---

## 1. What we already have (assets, not toys)

| Asset | Status | Why it matters |
|---|---|---|
| Matching engine (CLOB, partial fills, price-time priority) | ✅ tested, 15-point health check | The hard exchange logic is DONE and provably correct |
| Supabase mirror + restore-from-DB | ✅ shipped last night | Postgres is the source of truth; memory is a rebuildable cache. Restarts lose nothing |
| Real-time trading terminal (Next.js + shadcn + Tailwind) | ✅ live against your DB | The storefront exists; buyers can already watch the market |
| Real-market price calibration (Azure + Vast.ai APIs) | ✅ verified | Our numbers are defensible in any pitch — rare for a student project |
| Research dossier (competitors, open-source prior art, licensing) | ✅ in RESEARCH.md | We know exactly where the gaps and landmines are |
| Public GitHub repo | ✅ (push when you wake up, if you haven't) | Credibility artifact #1 |

We are further along than most "idea stage" founders ever get: the exchange
works, the terminal works, the data is real. What doesn't exist yet: real
users, real GPUs, real money. The whole game is sequencing those three
without dying.

---

## 2. Verdict on your five-pillar plan

Your plan is directionally right — it reads like the correct end-state. But
five corrections matter enough to change what you build first:

### 2.1 "Replace the in-memory engine with Postgres" — ❌ half-wrong
We *tried* DB-per-matching-step (your engine v2): every match was a network
round-trip, trades failed silently, the book crossed. The proven pattern
(shipped last night): **matching stays in one process; Postgres is the
projection** — orders/fills mirrored as they happen, book rebuilt from the DB
on restart (`CLAAS_DB_BOOTSTRAP=restore`), spot price seeded from the last
trade. You get durability *and* microsecond matching *and* realtime reads.
Scale-out later = event journal + single-writer election, not round-trips.

### 2.2 "The database triggers the Python agent" — ❌ impossible as stated
Supabase cannot reach into a home network behind NAT/firewall. Agents must
**pull**: they sign in as the supplier (JWT), subscribe to `assignments` for
their own nodes via Realtime (or poll — v0 polls). This is also the correct
security shape: the agent's token can only ever touch its own rows (RLS),
and a stolen agent token can't rearrange the book.

### 2.3 Arbitrary container pulls — ❌ the single most dangerous idea in the plan
Running a buyer-supplied image = running arbitrary code on the supplier's
machine. Docker is not a security boundary against a motivated adversary
(kernel attacks, `--gpus all` device access). **MVP rule: buyers choose from
a CURATED image list** (pytorch, jupyter, vllm, …) that the platform has
vetted; arbitrary custom images come years later, if ever, behind gVisor or
Firecracker-grade isolation. Curated images kill 90% of the attack surface
for one afternoon of work.

### 2.4 Stripe per-transaction charges — ❌ loses money at our price scale
Do the math: Stripe takes 2.9% + 30¢. A 2-hour H100 job at $2/hr = $4
charge → fees ≈ 42¢ = **10.5% of GMV** — more than our entire 10% take.
Fix: **wallets**. Top up $20+ once (fees ≈ 4.4%), escrow holds/releases are
ledger entries (free, instant), suppliers cash out weekly in batches.
Wallets also solve the other escrow trap you'd have hit: card authorization
holds expire after ~7 days, but training jobs run longer. Ledger escrow
doesn't expire. (This is in the schema: `wallets` + `wallet_ledger`,
service-role only.)

### 2.5 Small but real
- `trycloudflare.com` quick tunnels are ephemeral and rate-limited; the
  product path is **named tunnels** (free with a Cloudflare account) so a
  node keeps a stable hostname.
- Don't hard-split "Supplier vs Buyer" roles — on a campus, everyone is both.
  One account, two hats (`is_supplier` flag). Hard walls kill liquidity.
- The plan never mentions the **matching→workload bridge**: a trade is
  fungible GPU-hours, but a job runs on a specific node. That bridge
  (allocation service) is a real design problem — §3.3.

---

## 3. Target architecture

```
            ┌────────────────────────────────────────────────┐
            │                 SUPABASE (source of truth)      │
            │  auth.users · profiles · wallets(+ledger)       │
            │  nodes · orders · trades · assignments          │
            │  RLS: clients read; engine writes; agents       │
            │       touch only their own rows                 │
            └───────▲───────────────▲─────────────▲───────────┘
                    │ service_role  │ supplier    │ anon reads +
                    │ (mirrors)     │ JWT (pull)  │ realtime
            ┌───────┴──────┐  ┌─────┴──────┐  ┌───┴──────────────┐
            │ MATCHING     │  │ NODE AGENT │  │ NEXT.JS TERMINAL │
            │ ENGINE (1)   │  │ (per GPU   │  │ book · tape ·    │
            │ in-memory    │  │  owner)    │  │ wallet · jobs    │
            │ CLOB + mirror│  │ detect ·   │  │ auth via Supabase│
            │              │  │ heartbeat ·│  │                  │
            │ ALLOCATION   │  │ run curated│  │ LANDING + DOCS   │
            │ (new, §3.3): │  │ images ·   │  │                  │
            │ trades →     │  │ tunnel ·   │  │                  │
            │ assignments  │  │ report     │  │                  │
            └──────────────┘  └────────────┘  └──────────────────┘
                    ▲                                 │
                    └───── order entry (JWT-bound) ───┘
```

### 3.1 Who writes what (the anti-manipulation constitution)
1. **Only the engine (service_role)** writes `orders`, `trades`, and wallet
   balances. Browsers and agents literally cannot — no RLS policy exists for
   them. No spoofed liquidity, no crossed books, no self-minted money.
2. **Agents** (supplier JWT) write their own `nodes` row and their own
   `assignments` status/heartbeats. Nothing else.
3. **Everyone** reads the book. Transparent by default.

### 3.2 Identity
Supabase Auth (Google + GitHub OAuth + email). The terminal gets a login
button; placing an order requires a session; the engine binds `owner_user_id`
to the verified JWT. Free tier: 50k MAU — irrelevant ceiling for us.

### 3.3 The missing piece: the allocation bridge
The order book trades fungible GPU-hours; workloads run on specific nodes.
The bridge is an **allocation loop** (inside the engine process for now):

```
trade executed
  → create assignment(ask.node_id, buyer, image, price, qty)
  → agent pulls it, runs container, heartbeats every 15s
  → agent reports gpu_seconds periodically
       → engine: wallet debit buyer, credit supplier, take 10% (ledger lines)
  → job done → assignment succeeded
  → node died (heartbeat stale >2min)?
       → assignment canceled, node offline, remainder re-quoted on the book
```

Later this becomes its own service with a queue; don't build that now.

### 3.4 One product idea worth stealing from first principles
Our book pretends an RTX 4090-hour and an H100-hour are different commodities
(tier multipliers). The elegant fix — and a genuine differentiator — is a
**benchmark-normalized unit**: the agent measures real FLOPs on registration
(GPU-burn style), and the book trades *normalized compute-hours*. Suddenly
heterogeneous hardware is ONE commodity, prices become directly comparable,
and our "financialization" story gets teeth. Park it as a Phase 4 experiment.

---

## 4. Sequencing — phases with exit criteria (the discipline)

**The rule: you may not build Phase N+1 until Phase N's exit criterion is met.
This is what kills scope creep and student burnout.**

| Phase | Build | Exit criterion | Est. |
|---|---|---|---|
| **0 — Demo** ✅ | CLOB + terminal + calibration + repo | Done. | done |
| **1 — Identity & persistence** | New Supabase project + `001_core_schema.sql`; engine mirror with `restore`; terminal login; JWT-bound orders | You place an order as a logged-in user; restart the engine and the book survives | 1 week |
| **2 — One real node (concierge++)** | Agent v1 (curated images only, hardened docker, named tunnel) on **your own Mac/GPU**; allocation loop; manual settlement (even Venmo + a spreadsheet) | A friend completes a real Jupyter job on your hardware, booked through the platform, start to finish | 2–4 wks |
| **3 — Money** | Wallets + Stripe Connect (top-ups, escrow ledger, batched payouts); automated allocation; 5–10 campus suppliers | 3 paid trades between people who aren't you, zero manual steps | months 2–3 |
| **4 — Trust & scale** | Reputation scores, benchmark units pilot, hardening review (gVisor?), ToS + export-control screening, growth experiments | Retention: repeat trades without you prompting anyone | months 4–6+ |

**The explicit DO-NOT-BUILD list** (tempting, premature, refuses until later):
futures/derivatives ❌ · Kubernetes ❌ · microservices ❌ · Rust rewrite ❌ ·
mobile app ❌ · multi-region ❌ · arbitrary buyer images ❌ · token/crypto
payments ❌ (regulatory tar pit + zero trust bonus).

---

## 5. Unit economics (know these cold before pitching)

**Transaction costs (the wallet rationale):**
| Charge | Stripe fee | % of charge |
|---|---|---|
| $4 (2 GPU-hrs) | ~$0.42 | 10.5% — *we lose money at our own take rate* |
| $20 wallet top-up | ~$0.88 | 4.4% |
| $100 top-up | ~$3.20 | 3.2% |

→ Wallet top-ups ≥$20 + weekly batched payouts (Connect: ~0.25%/25¢ per
payout). At 10% take and ~4% payment drag, platform keeps ~6% of GMV.

**Supply-side honesty (what we pitch suppliers):**
- H100 (~$25k card) at $2/hr, 85% share, 40% utilization → ~$2,000/mo…
  per 24-GPU HGX system. A single card is a ~4-year payback. **Do not pitch
  "buy GPUs to rent."** Pitch: *monetize capacity you already own idle.*
- RTX 4090 (~$1,600) at $0.45/hr, 85% share, 40% util → ~$130/mo →
  ~12-month payback. This is the consumer-card growth story (Vast.ai's).

**GMV ladder (what "big" requires):**
| Traded volume | GMV/mo (@$1.50 avg) | Platform rev @10% |
|---|---|---|
| 1,000 GPU-hr/day | ~$45k | ~$4.5k |
| 10,000 GPU-hr/day | ~$450k | ~$45k |
| 100,000 GPU-hr/day | ~$4.5M | ~$450k |

Translation: this is a real business only at serious volume — but Phase 2–3
costs ≈ $0 in cash (Supabase/Cloudflare/GitHub free tiers + Stripe
pay-as-you-go), so the experiment is free. The only real spend is your time.

---

## 6. Security & trust (the section that keeps us out of trouble)

**Agent execution checklist (gate for Phase 2 — all must pass):**
- [ ] Curated image allowlist ONLY (platform-vetted tags, digest-pinned)
- [ ] Never `--privileged`; never mount `/var/run/docker.sock`
- [ ] `--user` (non-root), `--cap-drop ALL`, `--security-opt no-new-privileges`
- [ ] Memory / CPU / PID limits per job
- [ ] Dedicated docker network; egress filtering (block metadata IPs, etc.)
- [ ] Named Cloudflare tunnel (not quick tunnels) + auth in front of the service
- [ ] Heartbeat watchdog: stale >2 min → assignment canceled, buyer refunded
      pro-rata, node marked offline
- [ ] Supplier signs an explicit "I am running third-party code" acknowledgment
- [ ] Job logs/egress belong to the buyer's job, not the supplier's other files
      (separate work dirs; document what the agent CAN see)

**Payments & fraud:**
- Wallets cap blast radius (top-ups, not card-on-file charges per job).
- Chargebacks: escrow release waits a dispute window on large amounts.
- New suppliers: first payouts held until first successful jobs complete.

**Export controls (real, not paranoia):** brokering H100/A100-class GPUs
across borders touches US export-control rules (ECCN 3A090 territory) and
sanctions law. Phase 3 answer: **US-only, campus-scoped pilot** with simple
geo-restrictions; get counsel before any cross-border GPU listing.

**NVIDIA licensing:** GeForce cards in commercial datacenter service has
ToS friction; renting a dorm 4090 to a classmate is a gray zone most of the
industry already lives in (Vast hosts thousands of them). Note it, don't
lose sleep over it at pilot scale, revisit at scale.

---

## 7. Risk register (top 8, with mitigations)

| # | Risk | Likelihood | Mitigation |
|---|---|---|---|
| 1 | Two-sided cold start | HIGH | Campus wedge: aggregate supply first (idle lab/dorm GPUs), hand-recruit 5 buyers from ML clubs; concierge until liquidity sticks |
| 2 | Security incident on a supplier machine | MED | §6 checklist; curated images; ToS; start with friends who accept the risk knowingly |
| 3 | Payment fraud / chargebacks | MED | Wallets, dispute windows, payout holds for new suppliers |
| 4 | Export-control exposure | LOW@pilot | US-only campus scope; counsel before scaling |
| 5 | Competitor (Vast/RunPod) adds a CLOB | LOW | Our wedge is the trading-terminal UX + campus trust network; move fast on the niche they ignore |
| 6 | Unit economics misfire | MED | Wallet batching; watch take-rate vs payment drag monthly |
| 7 | Founder burnout (you're a student) | HIGH 😅 | Phase gates; the DO-NOT-BUILD list is a promise to yourself |
| 8 | Engine correctness at scale | LOW | It's the most-tested thing we own; keep the health check in CI |

---

## 8. Tomorrow morning (in order, ~45 min)

1. Sync the engine pack (`claas_sync_2026-10-31.zip` — now includes the
   restore feature) + `pip install supabase` in the venv, per the last message.
2. `CLAAS_DB_BOOTSTRAP=cancel ./run.sh` once → uncross the demo book.
3. Confirm trades persist: place a crossing order in the terminal, watch the
   trades table fill and the spot price go live.
4. Commit + push to GitHub. Enable secret scanning/Dependabot/CodeQL.
5. In Supabase SQL editor (demo project, for now): `alter publication
   supabase_realtime add table public.orders, public.trades;`
6. When ready for Phase 1: create a NEW Supabase project, run
   `db/migrations/001_core_schema.sql`, and we wire the terminal to it.

Then ask me for whichever piece you want built next — my votes, in order:
**(a)** Phase 2 agent v1 with the hardened runner, **(b)** terminal login +
JWT-bound order entry, **(c)** the allocation loop. Sleep well; the exchange
will still be matching when you wake up. 🌙

---

## §11 — Competitive intel addendum (Nov 2026)

Repo teardowns + landscape sweep live in **COMPETITIVE_INTEL.md**. Adoptions already
reflected in phase thinking: AMM liquidity floor for cold start (Phase 2–3 experiment),
outbox-pattern mirror + idempotency keys (engine hardening), NVML telemetry +
quarantine + external prober (agent v1), benchmark-certified hardware tiers (Phase 2).
Graveyard rule reaffirmed: never build listings-first, never crypto-friction,
never Phase 3 before Phase 1 earns it.
