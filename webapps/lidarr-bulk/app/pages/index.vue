<script setup lang="ts">
import type { Candidate, Kind, ParsedItem } from '~~/shared/types'

type Tab = 'artist' | 'album' | 'ai'
const tab = ref<Tab>('artist')
// The AI tab produces album rows, so it drives an album job under the hood.
const kind = computed<Kind>(() => (tab.value === 'ai' ? 'album' : tab.value))
const blob = ref('')
const deduped = ref(0)
const { job, start, choose } = useJob()

const jobInFlight = computed(() => !!job.value && !job.value.done)

const tabItems = [
  { label: 'Artists', value: 'artist', icon: 'i-lucide-mic-vocal' },
  { label: 'Albums', value: 'album', icon: 'i-lucide-disc-3' },
  { label: 'Discover ✨', value: 'ai', icon: 'i-lucide-sparkles' },
]

async function onStart(items: ParsedItem[], opts: {
  monitorMode: 'all' | 'future'
  dryRun: boolean
  metadataProfileId?: number
  qualityProfileId?: number
  deduped: number
}): Promise<void> {
  deduped.value = opts.deduped
  await start(kind.value, items, opts.monitorMode, {
    dryRun: opts.dryRun,
    metadataProfileId: opts.metadataProfileId,
    qualityProfileId: opts.qualityProfileId,
  })
}

function onChoose(itemId: string, candidate: Candidate | null): void {
  void choose(itemId, candidate)
}

const artistIntro = 'Paste artist names, one per line (or comma/semicolon/tab-separated). Multi-word names stay together. Exact matches add automatically; you only pick when there\'s a real ambiguity.'
const albumIntro = 'Paste albums as "Artist - Album", "Album by Artist", "Artist | Album", or CSV, one per line.'
</script>

<template>
  <div class="space-y-4">
    <UTabs v-model="tab" :items="tabItems" :content="false" />

    <AiDiscoverPanel v-if="tab === 'ai'" v-model:blob="blob" />

    <BulkAddForm
      v-model:blob="blob"
      :kind="kind"
      :job-in-flight="jobInFlight"
      @start="onStart"
    >
      <template #intro>
        <p class="text-sm text-muted mb-3">
          {{ tab === 'artist' ? artistIntro : albumIntro }}
        </p>
      </template>
    </BulkAddForm>

    <JobMonitor v-if="job" :job="job" :deduped="deduped" @choose="onChoose" />
  </div>
</template>
