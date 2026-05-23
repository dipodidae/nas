import { createError, defineEventHandler, getRouterParam, readBody } from 'h3'
import { z } from 'zod'
import { choose } from '../../../utils/jobs'

const candidateSchema = z.object({
  kind: z.enum(['artist', 'album']),
  // We don't re-validate the inner Lidarr fields — they came from us and are
  // forwarded as-is into the next API call.
  value: z.record(z.string(), z.unknown()),
}).nullable()

const schema = z.object({
  itemId: z.string(),
  candidate: candidateSchema,
})

export default defineEventHandler(async (event) => {
  const id = getRouterParam(event, 'id') ?? ''
  const body = await readBody(event)
  const parsed = schema.safeParse(body)
  if (!parsed.success)
    throw createError({ statusCode: 400, statusMessage: parsed.error.message })
  const ok = choose(id, parsed.data.itemId, parsed.data.candidate as never)
  if (!ok)
    throw createError({ statusCode: 404, statusMessage: 'no pending decision' })
  return { ok: true }
})
