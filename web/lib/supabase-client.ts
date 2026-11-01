"use client"

import { createClient, type SupabaseClient } from "@supabase/supabase-js"

/**
 * Browser-side Supabase client (singleton).
 *
 * Configure via .env.local:
 *   NEXT_PUBLIC_SUPABASE_URL=...
 *   NEXT_PUBLIC_SUPABASE_ANON_KEY=...
 *
 * When those are missing, `isSupabaseConfigured` is false and the app falls
 * back to a clearly-labeled demo feed (lib/demo-data.ts) so the UI is always
 * renderable — useful for development, screenshots, and demos.
 *
 * The anon key is the *publishable* key: safe for the browser because access
 * is governed by Row Level Security (see web/README.md for the read policies).
 */

const url = process.env.NEXT_PUBLIC_SUPABASE_URL
const anonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY

export const isSupabaseConfigured = Boolean(url && anonKey)

let client: SupabaseClient | null = null

export function getSupabase(): SupabaseClient | null {
  if (!isSupabaseConfigured) return null
  if (!client) {
    client = createClient(url as string, anonKey as string, {
      realtime: { params: { eventsPerSecond: 10 } },
      auth: { persistSession: false },
    })
  }
  return client
}
