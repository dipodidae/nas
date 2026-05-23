import { createError, defineEventHandler, readBody } from 'h3'
import { z } from 'zod'
import { loadSettings, saveSettings } from '../utils/settings'

const schema = z.object({
  rootFolderPath: z.string().min(1),
  qualityProfileId: z.number().int().positive(),
  metadataProfileId: z.number().int().positive(),
  monitorMode: z.enum(['all', 'future']),
})

export default defineEventHandler(async (event) => {
  const body = await readBody(event)
  const parsed = schema.safeParse(body)
  if (!parsed.success)
    throw createError({ statusCode: 400, statusMessage: parsed.error.message })
  await saveSettings(parsed.data)
  return loadSettings()
})
