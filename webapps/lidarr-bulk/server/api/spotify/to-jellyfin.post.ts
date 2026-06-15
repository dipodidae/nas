import type { JellyfinPushResult } from '~~/shared/types'
import { createError, defineEventHandler, readBody } from 'h3'
import { z } from 'zod'
import { loadEnv } from '../../utils/env'
import { jellyfinEnabled, recreatePlaylistInJellyfin } from '../../utils/jellyfin'
import { fetchPlaylistTrackDetails, getValidAccessToken, spotifyEnabled } from '../../utils/spotify'

const schema = z.object({
  playlistId: z.string().min(1),
  playlistName: z.string().min(1),
})

export default defineEventHandler(async (event): Promise<JellyfinPushResult> => {
  const env = loadEnv()
  if (!spotifyEnabled(env))
    throw createError({ statusCode: 503, statusMessage: 'Spotify is not configured.' })
  if (!jellyfinEnabled(env))
    throw createError({ statusCode: 503, statusMessage: 'Jellyfin is not configured.' })

  const parsed = schema.safeParse(await readBody(event))
  if (!parsed.success)
    throw createError({ statusCode: 400, statusMessage: parsed.error.message })

  let token: string | null
  try {
    token = await getValidAccessToken(env)
  }
  catch {
    throw createError({ statusCode: 401, statusMessage: 'Spotify not connected.' })
  }
  if (!token)
    throw createError({ statusCode: 401, statusMessage: 'Spotify not connected.' })

  try {
    const tracks = await fetchPlaylistTrackDetails(token, parsed.data.playlistId)
    return await recreatePlaylistInJellyfin(env, parsed.data.playlistName, tracks)
  }
  catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err)
    throw createError({ statusCode: 502, statusMessage: `Recreate in Jellyfin failed: ${msg}` })
  }
})
