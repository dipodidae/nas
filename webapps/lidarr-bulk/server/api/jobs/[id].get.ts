import { createError, defineEventHandler, getRouterParam } from 'h3'
import { getJob } from '../../utils/jobs'

export default defineEventHandler((event) => {
  const id = getRouterParam(event, 'id') ?? ''
  const job = getJob(id)
  if (!job)
    throw createError({ statusCode: 404 })
  return job
})
