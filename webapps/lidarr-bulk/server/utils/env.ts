import { z } from 'zod'

const schema = z.object({
  LIDARR_URL: z.string().url(),
  LIDARR_API_KEY: z.string().min(1),
  APP_BEARER_TOKEN: z.string().optional().default(''),
  // Session login. When both are set, the UI requires login; /api/* requires
  // either a valid session cookie or APP_BEARER_TOKEN (if set).
  APP_USERNAME: z.string().optional().default(''),
  APP_PASSWORD: z.string().optional().default(''),
  CONFIG_DIR: z.string().default('/config'),
  RATE_LIMIT_PER_MINUTE: z.coerce.number().int().positive().default(30),
  BODY_LIMIT_BYTES: z.coerce.number().int().positive().default(262144),
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
