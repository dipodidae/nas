import type { Candidate, JobItem, JobSnapshot, Kind, ParsedItem } from '~~/shared/types'

export function useJob() {
  const job = ref<JobSnapshot | null>(null)
  let es: EventSource | null = null

  function close(): void {
    es?.close()
    es = null
  }

  interface StartOptions {
    dryRun?: boolean
    metadataProfileId?: number
    qualityProfileId?: number
  }

  async function start(
    kind: Kind,
    items: ParsedItem[],
    monitorMode: 'all' | 'future',
    opts: StartOptions = {},
  ): Promise<void> {
    close()
    const snap = await $fetch<JobSnapshot>('/api/jobs', {
      method: 'POST',
      body: { kind, items, monitorMode, ...opts },
    })
    job.value = snap
    es = new EventSource(`/api/jobs/${snap.id}/stream`)
    // The server sends one full snapshot, then a patch per changed item. Merging
    // patches locally keeps a 900-album job's update cost flat instead of
    // re-transferring (and re-rendering) every row on every status change.
    const index = new Map<string, number>()
    function reindex(s: JobSnapshot): void {
      index.clear()
      s.items.forEach((it, i) => index.set(it.id, i))
    }
    es.addEventListener('snapshot', (e) => {
      const s = JSON.parse((e as MessageEvent).data) as JobSnapshot
      reindex(s)
      job.value = s
    })
    es.addEventListener('item', (e) => {
      const patched = JSON.parse((e as MessageEvent).data) as JobItem
      const current = job.value
      if (!current)
        return
      const at = index.get(patched.id)
      if (at === undefined)
        return
      current.items[at] = patched
    })
    es.addEventListener('done', () => {
      if (job.value)
        job.value.done = true
      close()
    })
    es.addEventListener('error', () => {
      // Connection may close cleanly on job completion; ignore.
    })
  }

  // Picking is one request per click, so a session working through a couple of
  // dozen ambiguous rows is the most likely thing to meet the per-IP limit. A
  // rejected pick would otherwise be lost — the click did nothing and the row
  // stays stuck — so honour Retry-After and try again rather than surfacing it.
  async function choose(itemId: string, candidate: Candidate | null): Promise<void> {
    if (!job.value)
      return
    const url = `/api/jobs/${job.value.id}/choose`
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        await $fetch(url, { method: 'POST', body: { itemId, candidate } })
        return
      }
      catch (err: unknown) {
        const e = err as { status?: number, statusCode?: number, response?: { headers?: Headers } }
        const status = e.status ?? e.statusCode
        if (status !== 429 || attempt === 2)
          throw err
        const retryAfter = Number.parseInt(e.response?.headers?.get('retry-after') ?? '', 10)
        const waitMs = Math.min(Number.isFinite(retryAfter) ? retryAfter * 1000 : 1500, 10_000)
        await new Promise(r => setTimeout(r, waitMs))
      }
    }
  }

  onScopeDispose(close)
  return { job, start, choose }
}
