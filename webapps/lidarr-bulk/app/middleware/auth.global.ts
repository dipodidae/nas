// Redirects unauthenticated users to /login when auth is enabled.
// Uses $fetch (not useFetch) so each navigation re-checks the session and we
// don't get bitten by Nuxt's per-key cache after a successful login.

export default defineNuxtRouteMiddleware(async (to) => {
  if (to.path === '/login')
    return
  if (import.meta.server)
    return // session cookie isn't reliably forwarded on initial SSR; let the
            // client re-check after hydration.
  try {
    const me = await $fetch<{
      authRequired: boolean
      user: { name: string } | null
    }>('/api/me')
    if (me.authRequired && !me.user)
      return navigateTo(`/login?next=${encodeURIComponent(to.fullPath)}`)
  }
  catch {
    // /api/me should not fail; if it does, fall through and let the page error.
  }
})
