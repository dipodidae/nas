<script setup lang="ts">
import type { Candidate } from '~~/shared/types'

defineProps<{
  label: string
  candidates: Candidate[]
}>()
const emit = defineEmits<{ pick: [Candidate], skip: [] }>()

function thumb(c: Candidate): string {
  const imgs = c.value.images ?? []
  const cover = imgs.find(i => i.coverType === 'cover' || i.coverType === 'poster')
  return cover?.remoteUrl ?? cover?.url ?? ''
}
function title(c: Candidate): string {
  return c.kind === 'artist' ? c.value.artistName : c.value.title
}
function subtitle(c: Candidate): string {
  if (c.kind === 'artist')
    return [c.value.artistType, c.value.disambiguation].filter(Boolean).join(' • ')
  const a = c.value
  const artistName = typeof a.artist === 'string' ? a.artist : a.artist?.artistName
  const year = a.releaseDate ? a.releaseDate.slice(0, 4) : ''
  return [artistName, a.albumType, year].filter(Boolean).join(' • ')
}
</script>

<template>
  <div>
    <p class="font-medium mb-2">
      {{ label }}
    </p>
    <div class="space-y-2">
      <div
        v-for="(c, i) in candidates"
        :key="i"
        class="flex items-center gap-3 p-2 rounded-md ring ring-default"
      >
        <UAvatar :src="thumb(c)" :alt="title(c)" size="lg" icon="i-lucide-disc-3" />
        <div class="flex-1 min-w-0">
          <p class="truncate">
            {{ title(c) }}
          </p>
          <p class="text-sm text-muted truncate">
            {{ subtitle(c) }}
          </p>
        </div>
        <UButton color="primary" variant="soft" label="Pick" @click="emit('pick', c)" />
      </div>
    </div>
    <UButton class="mt-2" color="neutral" variant="ghost" label="Skip" @click="emit('skip')" />
  </div>
</template>
