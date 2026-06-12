<script setup lang="ts">
import type { Candidate, Kind, ParsedItem } from '~~/shared/types'

type Tab = 'artist' | 'album' | 'ai' | 'spotify'
const tab = ref<Tab>('artist')
// The AI and Spotify tabs both produce album rows, so they drive an album job.
const kind = computed<Kind>(() => (tab.value === 'artist' ? 'artist' : 'album'))
const blob = ref('')
const deduped = ref(0)
const { job, start, choose } = useJob()

const jobInFlight = computed(() => !!job.value && !job.value.done)

const spotifyEnabled = ref(false)
const savedMonitorMode = ref<'all' | 'future'>('all')

onMounted(async () => {
  try {
    const s = await $fetch<{ enabled: boolean }>('/api/spotify/status')
    spotifyEnabled.value = s.enabled
  }
  catch {
    spotifyEnabled.value = false
  }
  try {
    const settings = await $fetch<{ monitorMode: 'all' | 'future' }>('/api/settings')
    savedMonitorMode.value = settings.monitorMode
  }
  catch {
    savedMonitorMode.value = 'all'
  }
})

const tabItems = computed(() => {
  const base = [
    { label: 'Artists', value: 'artist', icon: 'i-lucide-mic-vocal' },
    { label: 'Albums', value: 'album', icon: 'i-lucide-disc-3' },
    { label: 'Discover ✨', value: 'ai', icon: 'i-lucide-sparkles' },
  ]
  if (spotifyEnabled.value)
    base.push({ label: 'Spotify', value: 'spotify', icon: 'i-lucide-music' })
  return base
})

async function onSpotifyQueue(items: ParsedItem[]): Promise<void> {
  deduped.value = items.length
  await start('album', items, savedMonitorMode.value)
}

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

    <SpotifyPanel v-if="tab === 'spotify'" @queue="onSpotifyQueue" />

    <BulkAddForm
      v-if="tab !== 'spotify'"
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
