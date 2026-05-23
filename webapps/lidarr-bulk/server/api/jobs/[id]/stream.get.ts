import { createEventStream, defineEventHandler, getRouterParam, setHeader } from 'h3'
import { getJob, subscribe } from '../../../utils/jobs'

export default defineEventHandler(async (event) => {
  const id = getRouterParam(event, 'id') ?? ''
  if (!getJob(id))
    return { error: 'not found' }
  // Tell nginx (SWAG) to bypass buffering for this response only, so SSE
  // updates flush immediately without a special location block.
  setHeader(event, 'X-Accel-Buffering', 'no')
  const stream = createEventStream(event)
  const unsub = subscribe(id, (snap) => {
    void stream.push({ event: 'snapshot', data: JSON.stringify(snap) })
    if (snap.done)
      void stream.close()
  })
  stream.onClosed(() => unsub?.())
  return stream.send()
})
