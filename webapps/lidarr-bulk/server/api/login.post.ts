import { timingSafeEqual } from 'node:crypto'
import { createError, defineEventHandler, readBody } from 'h3'
import { z } from 'zod'
import { loadEnv } from '../utils/env'

const schema = z.object({
  username: z.string().min(1),
  password: z.string().min(1),
})

function eq(a: string, b: string): boolean {
  const ab = Buffer.from(a)
  const bb = Buffer.from(b)
  // timingSafeEqual requires equal-length buffers.
  if (ab.length !== bb.length)
    return false
  return timingSafeEqual(ab, bb)
}

export default defineEventHandler(async (event) => {
  const env = loadEnv()
  if (!env.APP_USERNAME || !env.APP_PASSWORD)
    throw createError({ statusCode: 400, statusMessage: 'auth disabled' })
  const body = await readBody(event)
  const parsed = schema.safeParse(body)
  if (!parsed.success)
    throw createError({ statusCode: 400, statusMessage: 'username/password required' })
  const userOk = eq(parsed.data.username, env.APP_USERNAME)
  const passOk = eq(parsed.data.password, env.APP_PASSWORD)
  if (!(userOk && passOk))
    throw createError({ statusCode: 401, statusMessage: 'invalid credentials' })
  await setUserSession(event, { user: { name: env.APP_USERNAME } })
  return { ok: true }
})
