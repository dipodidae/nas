// https://nuxt.com/docs/4.x/api/nuxt-config
export default defineNuxtConfig({
  future: {
    compatibilityVersion: 4,
  },
  compatibilityDate: '2025-01-01',
  modules: ['@nuxt/eslint', '@nuxt/ui'],
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
  // No auth keys here. This app has no login of its own: the public route is
  // gated by the single tinyauth door at SWAG (ADR-0034), and nothing reaches
  // it on nas-network except SWAG itself.
  runtimeConfig: {
    lidarrUrl: '',
    lidarrApiKey: '',
    configDir: '/config',
    rateLimitPerMinute: 30,
    bodyLimitBytes: 262144,
  },
})
