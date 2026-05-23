// Per-IP sliding-window rate limit on /api/*. Single-instance only — backed by
// an in-memory ring per IP. SSE streams are excluded so progress doesn't burn
// budget.

import { createError, defineEventHandler, getHeader, getRequestIP, getRequestURL } from 'h3'
import { loadEnv } from '../utils/env'

const WINDOW_MS = 60_000
const buckets = new Map<string, number[]>()

function trim(arr: number[], now: number): number[] {
  const cutoff = now - WINDOW_MS
  let i = 0
  while (i < arr.length && arr[i]! < cutoff) i++
  return i > 0 ? arr.slice(i) : arr
}

export default defineEventHandler((event) => {
  const url = getRequestURL(event)
  if (!url.pathname.startsWith('/api/'))
    return
  if (url.pathname.endsWith('/stream'))
    return // SSE — no rate limit
  const env = loadEnv()
  const ip
    = getHeader(event, 'x-forwarded-for')?.split(',')[0]?.trim()
      ?? getRequestIP(event)
      ?? 'unknown'
  const now = Date.now()
  const prev = trim(buckets.get(ip) ?? [], now)
  if (prev.length >= env.RATE_LIMIT_PER_MINUTE) {
    throw createError({ statusCode: 429, statusMessage: 'Too Many Requests' })
  }
  prev.push(now)
  buckets.set(ip, prev)
})
