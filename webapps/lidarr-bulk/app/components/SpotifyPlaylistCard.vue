<script setup lang="ts">
import type { SpotifyPlaylist } from '~~/shared/types'

// Single playlist tile shared by both grids in SpotifyPanel (the connected
// account's own playlists and the public-search results). `busy` dims/disables
// the whole tile while any resolve/recreate is in flight; `resolving` /
// `recreating` mark whether *this* tile is the one in flight.
defineProps<{
  playlist: SpotifyPlaylist
  busy: boolean
  resolving: boolean
  recreating: boolean
  jellyfinEnabled: boolean
}>()

defineEmits<{ pick: [], recreate: [] }>()
</script>

<template>
  <div
    class="text-left rounded-lg border border-default p-2 transition"
    :class="{ 'opacity-50': busy }"
  >
    <button
      type="button"
      :disabled="busy"
      class="block w-full text-left hover:opacity-90"
      @click="$emit('pick')"
    >
      <img v-if="playlist.imageUrl" :src="playlist.imageUrl" :alt="playlist.name" class="w-full aspect-square object-cover rounded-md mb-2">
      <div v-else class="w-full aspect-square rounded-md mb-2 bg-elevated flex items-center justify-center">
        <UIcon name="i-lucide-music" class="text-2xl text-muted" />
      </div>
      <div class="text-sm font-medium truncate">
        {{ playlist.name }}
      </div>
      <div class="text-xs text-muted truncate">
        <template v-if="resolving">resolving…</template>
        <template v-else>
          {{ playlist.trackCount }} track{{ playlist.trackCount === 1 ? '' : 's' }}<template v-if="playlist.owner"> · {{ playlist.owner }}</template>
        </template>
      </div>
    </button>
    <UButton
      v-if="jellyfinEnabled"
      class="mt-2 w-full justify-center"
      size="xs"
      color="neutral"
      variant="soft"
      icon="i-lucide-list-music"
      :loading="recreating"
      :disabled="busy"
      :label="recreating ? 'Recreating…' : 'Recreate in Jellyfin'"
      @click="$emit('recreate')"
    />
  </div>
</template>
