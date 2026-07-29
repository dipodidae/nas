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

  async function choose(itemId: string, candidate: Candidate | null): Promise<void> {
    if (!job.value)
      return
    await $fetch(`/api/jobs/${job.value.id}/choose`, {
      method: 'POST',
      body: { itemId, candidate },
    })
  }

  onScopeDispose(close)
  return { job, start, choose }
}
