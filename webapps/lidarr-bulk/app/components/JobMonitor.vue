<script setup lang="ts">
import type { Candidate, ItemStatus, JobSnapshot, ParsedItem } from '~~/shared/types'

const props = defineProps<{
  job: JobSnapshot
  deduped?: number
}>()
const emit = defineEmits<{ choose: [itemId: string, candidate: Candidate | null] }>()

const TERMINAL: ItemStatus[] = [
  'done', 'nudged', 'already-added', 'would-add', 'not-found', 'error', 'skipped',
]
const counts = computed(() => {
  const out: Partial<Record<ItemStatus, number>> = {}
  for (const i of props.job.items)
    out[i.status] = (out[i.status] ?? 0) + 1
  return out
})
const needsAttention = computed(() => props.job.items.filter(i => i.status === 'needs-choice'))
const pending = computed(() => props.job.items.filter(i =>
  !TERMINAL.includes(i.status) && i.status !== 'needs-choice'))
const settled = computed(() => props.job.items.filter(i => TERMINAL.includes(i.status)))

function label(p: ParsedItem): string {
  return p.artist && p.title ? `${p.artist} — ${p.title}` : p.raw
}
</script>

<template>
  <div class="space-y-4">
    <UCard>
      <div class="flex items-center justify-between gap-2 flex-wrap">
        <div class="flex items-center gap-2 flex-wrap">
          <span class="font-semibold">{{ job.items.length }} items</span>
          <UBadge v-if="job.dryRun" color="warning" variant="subtle" label="dry-run" />
          <span v-if="deduped" class="text-sm text-muted">
            ({{ deduped }} duplicate{{ deduped === 1 ? '' : 's' }} removed)
          </span>
        </div>
        <div class="flex items-center gap-1.5 flex-wrap">
          <UBadge v-if="counts.done" color="success" variant="subtle" :label="`${counts.done} added`" />
          <UBadge v-if="counts.nudged" color="success" variant="subtle" :label="`${counts.nudged} nudged`" />
          <UBadge v-if="counts['would-add']" color="primary" variant="subtle" :label="`${counts['would-add']} would add`" />
          <UBadge v-if="counts['already-added']" color="success" variant="subtle" :label="`${counts['already-added']} already in lidarr`" />
          <UBadge v-if="counts['needs-choice']" color="warning" variant="subtle" :label="`${counts['needs-choice']} need pick`" />
          <UBadge v-if="counts['not-found']" color="error" variant="subtle" :label="`${counts['not-found']} not found`" />
          <UBadge v-if="counts.error" color="error" variant="subtle" :label="`${counts.error} errored`" />
          <UBadge v-if="counts.skipped" color="neutral" variant="subtle" :label="`${counts.skipped} skipped`" />
        </div>
      </div>
      <p v-if="job.done" class="text-sm text-muted mt-2">
        paste another batch above to continue
      </p>
    </UCard>

    <UCard v-if="needsAttention.length">
      <template #header>
        <span class="font-semibold text-warning">{{ needsAttention.length }} need your pick</span>
        <p class="text-sm text-muted mt-1">
          Multiple matches — pick the right one, or skip.
        </p>
      </template>
      <div class="space-y-4">
        <CandidatePicker
          v-for="item in needsAttention"
          :key="item.id"
          :label="label(item.parsed)"
          :candidates="item.candidates ?? []"
          @pick="(c) => emit('choose', item.id, c)"
          @skip="emit('choose', item.id, null)"
        />
      </div>
    </UCard>

    <UCard v-if="pending.length">
      <template #header>
        <span class="text-sm text-muted">Working ({{ pending.length }})</span>
      </template>
      <JobItemRow v-for="item in pending" :key="item.id" :item="item" />
    </UCard>

    <UCard v-if="settled.length">
      <template #header>
        <span class="text-sm text-muted">Settled ({{ settled.length }})</span>
      </template>
      <JobItemRow v-for="item in settled" :key="item.id" :item="item" />
    </UCard>
  </div>
</template>
