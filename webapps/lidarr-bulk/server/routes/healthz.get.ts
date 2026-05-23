import { defineEventHandler, setResponseStatus } from 'h3'
import { healthCheck } from '../utils/lidarr'

export default defineEventHandler(async (event) => {
  const ok = await healthCheck()
  if (!ok) {
    setResponseStatus(event, 503)
    return { ok: false }
  }
  return { ok: true }
})
