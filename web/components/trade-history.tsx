"use client"

import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import type { Trade } from "@/lib/types"
import { cn, formatPrice, formatQty } from "@/lib/utils"

/**
 * Section 3: trade history feed — the most recently executed trades,
 * newest first. Price is colored by direction vs. the previous (older) trade.
 * Rows are keyed by trade id so the entry animation replays on new trades.
 */
export function TradeHistory({
  trades,
  loaded,
}: {
  trades: Trade[]
  loaded: boolean
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Trade History</CardTitle>
        <p className="mt-1 text-xs text-muted-foreground">
          Most recent executed trades
        </p>
      </CardHeader>
      <CardContent className="p-0 pb-2">
        {!loaded ? (
          <div className="space-y-2 p-6 pt-0" aria-hidden="true">
            {Array.from({ length: 8 }).map((_, i) => (
              <Skeleton key={i} className="h-7 w-full" />
            ))}
          </div>
        ) : trades.length === 0 ? (
          <p className="px-6 py-10 text-center text-xs text-muted-foreground">
            No trades executed yet
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Time</TableHead>
                <TableHead>Price</TableHead>
                <TableHead className="text-right">Qty</TableHead>
                <TableHead className="text-right">Notional</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {trades.map((t, i) => {
                const older = trades[i + 1]?.price
                const up = older == null || t.price >= older
                return (
                  <TableRow
                    key={t.id}
                    className="animate-in fade-in slide-in-from-top-1 duration-300"
                  >
                    <TableCell className="py-1.5 font-mono text-xs tabular-nums text-muted-foreground">
                      {new Date(t.executed_at).toLocaleTimeString("en-US", {
                        hour12: false,
                      })}
                    </TableCell>
                    <TableCell
                      className={cn(
                        "py-1.5 font-mono text-xs font-semibold tabular-nums",
                        up ? "text-emerald-400" : "text-red-400",
                      )}
                    >
                      {formatPrice(t.price)}
                    </TableCell>
                    <TableCell className="py-1.5 text-right font-mono text-xs tabular-nums">
                      {formatQty(t.quantity)}
                    </TableCell>
                    <TableCell className="py-1.5 text-right font-mono text-xs tabular-nums text-muted-foreground">
                      {formatPrice(t.price * t.quantity)}
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  )
}
