<script setup lang="ts">
import type { JellyfinPushResult, ParsedItem, SpotifyPlaylist, SpotifyResolveResult, SpotifyStatus } from '~~/shared/types'

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

async function disconnect(): Promise<void> {
  await $fetch('/api/spotify/disconnect', { method: 'POST' })
  connected.value = false
  playlists.value = []
  loadError.value = null
  toast.add({ title: 'Spotify disconnected', color: 'neutral' })
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
    toast.add({
      title: `Queuing ${res.items.length} album${res.items.length === 1 ? '' : 's'}`,
      description: `from ${res.stats.tracks} tracks in “${playlist.name}”`,
      color: 'success',
    })
    emit('queue', res.items)
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
      body: { playlistId: playlist.id, playlistName: playlist.name },
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
      <div class="flex items-center justify-between gap-4 flex-wrap">
        <p class="text-muted m-0 text-sm">
          Click a playlist to queue its unique albums using your saved default profiles and monitor mode.
        </p>
        <div class="flex items-center gap-2">
          <UButton size="xs" color="neutral" variant="soft" icon="i-lucide-repeat" label="Use a different account" to="/api/spotify/login" external />
          <UButton size="xs" color="neutral" variant="link" label="Disconnect" @click="disconnect" />
        </div>
      </div>

      <UAlert
        v-if="loadError"
        class="mt-4"
        color="warning"
        variant="soft"
        icon="i-lucide-triangle-alert"
        title="Couldn’t load your playlists"
        :description="`${loadError} — if this is a 403, the account must be added under User Management in your Spotify app (apps in Development Mode only allow allowlisted accounts), or disconnect and connect a different account above.`"
      />
      <div v-else-if="loading" class="mt-4 text-sm text-muted">
        Loading playlists…
      </div>
      <div v-else-if="playlists.length === 0" class="mt-4 text-sm text-muted">
        No playlists found on your account.
      </div>
      <div v-else class="mt-4 grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
        <div
          v-for="p in playlists"
          :key="p.id"
          class="text-left rounded-lg border border-default p-2 transition"
          :class="{ 'opacity-50': resolvingId !== null || recreatingId !== null }"
        >
          <button
            type="button"
            :disabled="resolvingId !== null || recreatingId !== null"
            class="block w-full text-left hover:opacity-90"
            @click="pick(p)"
          >
            <img v-if="p.imageUrl" :src="p.imageUrl" :alt="p.name" class="w-full aspect-square object-cover rounded-md mb-2">
            <div v-else class="w-full aspect-square rounded-md mb-2 bg-elevated flex items-center justify-center">
              <UIcon name="i-lucide-music" class="text-2xl text-muted" />
            </div>
            <div class="text-sm font-medium truncate">
              {{ p.name }}
            </div>
            <div class="text-xs text-muted">
              <template v-if="resolvingId === p.id">resolving…</template>
              <template v-else>{{ p.trackCount }} track{{ p.trackCount === 1 ? '' : 's' }}</template>
            </div>
          </button>
          <UButton
            v-if="jellyfinEnabled"
            class="mt-2 w-full justify-center"
            size="xs"
            color="neutral"
            variant="soft"
            icon="i-lucide-list-music"
            :loading="recreatingId === p.id"
            :disabled="resolvingId !== null || recreatingId !== null"
            :label="recreatingId === p.id ? 'Recreating…' : 'Recreate in Jellyfin'"
            @click="recreate(p)"
          />
        </div>
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
