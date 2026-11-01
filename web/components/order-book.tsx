"use client"

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import type { Order, Side } from "@/lib/types"
import { cn, formatPrice, formatQty } from "@/lib/utils"

/**
 * Section 2: the order book.
 * Bids on the left (price desc), asks on the right (price asc),
 * status = 'open' (filtered server-side in hooks/use-live-market.ts).
 * Each row is one open order; the bar behind it is proportional to quantity.
 */

function BookColumn({
  side,
  orders,
  loaded,
  maxQty,
}: {
  side: Side
  orders: Order[]
  loaded: boolean
  maxQty: number
}) {
  const isBid = side === "bid"

  return (
    <div className="min-w-0" aria-label={isBid ? "Open bids" : "Open asks"}>
      <p className="mb-2 text-xs font-bold tracking-wider">
        <span className={isBid ? "text-emerald-400" : "text-red-400"}>
          {isBid ? "BIDS" : "ASKS"}
        </span>{" "}
        <span className="font-normal text-muted-foreground">({orders.length})</span>
      </p>

      <div className="mb-1 grid grid-cols-[4.5rem_4rem_1fr] gap-2 px-2 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
        <span>Price</span>
        <span className="text-right">Qty</span>
        <span className="pl-2">Maker</span>
      </div>

      {!loaded ? (
        <div className="space-y-2" aria-hidden="true">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-7 w-full" />
          ))}
        </div>
      ) : orders.length === 0 ? (
        <p className="px-2 py-8 text-center text-xs text-muted-foreground">
          No open {side}s
        </p>
      ) : (
        <div className="space-y-0.5">
          {orders.map((o) => (
            <div
              key={o.id}
              className="relative grid grid-cols-[4.5rem_4rem_1fr] items-center gap-2 rounded px-2 py-1 text-xs hover:bg-accent/40"
              title={`${o.maker} — ${formatQty(o.quantity)} GPU-hrs @ ${formatPrice(o.price)}`}
            >
              {/* depth bar */}
              <span
                className={cn(
                  "absolute inset-y-0 left-0 rounded",
                  isBid ? "bg-emerald-500/10" : "bg-red-500/10",
                )}
                style={{
                  width: `${Math.max(6, (o.quantity / maxQty) * 100)}%`,
                }}
                aria-hidden="true"
              />
              <span
                className={cn(
                  "relative font-mono font-semibold tabular-nums",
                  isBid ? "text-emerald-400" : "text-red-400",
                )}
              >
                {formatPrice(o.price)}
              </span>
              <span className="relative text-right font-mono tabular-nums text-muted-foreground">
                {formatQty(o.quantity)}
              </span>
              <span className="relative truncate pl-2 text-muted-foreground">
                {o.maker}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export function OrderBook({
  bids,
  asks,
  loaded,
}: {
  bids: Order[]
  asks: Order[]
  loaded: boolean
}) {
  const maxQty = Math.max(
    1,
    ...bids.map((o) => o.quantity),
    ...asks.map((o) => o.quantity),
  )
  const spread =
    bids[0] && asks[0] ? asks[0].price - bids[0].price : null

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0">
        <div>
          <CardTitle className="text-base">Order Book</CardTitle>
          <p className="mt-1 text-xs text-muted-foreground">
            Open orders · GPU-hours · USD per GPU-hour
          </p>
        </div>
        {spread != null && (
          <span className="rounded-md border bg-muted/40 px-2 py-1 font-mono text-xs tabular-nums text-muted-foreground">
            spread {formatPrice(spread)}
          </span>
        )}
      </CardHeader>
      <CardContent className="grid grid-cols-2 gap-6">
        <BookColumn side="bid" orders={bids} loaded={loaded} maxQty={maxQty} />
        <BookColumn side="ask" orders={asks} loaded={loaded} maxQty={maxQty} />
      </CardContent>
    </Card>
  )
}
