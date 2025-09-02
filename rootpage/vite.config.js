import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'

export default defineConfig({
  // Vite plugins
  plugins: [
    tailwindcss(),
  ],

  // Set the base path for assets
  base: '/',

  // Build configuration
  build: {
    // Output directory (default is 'dist')
    outDir: 'dist',

    // Clean the output directory before building
    emptyOutDir: true,

    // Generate source maps for debugging
    sourcemap: false,

    // Minify the output
    minify: true,

    // Asset handling
    assetsDir: 'assets',

    // Rollup options for more control
    rollupOptions: {
      input: {
        main: './index.html',
      },
    },
  },

  // Development server configuration
  server: {
    host: '0.0.0.0',
    port: 3000,
    open: false,
  },
})
