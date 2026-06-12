import type { SpotifyPlaylist } from '~~/shared/types'
import { createError, defineEventHandler } from 'h3'
import { loadEnv } from '../../utils/env'
import { fetchPlaylists, getValidAccessToken, spotifyEnabled, trimPlaylist } from '../../utils/spotify'

export default defineEventHandler(async (): Promise<{ playlists: SpotifyPlaylist[] }> => {
  const env = loadEnv()
  if (!spotifyEnabled(env))
    throw createError({ statusCode: 503, statusMessage: 'Spotify is not configured.' })
  let token: string | null
  try {
    token = await getValidAccessToken(env)
  }
  catch {
    // Refresh failed (revoked grant) — surface as "not connected".
    throw createError({ statusCode: 401, statusMessage: 'Spotify not connected.' })
  }
  if (!token)
    throw createError({ statusCode: 401, statusMessage: 'Spotify not connected.' })
  try {
    const raw = await fetchPlaylists(token)
    return { playlists: raw.map(trimPlaylist) }
  }
  catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err)
    throw createError({ statusCode: 502, statusMessage: `Spotify playlist fetch failed: ${msg}` })
  }
})
