<script setup lang="ts">
import type { HistoryEntry } from '~~/shared/types'

const { data, refresh, pending } = await useFetch<{ entries: HistoryEntry[] }>('/api/history?limit=50')
</script>

<template>
  <div class="space-y-4">
    <div class="flex items-center justify-between">
      <h2 class="text-xl font-semibold">
        History
      </h2>
      <UButton color="neutral" variant="soft" :loading="pending" label="refresh" @click="refresh()" />
    </div>

    <UCard v-if="!data?.entries?.length">
      <p class="text-muted">
        No completed jobs yet. Run a batch on the Add page and it'll show up here.
      </p>
    </UCard>

    <HistoryEntryCard v-for="entry in data?.entries ?? []" :key="entry.id" :entry="entry" />
  </div>
</template>
