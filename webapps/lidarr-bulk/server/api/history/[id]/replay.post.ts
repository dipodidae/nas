// Re-run items from a historic job. By default, only the items that didn't
// succeed (not-found, error, skipped). Pass `all=true` to retry everything.

import { createError, defineEventHandler, getRouterParam, readBody } from 'h3'
import { z } from 'zod'
import { findHistoryEntry } from '../../../utils/history'
import { createJob } from '../../../utils/jobs'

const RETRY_STATUSES = new Set(['not-found', 'error', 'skipped'])

const schema = z.object({
  all: z.boolean().optional().default(false),
  dryRun: z.boolean().optional().default(false),
})

export default defineEventHandler(async (event) => {
  const id = getRouterParam(event, 'id') ?? ''
  const body = await readBody(event).catch(() => ({}))
  const parsed = schema.safeParse(body)
  if (!parsed.success)
    throw createError({ statusCode: 400, statusMessage: parsed.error.message })
  const entry = await findHistoryEntry(id)
  if (!entry)
    throw createError({ statusCode: 404, statusMessage: 'history entry not found' })
  const items = parsed.data.all
    ? entry.items.map(i => i.parsed)
    : entry.items.filter(i => RETRY_STATUSES.has(i.status)).map(i => i.parsed)
  if (items.length === 0)
    throw createError({ statusCode: 400, statusMessage: 'nothing to retry' })
  return createJob(entry.kind, items, 'all', {
    dryRun: parsed.data.dryRun,
  })
})
