"use client"

import { OrderBook } from "@/components/order-book"
import { SpotHeader } from "@/components/spot-header"
import { TradeHistory } from "@/components/trade-history"
import { useLiveMarket } from "@/hooks/use-live-market"

/**
 * The CLaaS trading terminal dashboard.
 *
 * Section 1: sticky header with the live Spot Price (last executed trade).
 * Section 2: two-column order book — bids (desc) left, asks (asc) right,
 *            status = 'open'.
 * Section 3: trade history feed (most recent executed trades).
 *
 * All data flows through useLiveMarket(): Supabase queries + realtime
 * subscriptions when credentials are configured, a clearly-labeled demo
 * feed when they are not.
 */
export default function DashboardPage() {
  const { bids, asks, trades, spot, prevSpot, connection, loaded } = useLiveMarket()

  return (
    <div className="min-h-screen bg-background">
      <SpotHeader
        spot={spot}
        prevSpot={prevSpot}
        bestBid={bids[0]?.price ?? null}
        bestAsk={asks[0]?.price ?? null}
        connection={connection}
      />

      <main className="mx-auto w-full max-w-7xl px-4 py-6 lg:px-8">
        {connection === "demo" && (
          <div className="mb-6 rounded-lg border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-sm text-amber-200">
            <strong className="font-semibold">Demo mode.</strong> Showing a
            simulated market. To display your real data, set{" "}
            <code className="rounded bg-amber-500/20 px-1 py-0.5">
              NEXT_PUBLIC_SUPABASE_URL
            </code>{" "}
            and{" "}
            <code className="rounded bg-amber-500/20 px-1 py-0.5">
              NEXT_PUBLIC_SUPABASE_ANON_KEY
            </code>{" "}
            in <code className="rounded bg-amber-500/20 px-1 py-0.5">.env.local</code>{" "}
            (see web/README.md) and restart the dev server.
          </div>
        )}

        <div className="grid gap-6 lg:grid-cols-5">
          <div className="lg:col-span-3">
            <OrderBook bids={bids} asks={asks} loaded={loaded} />
          </div>
          <div className="lg:col-span-2">
            <TradeHistory trades={trades} loaded={loaded} />
          </div>
        </div>
      </main>
    </div>
  )
}
