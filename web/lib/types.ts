/**
 * Row types matching the Supabase schema.
 *
 * `price` is a Postgres `numeric`, which supabase-js returns as a STRING —
 * the mapping layer in hooks/use-live-market.ts coerces it with Number().
 */

export type Side = "bid" | "ask"

export type OrderStatus = "open" | "partial" | "filled" | "canceled"

export interface Order {
  id: string
  maker: string
  side: Side
  price: number
  quantity: number
  status: OrderStatus
}

export interface Trade {
  id: string
  price: number
  quantity: number
  executed_at: string // ISO timestamp
}
