import { randomUUID } from 'node:crypto'
import { createError, defineEventHandler, sendRedirect, setCookie } from 'h3'
import { loadEnv } from '../../utils/env'
import { buildAuthorizeUrl, spotifyEnabled } from '../../utils/spotify'

export default defineEventHandler((event) => {
  const env = loadEnv()
  if (!spotifyEnabled(env))
    throw createError({ statusCode: 503, statusMessage: 'Spotify is not configured.' })
  const state = randomUUID()
  // Short-lived CSRF guard echoed back by Spotify and checked in the callback.
  setCookie(event, 'spotify_oauth_state', state, {
    httpOnly: true,
    sameSite: 'lax',
    path: '/',
    maxAge: 600,
  })
  return sendRedirect(event, buildAuthorizeUrl(env, state))
})
