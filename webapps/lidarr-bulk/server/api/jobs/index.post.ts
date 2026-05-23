import { createError, defineEventHandler, readBody } from 'h3'
import { z } from 'zod'
import { createJob } from '../../utils/jobs'

const schema = z.object({
  kind: z.enum(['artist', 'album']),
  monitorMode: z.enum(['all', 'future']),
  dryRun: z.boolean().optional().default(false),
  metadataProfileId: z.number().int().positive().optional(),
  qualityProfileId: z.number().int().positive().optional(),
  items: z.array(z.object({
    raw: z.string(),
    kind: z.enum(['artist', 'album']),
    artist: z.string().optional(),
    title: z.string().optional(),
    needsReview: z.boolean().optional(),
  })),
})

export default defineEventHandler(async (event) => {
  const body = await readBody(event)
  const parsed = schema.safeParse(body)
  if (!parsed.success)
    throw createError({ statusCode: 400, statusMessage: parsed.error.message })
  return createJob(parsed.data.kind, parsed.data.items, parsed.data.monitorMode, {
    dryRun: parsed.data.dryRun,
    metadataProfileId: parsed.data.metadataProfileId,
    qualityProfileId: parsed.data.qualityProfileId,
  })
})
