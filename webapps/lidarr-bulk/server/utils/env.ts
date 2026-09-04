import { z } from 'zod'

const schema = z.object({
  LIDARR_URL: z.string().url(),
  LIDARR_API_KEY: z.string().min(1),
  // Lidarr's metadata backend, used to resolve Various Artists comps by MBID
  // (Lidarr's own /album/lookup hides the special VA entity from text search).
  LIDARR_METADATA_URL: z.string().url().default('https://api.lidarr.audio/api/v0.4'),
  // No APP_USERNAME / APP_PASSWORD / APP_BEARER_TOKEN. This app has no login
  // of its own: the public route is gated by the one tinyauth door at SWAG
  // (ADR-0034), and on nas-network nothing but SWAG can reach it.
  CONFIG_DIR: z.string().default('/config'),
  // Per-IP sliding window on /api/*. Sized for a single-user UI that makes one
  // request per candidate pick: 30 was tripping mid-session on a playlist with a
  // couple of dozen ambiguous rows, and once tripped it 429s *everything* —
  // including loading the Spotify playlists.
  RATE_LIMIT_PER_MINUTE: z.coerce.number().int().positive().default(300),
  BODY_LIMIT_BYTES: z.coerce.number().int().positive().default(262144),
  // Optional — enables the AI "Discover" tab. When unset, the endpoint 503s and
  // the tab tells the user it's disabled.
  OPENAI_API_KEY: z.string().optional().default(''),
  OPENAI_MODEL: z.string().min(1).default('gpt-4o'),
  // Optional — enables the Spotify "Spotify" tab. All three required to enable;
  // when any is unset the tab is hidden and the endpoints report disabled.
  SPOTIFY_API_CLIENT_ID: z.string().optional().default(''),
  SPOTIFY_API_CLIENT_SECRET: z.string().optional().default(''),
  // Must exactly match a Redirect URI registered in the Spotify dashboard, e.g.
  // https://lidarr-bulk.example.com/api/spotify/callback
  SPOTIFY_REDIRECT_URI: z.string().optional().default(''),
  // Optional — enables the Spotify "Recreate in Jellyfin" action. All three
  // required to enable; when any is unset the button is hidden and the endpoint
  // reports disabled. Reuses the nas stack's existing Jellyfin API key + user id.
  JELLYFIN_URL: z.string().optional().default(''),
  JELLYFIN_API_KEY: z.string().optional().default(''),
  JELLYFIN_USER_ID: z.string().optional().default(''),
})

export type Env = z.infer<typeof schema>

let cached: Env | undefined

export function loadEnv(): Env {
  if (cached)
    return cached
  const parsed = schema.safeParse(process.env)
  if (!parsed.success) {
    const issues = parsed.error.issues.map(i => `${i.path.join('.')}: ${i.message}`).join('; ')
    throw new Error(`Invalid env: ${issues}`)
  }
  cached = parsed.data
  return cached
}
