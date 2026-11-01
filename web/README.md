# CLaaS Terminal — Next.js frontend

The web trading terminal for the CLaaS GPU compute marketplace.
**Next.js 15 (App Router) + Tailwind CSS + shadcn/ui (dark theme) + Supabase realtime.**

Read-only by design: it subscribes to your `orders` and `trades` tables and
renders them — no matching logic lives here.

```
app/page.tsx                 dashboard page (3 sections)
components/spot-header.tsx   sticky header: spot price + tick delta + connection state
components/order-book.tsx    bids (price desc) | asks (price asc), status = 'open'
components/trade-history.tsx most recent executed trades
hooks/use-live-market.ts     Supabase fetch + realtime subscriptions (+ demo fallback)
lib/supabase-client.ts       browser client singleton (env-driven)
lib/demo-data.ts             simulated feed used when Supabase isn't configured
components/ui/*              shadcn/ui primitives (card, badge, skeleton, table)
```

## Run it

```bash
cd web
npm install
cp .env.local.example .env.local   # then fill in your Supabase URL + anon key
npm run dev                        # http://localhost:3000
```

Without `.env.local` values the app runs in **demo mode** — a simulated feed
drives the same UI (clearly labeled with an amber banner).

## Supabase setup

If you haven't already, run this in the Supabase SQL editor:

```sql
-- Tables (skips if they already exist)
create table if not exists public.orders (
  id       uuid primary key default gen_random_uuid(),
  maker    text not null,
  side     text not null check (side in ('bid','ask')),
  price    numeric not null check (price > 0),
  quantity integer not null check (quantity >= 0),
  status   text not null default 'open'
            check (status in ('open','partial','filled','canceled'))
);
create index if not exists orders_book_idx on public.orders (side, status, price);

create table if not exists public.trades (
  id          uuid primary key default gen_random_uuid(),
  price       numeric not null,
  quantity    integer not null check (quantity > 0),
  executed_at timestamptz not null default now()
);
create index if not exists trades_time_idx on public.trades (executed_at desc);

-- Realtime (skip the "add table" lines if already enabled)
alter publication supabase_realtime add table public.orders;
alter publication supabase_realtime add table public.trades;

-- Row Level Security: the anon key can READ, nothing else.
-- Your matching engine should write via the service-role key (bypasses RLS)
-- or via authenticated server-side sessions — never from the browser.
alter table public.orders enable row level security;
alter table public.trades enable row level security;
create policy "public read orders" on public.orders for select using (true);
create policy "public read trades" on public.trades for select using (true);
```

## Notes worth knowing

- **Postgres `numeric` arrives as a string** in supabase-js. The mapping layer
  in `hooks/use-live-market.ts` coerces prices with `Number()` — keep that if
  you extend the select lists.
- **Partially-filled orders**: the spec filters `status = 'open'`. A
  partially-filled order still has resting quantity, so you may want it on the
  book too — add `"partial"` to `BOOK_STATUSES` in `hooks/use-live-market.ts`.
- **Live updates**: any INSERT/UPDATE/DELETE on either table triggers a
  debounced refetch (simple and always consistent). A 15s safety poll keeps
  data fresh even if realtime isn't enabled on a table.
- **Adding more shadcn components**: the project ships a `components.json`,
  so `npx shadcn@latest add button dialog ...` drops them straight into
  `components/ui/`.
- **Keys**: only the anon (publishable) key belongs in `.env.local`. The
  service-role key must stay server-side only.
