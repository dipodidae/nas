import { createError, defineEventHandler, getQuery } from 'h3'
import { lookupAlbum } from '../../utils/lidarr'

export default defineEventHandler((event) => {
  const term = String(getQuery(event).term ?? '').trim()
  if (!term)
    throw createError({ statusCode: 400, statusMessage: 'term required' })
  return lookupAlbum(term)
})
