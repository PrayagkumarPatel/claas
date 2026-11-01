import type { Order, Side, Trade } from "./types"

/**
 * Demo market feed — used ONLY when Supabase env vars are not configured.
 * It mutates an in-memory book and prints synthetic trades so the terminal
 * animates for development and demos. The UI labels this mode prominently.
 */

const MAKERS = [
  "Helios Cloud", "Orbital DC Partners", "Midwest GPU Barn", "Cascade HPC",
  "Alpine Research", "MammothML", "MirageVision", "Nebula Analytics",
  "TinyTune AI", "PromptDynasty", "Skunkworks Labs", "Babylon Bio",
]

const uid = () => Math.random().toString(36).slice(2, 10)
const round2 = (x: number) => Math.round(x * 100) / 100
// cheap approximately-normal noise in roughly [-1.8, 1.8]
const gauss = () => (Math.random() + Math.random() + Math.random() - 1.5) * 1.2
const rand = (min: number, max: number) =>
  Math.floor(Math.random() * (max - min + 1)) + min
const pick = <T>(arr: T[]): T => arr[Math.floor(Math.random() * arr.length)]

export interface DemoSnapshot {
  orders: Order[]
  trades: Trade[]
}

export function startDemoFeed(onTick: (snap: DemoSnapshot) => void): () => void {
  let price = 2.2
  let orders: Order[] = []
  let trades: Trade[] = []

  const makeOrder = (side: Side, p: number): Order => ({
    id: uid(),
    maker: pick(MAKERS),
    side,
    price: round2(p),
    quantity: rand(5, 120),
    status: "open",
  })

  // seed a plausible initial book around the starting price
  for (let i = 0; i < 6; i++) orders.push(makeOrder("bid", price - 0.02 * (i + 1)))
  for (let i = 0; i < 6; i++) orders.push(makeOrder("ask", price + 0.02 * (i + 1)))

  const capSide = (side: Side, max = 12) => {
    const indexes = orders.map((o, i) => (o.side === side ? i : -1)).filter((i) => i >= 0)
    while (indexes.length > max) {
      orders.splice(indexes[0], 1)
      indexes.shift()
    }
  }

  const tick = () => {
    // wander the fair value within a sane band
    price = Math.min(3.1, Math.max(1.5, price + gauss() * 0.02))

    // mutate the book: add near the market, or retire a random order
    const r = Math.random()
    if (r < 0.45) {
      const side: Side = Math.random() < 0.5 ? "bid" : "ask"
      const near = side === "bid" ? price - Math.random() * 0.08 : price + Math.random() * 0.08
      orders.push(makeOrder(side, near))
    } else if (r < 0.6 && orders.length > 8) {
      orders.splice(Math.floor(Math.random() * orders.length), 1)
    }

    // occasionally print a trade near the fair value
    if (Math.random() < 0.6) {
      trades = [
        {
          id: uid(),
          price: round2(price + gauss() * 0.01),
          quantity: rand(2, 80),
          executed_at: new Date().toISOString(),
        },
        ...trades,
      ].slice(0, 25)
    }

    capSide("bid")
    capSide("ask")
    onTick({ orders: [...orders], trades })
  }

  tick() // paint the first frame immediately
  const timer = setInterval(tick, 1500)
  return () => clearInterval(timer)
}
