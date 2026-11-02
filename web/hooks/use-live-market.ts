"use client"

import { useCallback, useEffect, useMemo, useRef, useState } from "react"

import { startDemoFeed } from "@/lib/demo-data"
import { getSupabase, isSupabaseConfigured } from "@/lib/supabase-client"
import type { Order, OrderStatus, Side, Trade } from "@/lib/types"

/**
 * Which order statuses are visible in the book. Per the spec only 'open'
 * orders are shown — add "partial" if you also want partially-filled orders
 * that still have resting quantity on the book.
 */
const BOOK_STATUSES: OrderStatus[] = ["open", "partial"]

const ORDERS_PER_SIDE = 30
const TRADES_LIMIT = 25
const REFRESH_DEBOUNCE_MS = 300
const SAFETY_POLL_MS = 15_000 // keeps data fresh even if realtime isn't enabled

export type ConnectionState = "connecting" | "live" | "offline" | "demo"

// Raw supabase rows: `price` arrives as a string (Postgres numeric)
interface OrderRow {
  id: string
  maker: string
  side: Side
  price: string | number
  quantity: number
  status: OrderStatus
}
interface TradeRow {
  id: string
  price: string | number
  quantity: number
  executed_at: string
}

const toOrder = (r: OrderRow): Order => ({ ...r, price: Number(r.price) })
const toTrade = (r: TradeRow): Trade => ({
  ...r,
  price: Number(r.price),
  executed_at: String(r.executed_at),
})

export interface LiveMarket {
  bids: Order[] // price desc (best first)
  asks: Order[] // price asc (best first)
  trades: Trade[] // executed_at desc (newest first)
  spot: number | null // price of the most recent trade
  prevSpot: number | null
  connection: ConnectionState
  loaded: boolean
}

/**
 * Live market data: initial fetch + Supabase Realtime subscriptions on the
 * `orders` and `trades` tables. Any INSERT / UPDATE / DELETE on either table
 * triggers a debounced refetch (simple + always-consistent, vs. patching
 * local state from individual payloads).
 *
 * When Supabase is not configured, a demo feed drives the same state shape.
 */
export function useLiveMarket(): LiveMarket {
  const [orders, setOrders] = useState<Order[]>([])
  const [trades, setTrades] = useState<Trade[]>([])
  const [loaded, setLoaded] = useState(false)
  const [connection, setConnection] = useState<ConnectionState>(
    isSupabaseConfigured ? "connecting" : "demo",
  )
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const fetchAll = useCallback(async () => {
    const supabase = getSupabase()
    if (!supabase) return

    const [bidsRes, asksRes, tradesRes] = await Promise.all([
      supabase
        .from("orders")
        .select("id,maker,side,price,quantity,status")
        .eq("side", "bid")
        .in("status", BOOK_STATUSES)
        .order("price", { ascending: false })
        .limit(ORDERS_PER_SIDE),
      supabase
        .from("orders")
        .select("id,maker,side,price,quantity,status")
        .eq("side", "ask")
        .in("status", BOOK_STATUSES)
        .order("price", { ascending: true })
        .limit(ORDERS_PER_SIDE),
      supabase
        .from("trades")
        .select("id,price,quantity,executed_at")
        .order("executed_at", { ascending: false })
        .limit(TRADES_LIMIT),
    ])

    if (bidsRes.error || asksRes.error || tradesRes.error) {
      setConnection("offline")
      return
    }

    setOrders([
      ...(bidsRes.data ?? []).map(toOrder),
      ...(asksRes.data ?? []).map(toOrder),
    ])
    setTrades((tradesRes.data ?? []).map(toTrade))
    setLoaded(true)
    setConnection((prev) => (prev === "connecting" ? "live" : prev))
  }, [])

  useEffect(() => {
    // ---- Demo mode (no credentials) --------------------------------------
    if (!isSupabaseConfigured) {
      const stop = startDemoFeed(({ orders: o, trades: t }) => {
        setOrders(o)
        setTrades(t)
        setLoaded(true)
      })
      return stop
    }

    // ---- Supabase mode ----------------------------------------------------
    const supabase = getSupabase()
    if (!supabase) return
    fetchAll()

    const scheduleRefresh = () => {
      if (refreshTimer.current) clearTimeout(refreshTimer.current)
      refreshTimer.current = setTimeout(() => {
        refreshTimer.current = null
        fetchAll()
      }, REFRESH_DEBOUNCE_MS)
    }

    const channel = supabase
      .channel("claas-market")
      .on(
        "postgres_changes",
        { event: "*", schema: "public", table: "orders" },
        scheduleRefresh,
      )
      .on(
        "postgres_changes",
        { event: "*", schema: "public", table: "trades" },
        scheduleRefresh,
      )
      .subscribe((status) => {
        if (status === "SUBSCRIBED") setConnection("live")
        else if (status === "CHANNEL_ERROR" || status === "TIMED_OUT")
          setConnection("offline")
      })

    const poll = setInterval(fetchAll, SAFETY_POLL_MS)

    return () => {
      if (refreshTimer.current) clearTimeout(refreshTimer.current)
      clearInterval(poll)
      supabase.removeChannel(channel)
    }
  }, [fetchAll])

  const bids = useMemo(
    () => orders.filter((o) => o.side === "bid").sort((a, b) => b.price - a.price),
    [orders],
  )
  const asks = useMemo(
    () => orders.filter((o) => o.side === "ask").sort((a, b) => a.price - b.price),
    [orders],
  )

  return {
    bids,
    asks,
    trades,
    spot: trades[0]?.price ?? null,
    prevSpot: trades[1]?.price ?? null,
    connection,
    loaded,
  }
}
