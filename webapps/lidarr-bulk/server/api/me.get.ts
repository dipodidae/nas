import { defineEventHandler } from 'h3'
import { loadEnv } from '../utils/env'

export default defineEventHandler(async (event) => {
  const env = loadEnv()
  const authRequired = Boolean(env.APP_USERNAME && env.APP_PASSWORD)
  if (!authRequired)
    return { authRequired: false, user: null }
  const session = await getUserSession(event)
  return { authRequired: true, user: session?.user ?? null }
})
