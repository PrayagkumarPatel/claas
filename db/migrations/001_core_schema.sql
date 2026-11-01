-- ============================================================================
-- CLaaS — 001_core_schema.sql
-- Production schema for the real marketplace (fresh Supabase project).
--
-- RECOMMENDATION: run this in a NEW Supabase project, not the demo project.
-- The demo project currently allows anon-key writes (the old engine wrote
-- with the public anon key) — the product must start clean.
--
-- Design decisions (see ROADMAP.md §3 for reasoning):
--   * One person can be BOTH supplier and buyer (no hard role wall —
--     hard walls kill marketplace liquidity, especially on campuses).
--   * Clients can never write to `orders`/`trades` — only the matching
--     engine (service_role) writes those. This makes fake liquidity,
--     spoofing, and crossed books impossible from the browser.
--   * Agents PULL work: they authenticate as their supplier and poll/
--     subscribe to `assignments` for their own nodes. The database can
--     never reach into a home network to "push" a job.
--   * Wallet balances are service-role-only. Stripe webhooks adjust them.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1) PROFILES — extends Supabase auth.users
-- ---------------------------------------------------------------------------
create table if not exists public.profiles (
  id                        uuid primary key references auth.users(id) on delete cascade,
  display_name              text,
  stripe_connect_account_id text,                          -- set during Connect onboarding
  is_supplier               boolean not null default false, -- a user may hold both hats
  is_verified               boolean not null default false, -- KYC / email verified
  created_at                timestamptz not null default now()
);

-- Auto-create a profile whenever a user signs up.
create or replace function public.handle_new_user()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  insert into public.profiles (id, display_name)
  values (new.id, coalesce(new.raw_user_meta_data->>'full_name', new.email))
  on conflict (id) do nothing;
  return new;
end $$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- ---------------------------------------------------------------------------
-- 2) WALLETS — escrow-style balances (service-role only!)
-- ---------------------------------------------------------------------------
create table if not exists public.wallets (
  user_id       uuid primary key references auth.users(id) on delete cascade,
  balance_cents bigint not null default 0 check (balance_cents >= 0),
  -- ledger lines (append-only) make disputes and audits tractable
  updated_at    timestamptz not null default now()
);

create table if not exists public.wallet_ledger (
  id          bigint generated always as identity primary key,
  user_id     uuid not null references auth.users(id) on delete cascade,
  delta_cents bigint not null,                    -- positive = credit, negative = debit
  reason      text not null,                      -- 'topup', 'escrow_hold', 'escrow_release', 'refund', 'payout'
  trade_id    uuid,                               -- nullable link to the trade that caused it
  created_at  timestamptz not null default now()
);
create index if not exists wallet_ledger_user_idx on public.wallet_ledger (user_id, created_at desc);

-- ---------------------------------------------------------------------------
-- 3) NODES — supplier hardware
-- ---------------------------------------------------------------------------
create table if not exists public.nodes (
  id             uuid primary key default gen_random_uuid(),
  supplier_id    uuid not null references auth.users(id) on delete cascade,
  name           text not null,
  gpus           jsonb not null default '[]',     -- [{"model":"RTX 4090","vram_gb":24,"count":2}]
  region         text,
  benchmark      jsonb,                           -- agent-measured specs (flops, mem bandwidth)
  trust_score    numeric not null default 50,     -- starts neutral, moves with completed jobs
  status         text not null default 'offline'
                   check (status in ('offline','online','busy','retired')),
  agent_version  text,
  last_heartbeat timestamptz,
  created_at     timestamptz not null default now()
);
create index if not exists nodes_supplier_idx on public.nodes (supplier_id);
create index if not exists nodes_status_idx on public.nodes (status);

-- ---------------------------------------------------------------------------
-- 4) ORDERS + TRADES — the book (written ONLY by the engine / service_role)
-- ---------------------------------------------------------------------------
create table if not exists public.orders (
  id                uuid primary key default gen_random_uuid(),
  owner_user_id     uuid references auth.users(id),  -- null = house/bot order
  maker             text not null,
  side              text not null check (side in ('bid','ask')),
  price             numeric(10,2) not null check (price > 0),
  quantity          integer not null check (quantity >= 0),
  original_quantity integer not null check (original_quantity > 0),
  node_id           uuid references public.nodes(id), -- asks: which node supplies
  status            text not null default 'open'
                      check (status in ('open','partial','filled','canceled')),
  created_at        timestamptz not null default now()
);
create index if not exists orders_book_idx    on public.orders (side, status, price);
create index if not exists orders_owner_idx   on public.orders (owner_user_id);
create index if not exists orders_node_idx    on public.orders (node_id);

create table if not exists public.trades (
  id          uuid primary key default gen_random_uuid(),
  bid_id      uuid not null references public.orders(id),
  ask_id      uuid not null references public.orders(id),
  price       numeric(10,2) not null check (price > 0),
  quantity    integer not null check (quantity > 0),
  executed_at timestamptz not null default now()
);
create index if not exists trades_time_idx on public.trades (executed_at desc);
create index if not exists trades_bid_idx  on public.trades (bid_id);
create index if not exists trades_ask_idx  on public.trades (ask_id);

-- ---------------------------------------------------------------------------
-- 5) ASSIGNMENTS — the bridge from a trade to actual running workloads
--    (one trade can fan out to many nodes over time; agents pull these)
-- ---------------------------------------------------------------------------
create table if not exists public.assignments (
  id              uuid primary key default gen_random_uuid(),
  trade_id        uuid not null references public.trades(id),
  buyer_order_id  uuid not null references public.orders(id),
  supplier_order_id uuid not null references public.orders(id),
  node_id         uuid not null references public.nodes(id),
  buyer_id        uuid not null references auth.users(id),
  image           text not null,                  -- CURATED image name (see agent security notes)
  status          text not null default 'pending'
                    check (status in ('pending','running','succeeded','failed','canceled')),
  tunnel_url      text,                           -- buyer-facing access URL
  gpu_seconds     bigint,                         -- agent-reported usage
  started_at      timestamptz,
  finished_at     timestamptz,
  last_heartbeat  timestamptz,
  created_at      timestamptz not null default now()
);
create index if not exists assignments_node_idx   on public.assignments (node_id, status);
create index if not exists assignments_buyer_idx  on public.assignments (buyer_id, created_at desc);

-- ---------------------------------------------------------------------------
-- 6) ROW LEVEL SECURITY
-- ---------------------------------------------------------------------------

-- PROFILES: anyone logged-in can see display names (market attribution);
-- you can update only your own row, and only the safe columns (via trigger
-- guard below — Stripe ids and verification flags are service-role only).
alter table public.profiles enable row level security;
create policy "profiles: authenticated read"
  on public.profiles for select to authenticated using (true);
create policy "profiles: owner update"
  on public.profiles for update to authenticated
  using (auth.uid() = id) with check (auth.uid() = id);

create or replace function public.guard_profile_columns()
returns trigger language plpgsql as $$
begin
  if new.stripe_connect_account_id is distinct from old.stripe_connect_account_id
     or new.is_verified is distinct from old.is_verified then
    raise exception 'columns are service-role managed';
  end if;
  return new;
end $$;

drop trigger if exists profiles_guard on public.profiles;
create trigger profiles_guard before update on public.profiles
  for each row execute function public.guard_profile_columns();

-- WALLETS: read your own; only the service role (Stripe webhooks / engine)
-- can credit or debit. There is deliberately NO client write policy.
alter table public.wallets enable row level security;
create policy "wallets: owner read"
  on public.wallets for select to authenticated using (auth.uid() = user_id);

alter table public.wallet_ledger enable row level security;
create policy "ledger: owner read"
  on public.wallet_ledger for select to authenticated using (auth.uid() = user_id);

-- NODES: public read (marketplace listings); suppliers manage only their own.
alter table public.nodes enable row level security;
create policy "nodes: public read"
  on public.nodes for select using (true);
create policy "nodes: supplier insert own"
  on public.nodes for insert to authenticated
  with check (auth.uid() = supplier_id);
create policy "nodes: supplier update own"
  on public.nodes for update to authenticated
  using (auth.uid() = supplier_id) with check (auth.uid() = supplier_id);
create policy "nodes: supplier delete own"
  on public.nodes for delete to authenticated
  using (auth.uid() = supplier_id);

-- ORDERS / TRADES: world-readable (the book is public data), but clients
-- can NEVER write — only the engine with the service_role key can. This is
-- the single most important anti-manipulation rule in the system.
alter table public.orders enable row level security;
create policy "orders: public read"
  on public.orders for select using (true);
-- (no insert/update/delete policies -> denied for anon and authenticated)

alter table public.trades enable row level security;
create policy "trades: public read"
  on public.trades for select using (true);

-- ASSIGNMENTS: each side sees only its own jobs.
alter table public.assignments enable row level security;
create policy "assignments: buyer reads own"
  on public.assignments for select to authenticated
  using (auth.uid() = buyer_id);
create policy "assignments: supplier reads own-node jobs"
  on public.assignments for select to authenticated
  using (exists (select 1 from public.nodes n
                 where n.id = assignments.node_id and n.supplier_id = auth.uid()));
create policy "assignments: supplier updates own-node jobs"
  on public.assignments for update to authenticated
  using (exists (select 1 from public.nodes n
                 where n.id = assignments.node_id and n.supplier_id = auth.uid()))
  with check (exists (select 1 from public.nodes n
                 where n.id = assignments.node_id and n.supplier_id = auth.uid()));

-- ---------------------------------------------------------------------------
-- 7) REALTIME — push updates to browsers and agents
-- ---------------------------------------------------------------------------
alter publication supabase_realtime add table public.orders;
alter publication supabase_realtime add table public.trades;
alter publication supabase_realtime add table public.nodes;
alter publication supabase_realtime add table public.assignments;

-- ============================================================================
-- NOTES
--   * The engine (service_role) is the ONLY writer of orders/trades and the
--     only minter of wallet movements. Keep the service key server-side.
--   * `wallet_ledger` is append-only by convention; enforce with a
--     before-update/before-delete trigger if you want it belt-and-braces.
--   * Add `hardware`/`buyer`/`seller` columns to orders/trades later if the
--     engine needs them — the engine's mirror auto-detects columns.
-- ============================================================================
