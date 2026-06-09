import { createError, defineEventHandler, readBody } from 'h3'
import { z } from 'zod'
import { loadEnv } from '../../utils/env'
import { AI_MAX_COUNT, AI_MIN_COUNT, AI_PROMPT_MAX_CHARS, clampCount, suggestAlbums } from '../../utils/openai'

const schema = z.object({
  prompt: z.string().trim().min(3).max(AI_PROMPT_MAX_CHARS),
  count: z.number().int().min(AI_MIN_COUNT).max(AI_MAX_COUNT).optional(),
})

export default defineEventHandler(async (event) => {
  const env = loadEnv()
  if (!env.OPENAI_API_KEY) {
    throw createError({
      statusCode: 503,
      statusMessage: 'AI discovery is disabled — set OPENAI_API_KEY to enable it.',
    })
  }

  const body = await readBody(event)
  const parsed = schema.safeParse(body)
  if (!parsed.success)
    throw createError({ statusCode: 400, statusMessage: parsed.error.message })

  try {
    const items = await suggestAlbums({
      apiKey: env.OPENAI_API_KEY,
      model: env.OPENAI_MODEL,
      prompt: parsed.data.prompt,
      count: clampCount(parsed.data.count),
    })
    return { items }
  }
  catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err)
    // Surface a clean upstream failure rather than a 500 with a stack trace.
    throw createError({ statusCode: 502, statusMessage: `AI suggestion failed: ${msg}` })
  }
})
