// https://nuxt.com/docs/4.x/api/nuxt-config
export default defineNuxtConfig({
  future: {
    compatibilityVersion: 4,
  },
  compatibilityDate: '2025-01-01',
  modules: ['@nuxt/ui', 'nuxt-auth-utils'],
  devtools: { enabled: true },
  colorMode: {
    preference: 'dark',
    fallback: 'dark',
  },
  typescript: {
    strict: true,
    typeCheck: false,
  },
  app: {
    head: {
      title: 'lidarr-bulk',
      meta: [
        { name: 'viewport', content: 'width=device-width,initial-scale=1' },
      ],
    },
  },
  css: ['~/assets/css/main.css'],
  nitro: {
    routeRules: {
      '/api/**': { cors: false },
    },
  },
  runtimeConfig: {
    lidarrUrl: '',
    lidarrApiKey: '',
    appBearerToken: '',
    appUsername: '',
    appPassword: '',
    configDir: '/config',
    rateLimitPerMinute: 30,
    bodyLimitBytes: 262144,
    session: {
      // nuxt-auth-utils picks NUXT_SESSION_PASSWORD from env; we generate one
      // at boot if missing (see server/plugins/init.ts).
      password: '',
    },
    public: {
      authRequired: false,
    },
  },
})
