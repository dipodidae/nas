import { defineEventHandler } from 'h3'
import { loadEnv } from '../../utils/env'

// Lets the UI decide whether to show the AI "Discover" tab without leaking the
// key — only a boolean and the (non-secret) model name.
export default defineEventHandler(() => {
  const env = loadEnv()
  return {
    enabled: Boolean(env.OPENAI_API_KEY),
    model: env.OPENAI_MODEL,
  }
})
