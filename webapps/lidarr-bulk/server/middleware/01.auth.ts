// Gate /api/* when EITHER bearer-token OR username/password auth is configured.
// Always-open endpoints: /api/login (so users can authenticate), /api/me (so
// the UI can decide whether to redirect to /login).

import { createError, defineEventHandler, getHeader, getRequestURL } from 'h3'
import { loadEnv } from '../utils/env'

const OPEN_PATHS = new Set(['/api/login', '/api/me'])

export default defineEventHandler(async (event) => {
  const url = getRequestURL(event)
  if (!url.pathname.startsWith('/api/'))
    return
  if (OPEN_PATHS.has(url.pathname))
    return
  const env = loadEnv()
  const sessionAuth = Boolean(env.APP_USERNAME && env.APP_PASSWORD)
  const bearerAuth = Boolean(env.APP_BEARER_TOKEN)
  if (!sessionAuth && !bearerAuth)
    return

  // Try bearer first (programmatic clients).
  if (bearerAuth) {
    const header = getHeader(event, 'authorization') ?? ''
    const [scheme, token] = header.split(' ', 2)
    if (scheme === 'Bearer' && token === env.APP_BEARER_TOKEN)
      return
  }
  // Then session cookie.
  if (sessionAuth) {
    const session = await getUserSession(event)
    if (session?.user)
      return
  }
  throw createError({ statusCode: 401, statusMessage: 'Unauthorized' })
})
