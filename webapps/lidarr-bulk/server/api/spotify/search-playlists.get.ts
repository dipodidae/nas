import type { SpotifyPlaylist } from '~~/shared/types'
import { createError, defineEventHandler, getQuery } from 'h3'
import { loadEnv } from '../../utils/env'
import { getValidAccessToken, searchPlaylists, spotifyEnabled } from '../../utils/spotify'

// Keyword search across ALL public Spotify playlists, reusing the connected
// account's token. Mirrors playlists.get.ts' guard ladder. A blank query is a
// no-op so the client can clear results without an upstream round-trip.
export default defineEventHandler(async (event): Promise<{ playlists: SpotifyPlaylist[] }> => {
  const env = loadEnv()
  if (!spotifyEnabled(env))
    throw createError({ statusCode: 503, statusMessage: 'Spotify is not configured.' })

  const q = String(getQuery(event).q ?? '').trim()
  if (!q)
    return { playlists: [] }

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
    return { playlists: await searchPlaylists(token, q) }
  }
  catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err)
    throw createError({ statusCode: 502, statusMessage: `Spotify search failed: ${msg}` })
  }
})
