<script setup lang="ts">
import type { JellyfinPushResult, ParsedItem, SpotifyPlaylist, SpotifyResolveResult, SpotifyStatus } from '~~/shared/types'

// jobInFlight gates the batch-continue buttons: queueing starts a new job and
// replaces the monitor, so letting a second batch start mid-run would hide the
// batch already in progress.
const props = defineProps<{ jobInFlight?: boolean }>()
const jobInFlight = computed(() => props.jobInFlight === true)
const emit = defineEmits<{ queue: [items: ParsedItem[]] }>()
const toast = useToast()

// `connected` tracks whether the server holds a token — independent of whether
// the playlist fetch succeeds. A 403 (account not on the app's allowlist) leaves
// a valid token in place, so we keep `connected` true and surface `loadError`
// instead, which keeps Disconnect / "different account" reachable.
const connected = ref(false)
const playlists = ref<SpotifyPlaylist[]>([])
const loading = ref(false)
const loadError = ref<string | null>(null)
const resolvingId = ref<string | null>(null)
const jellyfinEnabled = ref(false)
const recreatingId = ref<string | null>(null)
const result = ref<JellyfinPushResult | null>(null)
const showResult = ref(false)

// Public-playlist search (all of Spotify, not just this account's playlists).
const searchQuery = ref('')
const searchResults = ref<SpotifyPlaylist[]>([])
const searching = ref(false)
const searchError = ref<string | null>(null)
const searched = ref(false) // a search has run — distinguishes "no results" from "not searched yet"

// One resolve/recreate at a time, shared across both grids so a click in one
// dims the other too.
const busy = computed(() => resolvingId.value !== null || recreatingId.value !== null)

function describeError(err: unknown): string {
  const e = err as { statusMessage?: string, data?: { statusMessage?: string }, message?: string }
  return e.data?.statusMessage ?? e.statusMessage ?? e.message ?? 'Spotify request failed.'
}

async function loadPlaylists(): Promise<void> {
  loading.value = true
  loadError.value = null
  try {
    const res = await $fetch<{ playlists: SpotifyPlaylist[] }>('/api/spotify/playlists')
    playlists.value = res.playlists
  }
  catch (err: unknown) {
    // Keep `connected` as-is — a token still exists server-side. Surface the
    // error so the user can disconnect or reconnect with a different account.
    loadError.value = describeError(err)
  }
  finally {
    loading.value = false
  }
}

onMounted(async () => {
  try {
    const s = await $fetch<SpotifyStatus>('/api/spotify/status')
    connected.value = s.connected
    jellyfinEnabled.value = s.jellyfin
  }
  catch {
    connected.value = false
  }
  // Surface the OAuth callback outcome carried back as a query flag.
  const flag = useRoute().query.spotify
  if (flag === 'connected') {
    connected.value = true
    toast.add({ title: 'Spotify connected', color: 'success' })
  }
  else if (flag === 'error') {
    toast.add({ title: 'Spotify connection failed', description: 'Authorization was cancelled or failed.', color: 'error' })
  }
  if (connected.value)
    await loadPlaylists()
})

async function runSearch(): Promise<void> {
  const q = searchQuery.value.trim()
  if (!q || searching.value)
    return
  searching.value = true
  searchError.value = null
  try {
    const res = await $fetch<{ playlists: SpotifyPlaylist[] }>('/api/spotify/search-playlists', { query: { q } })
    searchResults.value = res.playlists
    searched.value = true
  }
  catch (err: unknown) {
    searchError.value = describeError(err)
  }
  finally {
    searching.value = false
  }
}

async function disconnect(): Promise<void> {
  await $fetch('/api/spotify/disconnect', { method: 'POST' })
  connected.value = false
  playlists.value = []
  loadError.value = null
  toast.add({ title: 'Spotify disconnected', color: 'neutral' })
}

// Anything at or above this many albums gets confirmed rather than queued on the
// strength of one click. A 1842-track playlist expands to ~900 albums, which is a
// large, slow, hard-to-undo commitment to a music library.
const CONFIRM_THRESHOLD = 25
const BATCH_SIZE = 100

const pendingQueue = ref<{ playlist: SpotifyPlaylist, result: SpotifyResolveResult } | null>(null)

// Where a batched playlist got to. Queueing 100 at a time is only useful if you
// can queue the *next* 100, so the resolved list and a cursor stay put until the
// playlist is exhausted or explicitly dismissed.
const batch = ref<{
  playlist: SpotifyPlaylist
  items: ParsedItem[]
  tracks: number
  queued: number
} | null>(null)

const batchRemaining = computed(() => {
  const b = batch.value
  return b ? b.items.length - b.queued : 0
})

function queueNow(items: ParsedItem[], playlist: SpotifyPlaylist, tracks: number): void {
  toast.add({
    title: `Queuing ${items.length} album${items.length === 1 ? '' : 's'}`,
    description: `from ${tracks} tracks in “${playlist.name}”`,
    color: 'success',
  })
  emit('queue', items)
}

function confirmQueueAll(): void {
  const p = pendingQueue.value
  if (!p)
    return
  pendingQueue.value = null
  batch.value = null
  queueNow(p.result.items, p.playlist, p.result.stats.tracks)
}

function confirmQueueFirst(): void {
  const p = pendingQueue.value
  if (!p)
    return
  pendingQueue.value = null
  batch.value = {
    playlist: p.playlist,
    items: p.result.items,
    tracks: p.result.stats.tracks,
    queued: 0,
  }
  queueNextBatch()
}

function queueNextBatch(): void {
  const b = batch.value
  if (!b)
    return
  const next = b.items.slice(b.queued, b.queued + BATCH_SIZE)
  if (next.length === 0) {
    batch.value = null
    return
  }
  b.queued += next.length
  queueNow(next, b.playlist, b.tracks)
  if (b.queued >= b.items.length) {
    batch.value = null
    toast.add({ title: 'Playlist fully queued', description: `all ${b.items.length} albums`, color: 'success' })
  }
}

function queueAllRemaining(): void {
  const b = batch.value
  if (!b)
    return
  const rest = b.items.slice(b.queued)
  batch.value = null
  if (rest.length > 0)
    queueNow(rest, b.playlist, b.tracks)
}

async function pick(playlist: SpotifyPlaylist): Promise<void> {
  if (resolvingId.value)
    return
  resolvingId.value = playlist.id
  try {
    const res = await $fetch<SpotifyResolveResult>('/api/spotify/resolve', {
      method: 'POST',
      body: { playlistId: playlist.id },
    })
    if (res.items.length === 0) {
      toast.add({ title: 'No albums', description: 'This playlist has no resolvable albums (local files / episodes only).', color: 'warning' })
      return
    }
    if (res.items.length >= CONFIRM_THRESHOLD) {
      pendingQueue.value = { playlist, result: res }
      return
    }
    queueNow(res.items, playlist, res.stats.tracks)
  }
  catch (err: unknown) {
    toast.add({ title: 'Resolve failed', description: describeError(err), color: 'error' })
  }
  finally {
    resolvingId.value = null
  }
}

async function recreate(playlist: SpotifyPlaylist): Promise<void> {
  if (recreatingId.value)
    return
  recreatingId.value = playlist.id
  try {
    const res = await $fetch<JellyfinPushResult>('/api/spotify/to-jellyfin', {
      method: 'POST',
      body: { playlistId: playlist.id, playlistName: playlist.name, imageUrl: playlist.imageUrl },
    })
    result.value = res
    showResult.value = true
  }
  catch (err: unknown) {
    toast.add({ title: 'Recreate failed', description: describeError(err), color: 'error' })
  }
  finally {
    recreatingId.value = null
  }
}
</script>

<template>
  <UCard>
    <template v-if="!connected">
      <p class="text-muted mt-0 text-sm">
        Connect your Spotify account, then click a playlist to queue every unique album behind its tracks into Lidarr.
      </p>
      <UButton class="mt-3" icon="i-lucide-music" label="Connect Spotify" to="/api/spotify/login" external />
    </template>

    <template v-else>
      <!-- Large playlists are confirmed, not queued on a single click: a 1842-track
           playlist is ~900 albums, which takes a long time and is tedious to undo.
           This has to be a modal, not an inline panel — the playlist grids are long,
           so a panel at the top of the card renders far above the viewport when you
           click something near the bottom and reads as "nothing happened". -->
      <UModal
        :open="pendingQueue !== null"
        title="Queue these albums?"
        @update:open="(v: boolean) => { if (!v) pendingQueue = null }"
      >
        <template #body>
          <div v-if="pendingQueue">
            <p class="font-medium m-0 truncate">
              {{ pendingQueue.playlist.name }}
            </p>
            <p class="text-sm text-muted mt-1 mb-0">
              {{ pendingQueue.result.stats.tracks }} tracks →
              <strong>{{ pendingQueue.result.items.length }} unique albums</strong>
              <template v-if="pendingQueue.result.stats.skipped > 0">
                ({{ pendingQueue.result.stats.skipped }} skipped)
              </template>
            </p>
          </div>
        </template>
        <template #footer>
          <div class="flex items-center gap-2 flex-wrap">
            <UButton
              color="primary"
              :label="`Queue all ${pendingQueue?.result.items.length ?? 0}`"
              @click="confirmQueueAll"
            />
            <UButton
              v-if="(pendingQueue?.result.items.length ?? 0) > 100"
              color="neutral"
              variant="soft"
              :label="`In batches of ${BATCH_SIZE}`"
              @click="confirmQueueFirst"
            />
            <UButton color="neutral" variant="ghost" label="Cancel" @click="pendingQueue = null" />
          </div>
        </template>
      </UModal>

      <!-- Continue a batched playlist. Queueing replaces the job view, so the next
           batch waits for the current one to finish rather than losing sight of it. -->
      <div
        v-if="batch && batchRemaining > 0"
        class="mb-4 p-3 rounded-md ring ring-default bg-elevated/50 flex items-center justify-between gap-3 flex-wrap"
      >
        <div class="min-w-0">
          <p class="font-medium m-0 truncate">
            {{ batch.playlist.name }}
          </p>
          <p class="text-sm text-muted m-0">
            queued {{ batch.queued }} of {{ batch.items.length }} —
            <strong>{{ batchRemaining }} left</strong>
            <template v-if="jobInFlight"> · waiting for the current batch to finish</template>
          </p>
        </div>
        <div class="flex items-center gap-2 flex-wrap">
          <UButton
            color="primary"
            :disabled="jobInFlight"
            :label="`Queue next ${Math.min(BATCH_SIZE, batchRemaining)}`"
            @click="queueNextBatch"
          />
          <UButton
            color="neutral"
            variant="soft"
            :disabled="jobInFlight"
            :label="`Queue all ${batchRemaining} remaining`"
            @click="queueAllRemaining"
          />
          <UButton color="neutral" variant="ghost" label="Dismiss" @click="batch = null" />
        </div>
      </div>

      <div class="flex items-center justify-between gap-4 flex-wrap">
        <p class="text-muted m-0 text-sm">
          Click a playlist to queue its unique albums using your saved default profiles and monitor mode.
        </p>
        <div class="flex items-center gap-2">
          <UButton size="xs" color="neutral" variant="soft" icon="i-lucide-repeat" label="Use a different account" to="/api/spotify/login" external />
          <UButton size="xs" color="neutral" variant="link" label="Disconnect" @click="disconnect" />
        </div>
      </div>

      <!-- Search all public Spotify playlists, not just this account's. -->
      <div class="mt-5">
        <p class="text-xs uppercase tracking-wide text-muted mb-2">
          Search all public playlists
        </p>
        <form class="flex gap-2" @submit.prevent="runSearch">
          <UInput
            v-model="searchQuery"
            class="flex-1"
            icon="i-lucide-search"
            placeholder="e.g. synthwave, 90s rock, rainy day…"
            :disabled="busy"
          />
          <UButton
            type="submit"
            icon="i-lucide-search"
            label="Search"
            :loading="searching"
            :disabled="busy || !searchQuery.trim()"
          />
        </form>

        <UAlert
          v-if="searchError"
          class="mt-3"
          color="warning"
          variant="soft"
          icon="i-lucide-triangle-alert"
          title="Search failed"
          :description="searchError"
        />
        <div v-else-if="searching" class="mt-3 text-sm text-muted">
          Searching…
        </div>
        <template v-else-if="searched">
          <div v-if="searchResults.length === 0" class="mt-3 text-sm text-muted">
            No public playlists matched “{{ searchQuery }}”.
          </div>
          <div v-else class="mt-3 grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
            <SpotifyPlaylistCard
              v-for="p in searchResults"
              :key="p.id"
              :playlist="p"
              :busy="busy"
              :resolving="resolvingId === p.id"
              :recreating="recreatingId === p.id"
              :jellyfin-enabled="jellyfinEnabled"
              @pick="pick(p)"
              @recreate="recreate(p)"
            />
          </div>
        </template>
      </div>

      <p class="text-xs uppercase tracking-wide text-muted mt-6 mb-2">
        Your playlists
      </p>
      <UAlert
        v-if="loadError"
        color="warning"
        variant="soft"
        icon="i-lucide-triangle-alert"
        title="Couldn’t load your playlists"
        :description="`${loadError} — if this is a 403, the account must be added under User Management in your Spotify app (apps in Development Mode only allow allowlisted accounts), or disconnect and connect a different account above.`"
      />
      <div v-else-if="loading" class="text-sm text-muted">
        Loading playlists…
      </div>
      <div v-else-if="playlists.length === 0" class="text-sm text-muted">
        No playlists found on your account.
      </div>
      <div v-else class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
        <SpotifyPlaylistCard
          v-for="p in playlists"
          :key="p.id"
          :playlist="p"
          :busy="busy"
          :resolving="resolvingId === p.id"
          :recreating="recreatingId === p.id"
          :jellyfin-enabled="jellyfinEnabled"
          @pick="pick(p)"
          @recreate="recreate(p)"
        />
      </div>
    </template>

    <UModal v-model:open="showResult" title="Recreate in Jellyfin">
      <template #body>
        <div v-if="result">
          <p class="text-sm">
            <span class="font-medium">{{ result.matched }}</span> of
            <span class="font-medium">{{ result.total }}</span> tracks matched in
            “{{ result.playlistName }}”.
          </p>
          <p v-if="result.matched === 0" class="text-sm text-muted mt-1">
            No tracks were found in your Jellyfin library — nothing was created.
          </p>
          <div v-if="result.skipped.length" class="mt-3">
            <p class="text-xs uppercase tracking-wide text-muted mb-1">
              Skipped ({{ result.skipped.length }})
            </p>
            <ul class="max-h-64 overflow-y-auto text-sm space-y-0.5">
              <li v-for="(s, i) in result.skipped" :key="i" class="truncate">
                {{ s.artist }} — {{ s.title }}
              </li>
            </ul>
          </div>
        </div>
      </template>
    </UModal>
  </UCard>
</template>
