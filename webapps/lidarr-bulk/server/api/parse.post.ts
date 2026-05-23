import { createError, defineEventHandler, readBody } from 'h3'
import { z } from 'zod'
import { loadEnv } from '../utils/env'
import { parseAlbums, parseArtists } from '../utils/parsers'

const schema = z.object({
  kind: z.enum(['artist', 'album']),
  blob: z.string(),
})

export default defineEventHandler(async (event) => {
  const body = await readBody(event)
  const parsed = schema.safeParse(body)
  if (!parsed.success)
    throw createError({ statusCode: 400, statusMessage: parsed.error.message })
  const env = loadEnv()
  if (Buffer.byteLength(parsed.data.blob, 'utf8') > env.BODY_LIMIT_BYTES) {
    throw createError({
      statusCode: 413,
      statusMessage: `blob exceeds ${env.BODY_LIMIT_BYTES} bytes`,
    })
  }
  const items = parsed.data.kind === 'artist'
    ? parseArtists(parsed.data.blob)
    : parseAlbums(parsed.data.blob)
  return { items }
})
