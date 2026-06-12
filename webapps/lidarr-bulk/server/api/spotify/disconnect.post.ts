import { defineEventHandler } from 'h3'
import { deleteToken } from '../../utils/spotify'

export default defineEventHandler(async () => {
  await deleteToken()
  return { ok: true }
})
