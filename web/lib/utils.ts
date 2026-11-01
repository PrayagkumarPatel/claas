import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

/** shadcn/ui class merge helper. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/** "$2.18" or "—" when there is no data yet. */
export function formatPrice(x: number | null | undefined, prefix = "$"): string {
  return x == null || !isFinite(x) ? "—" : `${prefix}${x.toFixed(2)}`
}

/** 1,234-style grouping. */
export function formatQty(x: number | null | undefined): string {
  return x == null ? "—" : x.toLocaleString("en-US")
}
