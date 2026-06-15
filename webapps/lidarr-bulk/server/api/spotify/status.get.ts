import type { SpotifyStatus } from '~~/shared/types'
import { defineEventHandler } from 'h3'
import { loadEnv } from '../../utils/env'
import { jellyfinEnabled } from '../../utils/jellyfin'
import { readToken, spotifyEnabled } from '../../utils/spotify'

// Lets the UI decide whether to show the Spotify tab and whether to prompt for
// connect — without leaking secrets. `connected` reflects a stored token;
// validity is enforced lazily on use (refresh / re-connect). `jellyfin` gates
// the per-playlist "Recreate in Jellyfin" action.
export default defineEventHandler(async (): Promise<SpotifyStatus> => {
  const env = loadEnv()
  const enabled = spotifyEnabled(env)
  const connected = enabled ? (await readToken()) !== null : false
  return { enabled, connected, jellyfin: jellyfinEnabled(env) }
})
