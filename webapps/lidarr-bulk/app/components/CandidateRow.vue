<script setup lang="ts">
import type { Candidate } from '~~/shared/types'

const props = defineProps<{ candidate: Candidate }>()
defineEmits<{ pick: [Candidate] }>()

const thumb = computed(() => {
  const imgs = props.candidate.value.images ?? []
  const cover = imgs.find(i => i.coverType === 'cover' || i.coverType === 'poster')
  return cover?.remoteUrl ?? cover?.url ?? ''
})

const title = computed(() => {
  const v = props.candidate.value
  if (props.candidate.kind === 'artist')
    return v.artistName
  const album = v as { title: string }
  return album.title
})

const subtitle = computed(() => {
  const v = props.candidate.value
  if (props.candidate.kind === 'artist') {
    const a = v as { disambiguation?: string, artistType?: string }
    return [a.artistType, a.disambiguation].filter(Boolean).join(' • ')
  }
  const a = v as {
    albumType?: string
    releaseDate?: string
    artist?: string | { artistName?: string }
  }
  const artistName = typeof a.artist === 'string' ? a.artist : a.artist?.artistName
  const year = a.releaseDate ? a.releaseDate.slice(0, 4) : ''
  return [artistName, a.albumType, year].filter(Boolean).join(' • ')
})
</script>

<template>
  <div class="candidate">
    <img v-if="thumb" :src="thumb" alt="">
    <div v-else />
    <div>
      <div>{{ title }}</div>
      <div class="meta">
        {{ subtitle }}
      </div>
    </div>
    <button class="secondary" @click="$emit('pick', candidate)">
      pick
    </button>
  </div>
</template>
