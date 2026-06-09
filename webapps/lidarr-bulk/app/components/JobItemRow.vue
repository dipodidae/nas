<script setup lang="ts">
import type { JobItem } from '~~/shared/types'

const props = defineProps<{ item: JobItem }>()
const label = computed(() =>
  props.item.parsed.artist && props.item.parsed.title
    ? `${props.item.parsed.artist} — ${props.item.parsed.title}`
    : props.item.parsed.raw,
)
</script>

<template>
  <div class="flex items-start gap-3 py-2 border-b border-default last:border-0">
    <div class="flex-1 min-w-0">
      <p class="truncate">
        {{ label }}
      </p>
      <p v-if="item.message" class="text-sm text-muted">
        {{ item.message }}
      </p>
    </div>
    <StatusBadge :status="item.status" />
  </div>
</template>
