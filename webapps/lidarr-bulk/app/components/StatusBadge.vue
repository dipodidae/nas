<script setup lang="ts">
import type { ItemStatus } from '~~/shared/types'
import { statusBadge } from '~~/shared/status-badge'

const props = defineProps<{ status: ItemStatus }>()
const badge = computed(() => statusBadge(props.status))
</script>

<template>
  <UBadge
    :color="badge.color"
    :icon="badge.icon"
    :label="badge.label"
    variant="subtle"
    size="sm"
  />
</template>
