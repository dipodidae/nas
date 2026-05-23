import type { Candidate, JobSnapshot, Kind, ParsedItem } from '~~/shared/types'

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
    es.addEventListener('snapshot', (e) => {
      job.value = JSON.parse((e as MessageEvent).data) as JobSnapshot
      if (job.value.done)
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
