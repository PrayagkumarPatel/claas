"use client"

import { Zap } from "lucide-react"

import type { ConnectionState } from "@/hooks/use-live-market"
import { cn, formatPrice } from "@/lib/utils"

/**
 * Section 1: sticky terminal header.
 * Shows the live Spot Price (most recent trade) with its tick delta,
 * best bid / ask, and the realtime connection state.
 */

const CONNECTION_STYLES: Record<
  ConnectionState,
  { label: string; dot: string; cls: string }
> = {
  live: {
    label: "LIVE",
    dot: "bg-emerald-400",
    cls: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
  },
  demo: {
    label: "DEMO DATA",
    dot: "bg-amber-400",
    cls: "border-amber-500/40 bg-amber-500/10 text-amber-300",
  },
  connecting: {
    label: "CONNECTING",
    dot: "bg-zinc-400",
    cls: "border-zinc-500/40 bg-zinc-500/10 text-zinc-300",
  },
  offline: {
    label: "OFFLINE",
    dot: "bg-red-400",
    cls: "border-red-500/40 bg-red-500/10 text-red-300",
  },
}

function ConnectionBadge({ state }: { state: ConnectionState }) {
  const s = CONNECTION_STYLES[state]
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-bold tracking-wider",
        s.cls,
      )}
      role="status"
      aria-label={`Connection: ${s.label}`}
    >
      <span className={cn("h-1.5 w-1.5 animate-pulse rounded-full", s.dot)} />
      {s.label}
    </span>
  )
}

export function SpotHeader({
  spot,
  prevSpot,
  bestBid,
  bestAsk,
  connection,
}: {
  spot: number | null
  prevSpot: number | null
  bestBid: number | null
  bestAsk: number | null
  connection: ConnectionState
}) {
  const delta =
    spot != null && prevSpot != null && spot !== prevSpot ? spot - prevSpot : null

  return (
    <header className="sticky top-0 z-40 border-b bg-background/80 backdrop-blur supports-[backdrop-filter]:bg-background/60">
      <div className="mx-auto flex w-full max-w-7xl flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3 lg:px-8">
        {/* brand */}
        <div className="flex items-center gap-2.5">
          <span className="grid h-8 w-8 place-items-center rounded-lg bg-primary text-primary-foreground">
            <Zap className="h-4 w-4" aria-hidden="true" />
          </span>
          <div>
            <p className="text-sm font-bold leading-none tracking-tight">
              CLaaS Terminal
            </p>
            <p className="mt-0.5 font-mono text-[11px] text-muted-foreground">
              GPU-HR · SPOT
            </p>
          </div>
        </div>

        {/* spot price + tick delta */}
        <div className="flex items-baseline gap-3" aria-label="Spot price">
          <span className="font-mono text-3xl font-bold tabular-nums tracking-tight">
            {formatPrice(spot)}
          </span>
          {delta != null && (
            <span
              className={cn(
                "font-mono text-sm font-semibold tabular-nums",
                delta > 0 ? "text-emerald-400" : "text-red-400",
              )}
            >
              {delta > 0 ? "▲" : "▼"} {Math.abs(delta).toFixed(2)}
            </span>
          )}
        </div>

        {/* top of book + connection */}
        <div className="ml-auto flex flex-wrap items-center gap-x-5 gap-y-2 font-mono text-xs tabular-nums text-muted-foreground">
          <span>
            BID{" "}
            <b className="font-semibold text-emerald-400">{formatPrice(bestBid)}</b>
          </span>
          <span>
            ASK{" "}
            <b className="font-semibold text-red-400">{formatPrice(bestAsk)}</b>
          </span>
          <ConnectionBadge state={connection} />
        </div>
      </div>
    </header>
  )
}
