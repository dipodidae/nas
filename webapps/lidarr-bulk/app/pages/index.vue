<script setup lang="ts">
import type {
  Candidate,
  ItemStatus,
  Kind,
  LidarrProfilesResponse,
  ParsedItem,
} from '~~/shared/types'

const kind = ref<Kind>('artist')
const blob = ref('')
const monitorMode = ref<'all' | 'future'>('all')
const dryRun = ref(false)
const metadataProfileId = ref<number | null>(null)
const qualityProfileId = ref<number | null>(null)
const submitting = ref(false)
const parseSummary = ref<{ shown: number, deduped: number } | null>(null)
const { job, start, choose } = useJob()

// Profile pickers populated lazily — only when the user actually opens the
// "Advanced" panel (so the page loads without hitting Lidarr).
const profiles = ref<LidarrProfilesResponse | null>(null)
const showAdvanced = ref(false)
async function loadProfiles(): Promise<void> {
  if (profiles.value)
    return
  try {
    profiles.value = await $fetch<LidarrProfilesResponse>('/api/lidarr/profiles')
  }
  catch {
    profiles.value = null
  }
}
watch(showAdvanced, (open) => { if (open) void loadProfiles() })

const jobInFlight = computed(() => !!job.value && !job.value.done)
const canSubmit = computed(() =>
  Boolean(blob.value.trim()) && !submitting.value && !jobInFlight.value,
)

async function addAll(): Promise<void> {
  if (!canSubmit.value)
    return
  submitting.value = true
  parseSummary.value = null
  try {
    // Discography uses the artist parser (single names per line).
    const parseKind = kind.value === 'discography' ? 'artist' : kind.value
    const res = await $fetch<{ items: ParsedItem[] }>('/api/parse', {
      method: 'POST',
      body: { kind: parseKind, blob: blob.value },
    })
    const beforeDedup = blob.value
      .split(/[\n,;\t]+/)
      .map(s => s.trim())
      .filter(Boolean).length
    parseSummary.value = {
      shown: res.items.length,
      deduped: Math.max(0, beforeDedup - res.items.length),
    }
    await start(kind.value, res.items, monitorMode.value, {
      dryRun: dryRun.value,
      metadataProfileId: metadataProfileId.value ?? undefined,
      qualityProfileId: qualityProfileId.value ?? undefined,
    })
    blob.value = ''
  }
  finally {
    submitting.value = false
  }
}

function switchTab(k: Kind): void {
  kind.value = k
  parseSummary.value = null
}

const counts = computed(() => {
  const out: Record<string, number> = {}
  for (const i of job.value?.items ?? [])
    out[i.status] = (out[i.status] ?? 0) + 1
  return out
})
const needsAttention = computed(() =>
  (job.value?.items ?? []).filter(i => i.status === 'needs-choice'),
)
const TERMINAL: ItemStatus[] = [
  'done', 'already-added', 'would-add', 'not-found', 'error', 'skipped',
]
const settled = computed(() =>
  (job.value?.items ?? []).filter(i => TERMINAL.includes(i.status)),
)
const pending = computed(() =>
  (job.value?.items ?? []).filter(i =>
    !TERMINAL.includes(i.status) && i.status !== 'needs-choice',
  ),
)

function label(p: ParsedItem): string {
  return p.artist && p.title ? `${p.artist} — ${p.title}` : p.raw
}
function topCandidates(cands: Candidate[] | undefined): Candidate[] {
  return cands ?? []
}
function badge(status: ItemStatus): { text: string, cls: string } {
  switch (status) {
    case 'done': return { text: 'added', cls: 'done' }
    case 'already-added': return { text: 'already in lidarr', cls: 'done' }
    case 'would-add': return { text: 'would add (dry-run)', cls: 'done' }
    case 'not-found': return { text: 'not found', cls: 'not-found' }
    case 'error': return { text: 'error', cls: 'error' }
    case 'skipped': return { text: 'skipped', cls: 'skipped' }
    case 'needs-choice': return { text: 'pick one', cls: 'needs-choice' }
    case 'searching': return { text: 'searching…', cls: '' }
    case 'adding': return { text: 'adding…', cls: '' }
    case 'searching-on-lidarr': return { text: 'queued', cls: '' }
    default: return { text: status, cls: '' }
  }
}

</script>

<template>
  <div class="container">
    <div class="tabs">
      <div class="tab" :class="{ active: kind === 'artist' }" @click="switchTab('artist')">
        Artists
      </div>
      <div class="tab" :class="{ active: kind === 'album' }" @click="switchTab('album')">
        Albums
      </div>
    </div>

    <div class="panel">
      <p class="muted" style="margin-top:0">
        <template v-if="kind === 'artist'">
          Paste artist names, one per line (or comma/semicolon/tab-separated).
          Multi-word names stay together. Click <strong>Add all</strong> — exact
          matches add automatically; you only pick when there's a real ambiguity.
          For studio-only adds, set a metadata profile in
          <strong>Advanced</strong>.
        </template>
        <template v-else>
          Paste albums as
          <span class="kbd">Artist - Album</span>,
          <span class="kbd">Album by Artist</span>,
          <span class="kbd">Artist | Album</span>, or CSV
          <span class="kbd">"Artist","Album"</span>, one per line.
        </template>
      </p>
      <textarea
        v-model="blob"
        :placeholder="kind === 'artist'
          ? 'Satanic Warmaster\nBurzum\nMayhem'
          : 'Adele - 30\nBeyoncé - Lemonade'"
        @keydown.meta.enter="addAll"
        @keydown.ctrl.enter="addAll"
      />
      <div class="row" style="margin-top:12px">
        <button :disabled="!canSubmit" @click="addAll">
          <template v-if="submitting">
            starting…
          </template>
          <template v-else-if="jobInFlight">
            wait for current batch…
          </template>
          <template v-else-if="dryRun">
            Preview only
          </template>
          <template v-else>
            Add all
          </template>
        </button>
        <label class="muted">
          <input v-model="dryRun" type="checkbox" style="width:auto; margin-right:4px">
          dry-run (no writes)
        </label>
        <label class="muted">
          Monitor:
          <select v-model="monitorMode" style="width:auto; display:inline-block; margin-left:6px">
            <option value="all">all albums</option>
            <option value="future">future only</option>
          </select>
        </label>
        <span class="muted kbd" style="margin-left:auto">⌘/Ctrl+Enter</span>
      </div>
      <details style="margin-top:10px" @toggle="showAdvanced = ($event.target as HTMLDetailsElement).open">
        <summary class="muted" style="cursor:pointer">Advanced</summary>
        <div class="row" style="margin-top:8px; gap:16px; flex-wrap:wrap">
          <label class="muted">
            Quality profile:
            <select v-model="qualityProfileId" style="width:auto; display:inline-block; margin-left:6px">
              <option :value="null">
                (default)
              </option>
              <option v-for="p in profiles?.qualityProfiles ?? []" :key="p.id" :value="p.id">
                {{ p.name }}
              </option>
            </select>
          </label>
          <label class="muted">
            Metadata profile:
            <select v-model="metadataProfileId" style="width:auto; display:inline-block; margin-left:6px">
              <option :value="null">
                (default)
              </option>
              <option v-for="p in profiles?.metadataProfiles ?? []" :key="p.id" :value="p.id">
                {{ p.name }}
              </option>
            </select>
          </label>
          <span v-if="!profiles" class="meta">loading profiles…</span>
        </div>
      </details>
    </div>

    <div v-if="job" class="panel">
      <div class="row" style="justify-content:space-between">
        <div>
          <strong>{{ job.items.length }} items</strong>
          <span v-if="job.dryRun" class="muted" style="color:var(--warn); margin-left:6px">[dry-run]</span>
          <span v-if="parseSummary?.deduped" class="muted">
            ({{ parseSummary.deduped }} duplicate{{ parseSummary.deduped === 1 ? '' : 's' }} removed)
          </span>
          <span class="muted" style="margin-left:12px">
            <template v-if="counts.done">✓ {{ counts.done }} added</template>
            <template v-if="counts['would-add']"> · ◇ {{ counts['would-add'] }} would add</template>
            <template v-if="counts['already-added']"> · ✓ {{ counts['already-added'] }} already in lidarr</template>
            <template v-if="counts['needs-choice']"> · ⚠ {{ counts['needs-choice'] }} need pick</template>
            <template v-if="counts['not-found']"> · ✗ {{ counts['not-found'] }} not found</template>
            <template v-if="counts.error"> · ✗ {{ counts.error }} errored</template>
            <template v-if="counts.skipped"> · {{ counts.skipped }} skipped</template>
          </span>
        </div>
        <span v-if="job.done" class="muted" style="font-size:12px">
          paste another batch above to continue
        </span>
      </div>
    </div>

    <div v-if="needsAttention.length" class="panel">
      <strong style="color:var(--warn)">{{ needsAttention.length }} need your pick</strong>
      <p class="muted" style="margin:6px 0 12px">
        Multiple matches — pick the right one, or skip.
      </p>
      <div v-for="item in needsAttention" :key="item.id" class="item" style="flex-direction:column; align-items:stretch">
        <div style="margin-bottom:6px">
          <strong>{{ label(item.parsed) }}</strong>
        </div>
        <CandidateRow
          v-for="(c, ci) in topCandidates(item.candidates)"
          :key="ci"
          :candidate="c"
          @pick="choose(item.id, c)"
        />
        <div style="margin-top:6px">
          <button class="secondary" @click="choose(item.id, null)">
            skip
          </button>
        </div>
      </div>
    </div>

    <div v-if="pending.length" class="panel">
      <div class="muted" style="font-size:12px; margin-bottom:6px">
        Working ({{ pending.length }})
      </div>
      <div v-for="item in pending" :key="item.id" class="item">
        <div style="flex:1">
          {{ label(item.parsed) }}
        </div>
        <span class="status">{{ badge(item.status).text }}</span>
      </div>
    </div>

    <div v-if="settled.length" class="panel">
      <div class="muted" style="font-size:12px; margin-bottom:6px">
        Settled ({{ settled.length }})
      </div>
      <div v-for="item in settled" :key="item.id" class="item">
        <div style="flex:1">
          {{ label(item.parsed) }}
          <div v-if="item.message" class="meta">
            {{ item.message }}
          </div>
        </div>
        <span class="status" :class="badge(item.status).cls">{{ badge(item.status).text }}</span>
      </div>
    </div>
  </div>
</template>
