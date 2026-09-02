import antfu from '@antfu/eslint-config'

export default antfu({
  formatters: true,
  ignores: [
    // Self-contained Nuxt app — linted by its own @nuxt/eslint toolchain
    // (`pnpm --dir webapps/lidarr-bulk lint`). The root config has no
    // TypeScript support, so it cannot parse this app's .ts/.vue correctly.
    'webapps/lidarr-bulk/**',
    // Don't lint code fragments embedded in Markdown — docs intentionally
    // contain illustrative, non-runnable snippets (JSON fragments, templates).
    '**/*.md/**',
    // Historical plan and spec documents. They are a record of what was decided
    // and when, so reformatting them rewrites history for no gain — and their
    // fenced Makefile recipes legitimately contain hard tabs.
    'docs/superpowers/**',
  ],
}, {
  // Build-time shim run by hand and by redeploy.sh; its console output IS the
  // user interface. Nothing else in the repo may use console.log.
  files: ['webapps/ongehoord/pnpm11-config-shim.mjs'],
  rules: { 'no-console': 'off' },
})
