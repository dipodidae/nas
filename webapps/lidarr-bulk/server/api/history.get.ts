import { defineEventHandler, getQuery } from 'h3'
import { listHistory } from '../utils/history'

export default defineEventHandler(async (event) => {
  const q = getQuery(event)
  const limit = Math.min(200, Math.max(1, Number.parseInt(String(q.limit ?? '50'), 10) || 50))
  return { entries: await listHistory(limit) }
})
