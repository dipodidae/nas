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
  // One `snapshot` up front, then one `item` per change. Pushing the whole job on
  // every status change was O(items²) over a run and reached tens of megabytes on
  // a 900-album playlist; the client merges patches into its own copy instead.
  const unsub = subscribe(id, (ev) => {
    if (ev.type === 'snapshot') {
      void stream.push({ event: 'snapshot', data: JSON.stringify(ev.snapshot) })
      return
    }
    if (ev.type === 'item') {
      void stream.push({ event: 'item', data: JSON.stringify(ev.item) })
      return
    }
    void stream.push({ event: 'done', data: '1' })
    void stream.close()
  })
  stream.onClosed(() => unsub?.())
  return stream.send()
})
