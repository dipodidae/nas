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
  ],
})
