<script setup lang="ts">
import type { HistoryEntry, ItemStatus, JobSnapshot } from '~~/shared/types'

const { data, refresh, pending } = await useFetch<{ entries: HistoryEntry[] }>(
  '/api/history?limit=50',
)
const expanded = ref<Record<string, boolean>>({})
const replaying = ref<string | null>(null)

const RETRY = new Set<ItemStatus>(['not-found', 'error', 'skipped'])

function fmtDate(ms: number): string {
  return new Date(ms).toLocaleString()
}
function fmtDuration(a: number, b: number): string {
  const s = Math.max(0, Math.round((b - a) / 1000))
  if (s < 60) return `${s}s`
  return `${Math.floor(s / 60)}m ${s % 60}s`
}
function statusOrder(c: Partial<Record<ItemStatus, number>>): [string, number][] {
  const order: ItemStatus[] = [
    'done', 'already-added', 'would-add', 'needs-choice', 'not-found',
    'error', 'skipped',
  ]
  return order.filter(k => c[k]).map(k => [k, c[k]!])
}
function retryableCount(entry: HistoryEntry): number {
  return entry.items.filter(i => RETRY.has(i.status)).length
}

async function replay(entry: HistoryEntry, all: boolean): Promise<void> {
  replaying.value = entry.id
  try {
    const snap = await $fetch<JobSnapshot>(`/api/history/${entry.id}/replay`, {
      method: 'POST',
      body: { all },
    })
    // Land on / so the user can watch progress streaming on the main page.
    await navigateTo(`/?job=${snap.id}`)
  }
  catch (e) {
    alert(`replay failed: ${(e as Error).message}`)
  }
  finally {
    replaying.value = null
  }
}
</script>

<template>
  <div class="container">
    <div class="row" style="justify-content:space-between; margin-bottom:8px">
      <h2 style="margin:0">
        History
      </h2>
      <button class="secondary" :disabled="pending" @click="refresh()">
        refresh
      </button>
    </div>

    <div v-if="!data?.entries?.length" class="panel">
      <p class="muted" style="margin:0">
        No completed jobs yet. Run a batch on the main page and it'll show up here.
      </p>
    </div>

    <div v-for="entry in data?.entries ?? []" :key="entry.id" class="panel">
      <div class="row" style="justify-content:space-between; align-items:flex-start">
        <div>
          <strong>{{ entry.kind }}</strong>
          <span v-if="entry.dryRun" class="muted" style="color:var(--warn); margin-left:6px">[dry-run]</span>
          <span class="muted" style="margin-left:8px">{{ entry.items.length }} items</span>
          <div class="meta" style="margin-top:4px">
            {{ fmtDate(entry.createdAt) }} · took {{ fmtDuration(entry.createdAt, entry.finishedAt) }}
          </div>
          <div style="margin-top:6px">
            <span v-for="[k, v] in statusOrder(entry.counts)" :key="k" class="kbd" style="margin-right:6px">
              {{ k }} {{ v }}
            </span>
          </div>
        </div>
        <div class="row" style="gap:6px">
          <button class="secondary" @click="expanded[entry.id] = !expanded[entry.id]">
            {{ expanded[entry.id] ? 'hide' : 'view' }}
          </button>
          <button
            v-if="retryableCount(entry) > 0"
            :disabled="replaying === entry.id"
            @click="replay(entry, false)"
          >
            retry failed ({{ retryableCount(entry) }})
          </button>
          <button
            class="secondary"
            :disabled="replaying === entry.id"
            @click="replay(entry, true)"
          >
            replay all
          </button>
        </div>
      </div>

      <div v-if="expanded[entry.id]" style="margin-top:12px">
        <div v-for="item in entry.items" :key="item.id" class="item">
          <div style="flex:1">
            {{ item.parsed.artist && item.parsed.title
              ? `${item.parsed.artist} — ${item.parsed.title}`
              : item.parsed.raw }}
            <div v-if="item.message" class="meta">
              {{ item.message }}
            </div>
          </div>
          <span class="status">{{ item.status }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.kbd { padding: 1px 6px; border: 1px solid var(--border); border-radius: 999px; font-size: 11px; }
</style>
