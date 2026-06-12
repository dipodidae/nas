import { defineEventHandler, getCookie, getQuery, sendRedirect, setCookie } from 'h3'
import { loadEnv } from '../../utils/env'
import { exchangeCode, spotifyEnabled } from '../../utils/spotify'

// Spotify redirects the browser here after consent. We verify the state cookie,
// exchange the code for tokens, then bounce back to the app with a status flag
// the UI can surface as a toast.
export default defineEventHandler(async (event) => {
  const env = loadEnv()
  const query = getQuery(event)
  const code = typeof query.code === 'string' ? query.code : ''
  const state = typeof query.state === 'string' ? query.state : ''
  const expected = getCookie(event, 'spotify_oauth_state')
  // Clear the one-shot state cookie regardless of outcome.
  setCookie(event, 'spotify_oauth_state', '', { path: '/', maxAge: 0 })

  if (!spotifyEnabled(env) || query.error || !code || !state || state !== expected)
    return sendRedirect(event, '/?spotify=error')

  try {
    await exchangeCode(env, code)
    return sendRedirect(event, '/?spotify=connected')
  }
  catch {
    return sendRedirect(event, '/?spotify=error')
  }
})
