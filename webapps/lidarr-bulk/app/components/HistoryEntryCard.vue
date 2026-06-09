<script setup lang="ts">
import type { HistoryEntry, ItemStatus, JobSnapshot } from '~~/shared/types'

const props = defineProps<{ entry: HistoryEntry }>()
const expanded = ref(false)
const replaying = ref(false)
const toast = useToast()

const RETRY = new Set<ItemStatus>(['not-found', 'error', 'skipped'])

function fmtDate(ms: number): string {
  return new Date(ms).toLocaleString()
}
function fmtDuration(a: number, b: number): string {
  const s = Math.max(0, Math.round((b - a) / 1000))
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`
}
function statusOrder(c: Partial<Record<ItemStatus, number>>): [string, number][] {
  const order: ItemStatus[] = ['done', 'nudged', 'already-added', 'would-add', 'needs-choice', 'not-found', 'error', 'skipped']
  return order.filter(k => c[k]).map(k => [k, c[k]!])
}
const retryableCount = computed(() => props.entry.items.filter(i => RETRY.has(i.status)).length)

function itemLabel(item: HistoryEntry['items'][number]): string {
  return item.parsed.artist && item.parsed.title
    ? `${item.parsed.artist} — ${item.parsed.title}`
    : item.parsed.raw
}

async function replay(all: boolean): Promise<void> {
  replaying.value = true
  try {
    const snap = await $fetch<JobSnapshot>(`/api/history/${props.entry.id}/replay`, { method: 'POST', body: { all } })
    // Land on / so the user can watch progress streaming on the main page.
    await navigateTo(`/?job=${snap.id}`)
  }
  catch (e) {
    toast.add({ title: 'Replay failed', description: (e as Error).message, color: 'error' })
  }
  finally {
    replaying.value = false
  }
}
</script>

<template>
  <UCard>
    <div class="flex items-start justify-between gap-2 flex-wrap">
      <div>
        <div class="flex items-center gap-2">
          <span class="font-semibold capitalize">{{ entry.kind }}</span>
          <UBadge v-if="entry.dryRun" color="warning" variant="subtle" label="dry-run" />
          <span class="text-sm text-muted">{{ entry.items.length }} items</span>
        </div>
        <p class="text-sm text-muted mt-1">
          {{ fmtDate(entry.createdAt) }} · took {{ fmtDuration(entry.createdAt, entry.finishedAt) }}
        </p>
        <div class="flex gap-1.5 flex-wrap mt-2">
          <UBadge v-for="[k, v] in statusOrder(entry.counts)" :key="k" color="neutral" variant="subtle" :label="`${k} ${v}`" />
        </div>
      </div>
      <div class="flex gap-1.5">
        <UButton color="neutral" variant="ghost" :label="expanded ? 'hide' : 'view'" @click="expanded = !expanded" />
        <UButton v-if="retryableCount > 0" :loading="replaying" :label="`retry failed (${retryableCount})`" @click="replay(false)" />
        <UButton color="neutral" variant="soft" :loading="replaying" label="replay all" @click="replay(true)" />
      </div>
    </div>

    <div v-if="expanded" class="mt-3">
      <div v-for="item in entry.items" :key="item.id" class="flex items-start gap-3 py-2 border-b border-default last:border-0">
        <div class="flex-1 min-w-0">
          <p class="truncate">
            {{ itemLabel(item) }}
          </p>
          <p v-if="item.message" class="text-sm text-muted">
            {{ item.message }}
          </p>
        </div>
        <StatusBadge :status="item.status" />
      </div>
    </div>
  </UCard>
</template>
