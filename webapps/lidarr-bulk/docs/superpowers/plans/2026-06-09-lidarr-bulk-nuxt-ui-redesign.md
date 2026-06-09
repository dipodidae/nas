# lidarr-bulk Nuxt UI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace lidarr-bulk's hand-rolled UI with Nuxt UI v4 components and extract the monolithic pages into focused, logic-owning components.

**Architecture:** Install `@nuxt/ui` v4 (free) with a violet/slate dark-first theme. Delete the bespoke CSS. Build a `default` layout (nav + color-mode toggle + user menu). Gut `index.vue` into `BulkAddForm`, `AiDiscoverPanel`, `JobMonitor`, `JobItemRow`, `CandidatePicker`, `StatusBadge`. Convert `login`/`settings`/`history` to `UForm`/`UCard`/`UCollapsible`. Toasts for transient errors, `UAlert` for persistent. `useJob.ts` is untouched.

**Tech Stack:** Nuxt 4, Nuxt UI v4 (`@nuxt/ui` + `tailwindcss`), Vue 3.5, TypeScript, Vitest.

**Component API note:** This codebase has never used Nuxt UI. The exact prop/slot names below reflect Nuxt UI v4 conventions; if `pnpm typecheck` or `pnpm build` reports a prop mismatch, consult the live docs via the `nuxt-ui-remote` MCP (`get-component` / `get-example`) and adjust — the structure stays the same.

**Verification baseline (run before starting):**
```bash
cd webapps/lidarr-bulk && pnpm test
```
Expected: existing suites (`lidarr`, `openai`, `external-prompt`) PASS. These must stay green throughout.

---

### Task 1: Install Nuxt UI and wire the theme

**Files:**
- Modify: `webapps/lidarr-bulk/package.json` (via pnpm)
- Modify: `webapps/lidarr-bulk/nuxt.config.ts`
- Replace: `webapps/lidarr-bulk/app/assets/css/main.css`
- Create: `webapps/lidarr-bulk/app/app.config.ts`
- Modify: `webapps/lidarr-bulk/app/app.vue`

- [ ] **Step 1: Install packages**

Run:
```bash
cd webapps/lidarr-bulk && pnpm add @nuxt/ui tailwindcss
```
Expected: `@nuxt/ui` and `tailwindcss` added to `dependencies`.

- [ ] **Step 2: Add the module to `nuxt.config.ts`**

Change the `modules` line to include `@nuxt/ui` and add a `colorMode` default. Final `modules` + new keys:
```ts
  modules: ['@nuxt/ui', 'nuxt-auth-utils'],
```
Remove the `{ name: 'color-scheme', content: 'dark light' }` meta entry from `app.head.meta` (color-mode handles this). Add at top level:
```ts
  colorMode: {
    preference: 'dark',
    fallback: 'dark',
  },
```

- [ ] **Step 3: Replace `app/assets/css/main.css` entirely**

```css
@import "tailwindcss";
@import "@nuxt/ui";
```

- [ ] **Step 4: Create `app/app.config.ts`**

```ts
export default defineAppConfig({
  ui: {
    colors: {
      primary: 'violet',
      neutral: 'slate',
    },
    button: {
      defaultVariants: {
        size: 'md',
      },
    },
  },
})
```

- [ ] **Step 5: Rewrite `app/app.vue`**

```vue
<template>
  <UApp>
    <NuxtLayout>
      <NuxtPage />
    </NuxtLayout>
  </UApp>
</template>
```

- [ ] **Step 6: Verify dev server boots and theme loads**

Run:
```bash
cd webapps/lidarr-bulk && pnpm build
```
Expected: build succeeds (Nuxt UI present, no missing-module error). The old nav/pages still render via their own markup until later tasks replace them.

- [ ] **Step 7: Commit**

```bash
git add webapps/lidarr-bulk/package.json webapps/lidarr-bulk/pnpm-lock.yaml webapps/lidarr-bulk/nuxt.config.ts webapps/lidarr-bulk/app/assets/css/main.css webapps/lidarr-bulk/app/app.config.ts webapps/lidarr-bulk/app/app.vue
git commit -m "feat(lidarr-bulk): install Nuxt UI v4 with violet/slate theme"
```

---

### Task 2: Status badge mapping (pure function + test)

**Files:**
- Create: `webapps/lidarr-bulk/shared/status-badge.ts`
- Create: `webapps/lidarr-bulk/tests/status-badge.test.ts`

- [ ] **Step 1: Write the failing test**

`webapps/lidarr-bulk/tests/status-badge.test.ts`:
```ts
import { describe, expect, it } from 'vitest'
import type { ItemStatus } from '../shared/types'
import { statusBadge } from '../shared/status-badge'

const ALL: ItemStatus[] = [
  'parsed', 'searching', 'needs-choice', 'matched', 'adding',
  'searching-on-lidarr', 'done', 'nudged', 'already-added',
  'would-add', 'not-found', 'error', 'skipped',
]

describe('statusBadge', () => {
  it('maps every ItemStatus to a label and color', () => {
    for (const s of ALL) {
      const b = statusBadge(s)
      expect(b.label.length).toBeGreaterThan(0)
      expect(b.color.length).toBeGreaterThan(0)
    }
  })

  it('uses success color for added states', () => {
    expect(statusBadge('done').color).toBe('success')
    expect(statusBadge('nudged').color).toBe('success')
    expect(statusBadge('already-added').color).toBe('success')
  })

  it('uses error color for failures', () => {
    expect(statusBadge('error').color).toBe('error')
    expect(statusBadge('not-found').color).toBe('error')
  })

  it('uses warning color for needs-choice', () => {
    expect(statusBadge('needs-choice').color).toBe('warning')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd webapps/lidarr-bulk && pnpm test status-badge`
Expected: FAIL — cannot resolve `../shared/status-badge`.

- [ ] **Step 3: Write `shared/status-badge.ts`**

```ts
import type { ItemStatus } from './types'

export type BadgeColor = 'neutral' | 'primary' | 'success' | 'warning' | 'error'

export interface StatusBadge {
  label: string
  color: BadgeColor
  icon?: string
}

// Single source of truth for how a job item's status renders. Replaces the
// inline badge() function and the .status CSS classes from the old index.vue.
export function statusBadge(status: ItemStatus): StatusBadge {
  switch (status) {
    case 'done': return { label: 'added', color: 'success', icon: 'i-lucide-check' }
    case 'nudged': return { label: 'nudged', color: 'success', icon: 'i-lucide-refresh-cw' }
    case 'already-added': return { label: 'already in lidarr', color: 'success', icon: 'i-lucide-check' }
    case 'would-add': return { label: 'would add (dry-run)', color: 'primary', icon: 'i-lucide-diamond' }
    case 'not-found': return { label: 'not found', color: 'error', icon: 'i-lucide-x' }
    case 'error': return { label: 'error', color: 'error', icon: 'i-lucide-triangle-alert' }
    case 'skipped': return { label: 'skipped', color: 'neutral' }
    case 'needs-choice': return { label: 'pick one', color: 'warning', icon: 'i-lucide-circle-help' }
    case 'searching': return { label: 'searching…', color: 'neutral' }
    case 'adding': return { label: 'adding…', color: 'neutral' }
    case 'searching-on-lidarr': return { label: 'queued', color: 'neutral' }
    case 'matched': return { label: 'matched', color: 'primary' }
    case 'parsed': return { label: 'parsed', color: 'neutral' }
    default: return { label: status, color: 'neutral' }
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd webapps/lidarr-bulk && pnpm test status-badge`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add webapps/lidarr-bulk/shared/status-badge.ts webapps/lidarr-bulk/tests/status-badge.test.ts
git commit -m "feat(lidarr-bulk): pure statusBadge mapping with test"
```

---

### Task 3: StatusBadge component

**Files:**
- Create: `webapps/lidarr-bulk/app/components/StatusBadge.vue`

- [ ] **Step 1: Write the component**

```vue
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
```

- [ ] **Step 2: Verify typecheck**

Run: `cd webapps/lidarr-bulk && pnpm typecheck`
Expected: no errors for `StatusBadge.vue`.

- [ ] **Step 3: Commit**

```bash
git add webapps/lidarr-bulk/app/components/StatusBadge.vue
git commit -m "feat(lidarr-bulk): StatusBadge component"
```

---

### Task 4: Default layout (nav chrome, color-mode, user menu)

**Files:**
- Create: `webapps/lidarr-bulk/app/layouts/default.vue`
- Modify: `webapps/lidarr-bulk/app/pages/login.vue` (add `layout: false` — confirm already present)

- [ ] **Step 1: Write `app/layouts/default.vue`**

Moves the `/api/me` + `logout()` logic out of `app.vue` (now empty of chrome).
```vue
<script setup lang="ts">
interface Me { authRequired: boolean, user: { name: string } | null }

const route = useRoute()
const colorMode = useColorMode()
const me = ref<Me | null>(null)

async function loadMe(): Promise<void> {
  try {
    me.value = await $fetch<Me>('/api/me')
  }
  catch {
    me.value = null
  }
}
watch(() => route.path, loadMe, { immediate: true })

async function logout(): Promise<void> {
  await $fetch('/api/logout', { method: 'POST' })
  await loadMe()
  await navigateTo('/login', { replace: true })
}

const navItems = computed(() => [
  { label: 'Add', icon: 'i-lucide-plus', to: '/' },
  { label: 'History', icon: 'i-lucide-history', to: '/history' },
  { label: 'Settings', icon: 'i-lucide-settings', to: '/settings' },
])

const userMenu = computed(() => [[
  { label: me.value?.user?.name ?? 'account', type: 'label' as const },
  { label: 'Sign out', icon: 'i-lucide-log-out', onSelect: () => { void logout() } },
]])

const isDark = computed({
  get: () => colorMode.value === 'dark',
  set: (v: boolean) => { colorMode.preference = v ? 'dark' : 'light' },
})
</script>

<template>
  <div class="min-h-screen bg-default text-default">
    <header class="border-b border-default">
      <UContainer class="flex items-center gap-4 h-14">
        <NuxtLink to="/" class="font-semibold text-highlighted">
          lidarr-bulk
        </NuxtLink>
        <UNavigationMenu :items="navItems" />
        <div class="ms-auto flex items-center gap-2">
          <ClientOnly>
            <UButton
              :icon="isDark ? 'i-lucide-moon' : 'i-lucide-sun'"
              color="neutral"
              variant="ghost"
              aria-label="Toggle color mode"
              @click="isDark = !isDark"
            />
          </ClientOnly>
          <UDropdownMenu v-if="me?.user" :items="userMenu">
            <UButton
              :label="me.user.name"
              icon="i-lucide-user"
              color="neutral"
              variant="ghost"
              trailing-icon="i-lucide-chevron-down"
            />
          </UDropdownMenu>
        </div>
      </UContainer>
    </header>
    <UContainer class="py-6">
      <slot />
    </UContainer>
  </div>
</template>
```

- [ ] **Step 2: Confirm `login.vue` opts out of the layout**

`app/pages/login.vue` already has `definePageMeta({ layout: false })` at the top — leave it (it is rewritten in Task 9, which preserves this).

- [ ] **Step 3: Verify build**

Run: `cd webapps/lidarr-bulk && pnpm build`
Expected: build succeeds; nav renders via Nuxt UI.

- [ ] **Step 4: Commit**

```bash
git add webapps/lidarr-bulk/app/layouts/default.vue
git commit -m "feat(lidarr-bulk): default layout with UNavigationMenu, color-mode toggle, user menu"
```

---

### Task 5: CandidatePicker component (replaces CandidateRow)

**Files:**
- Create: `webapps/lidarr-bulk/app/components/CandidatePicker.vue`
- Delete: `webapps/lidarr-bulk/app/components/CandidateRow.vue` (in Task 10 cleanup)

- [ ] **Step 1: Write `CandidatePicker.vue`**

Owns the thumb/title/subtitle derivation that lived in `CandidateRow.vue`, plus the pick/skip controls (so `index.vue` no longer renders the skip button itself).
```vue
<script setup lang="ts">
import type { Candidate } from '~~/shared/types'

const props = defineProps<{
  label: string
  candidates: Candidate[]
}>()
const emit = defineEmits<{ pick: [Candidate]; skip: [] }>()

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
          <p class="truncate">{{ title(c) }}</p>
          <p class="text-sm text-muted truncate">{{ subtitle(c) }}</p>
        </div>
        <UButton color="primary" variant="soft" label="Pick" @click="emit('pick', c)" />
      </div>
    </div>
    <UButton class="mt-2" color="neutral" variant="ghost" label="Skip" @click="emit('skip')" />
  </div>
</template>
```

- [ ] **Step 2: Verify typecheck**

Run: `cd webapps/lidarr-bulk && pnpm typecheck`
Expected: no errors for `CandidatePicker.vue`.

- [ ] **Step 3: Commit**

```bash
git add webapps/lidarr-bulk/app/components/CandidatePicker.vue
git commit -m "feat(lidarr-bulk): CandidatePicker component"
```

---

### Task 6: JobItemRow + JobMonitor components

**Files:**
- Create: `webapps/lidarr-bulk/app/components/JobItemRow.vue`
- Create: `webapps/lidarr-bulk/app/components/JobMonitor.vue`

- [ ] **Step 1: Write `JobItemRow.vue`**

```vue
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
      <p class="truncate">{{ label }}</p>
      <p v-if="item.message" class="text-sm text-muted">{{ item.message }}</p>
    </div>
    <StatusBadge :status="item.status" />
  </div>
</template>
```

- [ ] **Step 2: Write `JobMonitor.vue`**

Owns all the grouping/counts logic from the old `index.vue` (lines 162-209, 401-474). Emits `choose` so the parent (which holds `useJob`) performs the pick.
```vue
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
        <p class="text-sm text-muted mt-1">Multiple matches — pick the right one, or skip.</p>
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
```

- [ ] **Step 3: Verify typecheck**

Run: `cd webapps/lidarr-bulk && pnpm typecheck`
Expected: no errors for the two new files.

- [ ] **Step 4: Commit**

```bash
git add webapps/lidarr-bulk/app/components/JobItemRow.vue webapps/lidarr-bulk/app/components/JobMonitor.vue
git commit -m "feat(lidarr-bulk): JobMonitor + JobItemRow components"
```

---

### Task 7: BulkAddForm component

**Files:**
- Create: `webapps/lidarr-bulk/app/components/BulkAddForm.vue`

Owns parse → start logic (old `index.vue` lines 126-155), dry-run/monitor controls, and the advanced profile `UCollapsible` with lazy profile fetch (lines 104-119). Takes the shared `useJob` controls via props so the parent owns the single job instance.

- [ ] **Step 1: Write `BulkAddForm.vue`**

```vue
<script setup lang="ts">
import type { Kind, LidarrProfilesResponse, ParsedItem } from '~~/shared/types'

const props = defineProps<{
  kind: Kind
  jobInFlight: boolean
  blob: string
}>()
const emit = defineEmits<{
  'update:blob': [string]
  'start': [items: ParsedItem[], opts: {
    monitorMode: 'all' | 'future'
    dryRun: boolean
    metadataProfileId?: number
    qualityProfileId?: number
    deduped: number
  }]
}>()

const toast = useToast()
const localBlob = computed({
  get: () => props.blob,
  set: v => emit('update:blob', v),
})

const monitorMode = ref<'all' | 'future'>('all')
const dryRun = ref(false)
const metadataProfileId = ref<number | null>(null)
const qualityProfileId = ref<number | null>(null)
const submitting = ref(false)
const showAdvanced = ref(false)
const profiles = ref<LidarrProfilesResponse | null>(null)

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

const canSubmit = computed(() =>
  Boolean(props.blob.trim()) && !submitting.value && !props.jobInFlight)

const submitLabel = computed(() => {
  if (submitting.value) return 'starting…'
  if (props.jobInFlight) return 'wait for current batch…'
  if (dryRun.value) return 'Preview only'
  return 'Add all'
})

const monitorItems = [
  { label: 'all albums', value: 'all' },
  { label: 'future only', value: 'future' },
]
const qualityItems = computed(() => [
  { label: '(default)', value: null as number | null },
  ...(profiles.value?.qualityProfiles ?? []).map(p => ({ label: p.name, value: p.id })),
])
const metadataItems = computed(() => [
  { label: '(default)', value: null as number | null },
  ...(profiles.value?.metadataProfiles ?? []).map(p => ({ label: p.name, value: p.id })),
])

const placeholder = computed(() => props.kind === 'artist'
  ? 'Satanic Warmaster\nBurzum\nMayhem'
  : 'Adele - 30\nBeyoncé - Lemonade')

async function addAll(): Promise<void> {
  if (!canSubmit.value)
    return
  submitting.value = true
  try {
    const res = await $fetch<{ items: ParsedItem[] }>('/api/parse', {
      method: 'POST',
      body: { kind: props.kind, blob: props.blob },
    })
    const beforeDedup = props.blob.split(/[\n,;\t]+/).map(s => s.trim()).filter(Boolean).length
    emit('start', res.items, {
      monitorMode: monitorMode.value,
      dryRun: dryRun.value,
      metadataProfileId: metadataProfileId.value ?? undefined,
      qualityProfileId: qualityProfileId.value ?? undefined,
      deduped: Math.max(0, beforeDedup - res.items.length),
    })
    emit('update:blob', '')
  }
  catch (e) {
    const err = e as { data?: { statusMessage?: string }, statusMessage?: string, message?: string }
    toast.add({
      title: 'Add failed',
      description: err.data?.statusMessage ?? err.statusMessage ?? err.message ?? 'Could not parse or start the job.',
      color: 'error',
    })
  }
  finally {
    submitting.value = false
  }
}
</script>

<template>
  <UCard>
    <slot name="intro" />
    <UTextarea
      v-model="localBlob"
      :rows="8"
      :placeholder="placeholder"
      class="w-full font-mono"
      autoresize
      @keydown.meta.enter="addAll"
      @keydown.ctrl.enter="addAll"
    />
    <div class="flex items-center gap-4 flex-wrap mt-3">
      <UButton :loading="submitting" :disabled="!canSubmit" :label="submitLabel" @click="addAll" />
      <USwitch v-model="dryRun" label="dry-run (no writes)" />
      <UFormField label="Monitor" class="flex items-center gap-2">
        <USelect v-model="monitorMode" :items="monitorItems" />
      </UFormField>
      <UKbd value="⌘/Ctrl+Enter" class="ms-auto" />
    </div>

    <UCollapsible v-model:open="showAdvanced" class="mt-3">
      <UButton
        label="Advanced"
        color="neutral"
        variant="link"
        trailing-icon="i-lucide-chevron-down"
        class="p-0"
      />
      <template #content>
        <div class="flex gap-4 flex-wrap mt-3">
          <UFormField label="Quality profile">
            <USelect v-model="qualityProfileId" :items="qualityItems" />
          </UFormField>
          <UFormField label="Metadata profile">
            <USelect v-model="metadataProfileId" :items="metadataItems" />
          </UFormField>
          <span v-if="!profiles" class="text-sm text-muted self-end">loading profiles…</span>
        </div>
      </template>
    </UCollapsible>
  </UCard>
</template>
```

- [ ] **Step 2: Verify typecheck**

Run: `cd webapps/lidarr-bulk && pnpm typecheck`
Expected: no errors for `BulkAddForm.vue`.

- [ ] **Step 3: Commit**

```bash
git add webapps/lidarr-bulk/app/components/BulkAddForm.vue
git commit -m "feat(lidarr-bulk): BulkAddForm component"
```

---

### Task 8: AiDiscoverPanel component

**Files:**
- Create: `webapps/lidarr-bulk/app/components/AiDiscoverPanel.vue`

Owns AI status fetch, GPT generation, and the external-prompt builder (old `index.vue` lines 27-100, 227-309). Pushes generated rows up into the shared blob via `update:blob`.

- [ ] **Step 1: Write `AiDiscoverPanel.vue`**

```vue
<script setup lang="ts">
import type { PromptFlavors } from '~~/shared/external-prompt'
import type { ParsedItem } from '~~/shared/types'
import { buildExternalPrompt, PROMPT_FLAVORS } from '~~/shared/external-prompt'

const emit = defineEmits<{ 'update:blob': [string] }>()
const toast = useToast()

const aiEnabled = ref(false)
const aiModel = ref('')
const aiPrompt = ref('')
const aiCount = ref(25)
const aiGenerating = ref(false)
const aiGenerated = ref<number | null>(null)

onMounted(async () => {
  try {
    const s = await $fetch<{ enabled: boolean, model: string }>('/api/ai/status')
    aiEnabled.value = s.enabled
    aiModel.value = s.model
  }
  catch {
    aiEnabled.value = false
  }
})

const canGenerate = computed(() => Boolean(aiPrompt.value.trim()) && !aiGenerating.value)

async function generateList(): Promise<void> {
  if (!canGenerate.value)
    return
  aiGenerating.value = true
  aiGenerated.value = null
  try {
    const res = await $fetch<{ items: ParsedItem[] }>('/api/ai/suggest', {
      method: 'POST',
      body: { prompt: aiPrompt.value.trim(), count: aiCount.value },
    })
    emit('update:blob', res.items.map(i => i.raw).join('\n'))
    aiGenerated.value = res.items.length
    if (res.items.length === 0)
      toast.add({ title: 'No albums', description: 'GPT returned no usable albums — try a more specific prompt.', color: 'warning' })
  }
  catch (err: unknown) {
    const e = err as { statusMessage?: string, data?: { statusMessage?: string }, message?: string }
    toast.add({ title: 'Generation failed', description: e.data?.statusMessage ?? e.statusMessage ?? e.message ?? 'Generation failed.', color: 'error' })
  }
  finally {
    aiGenerating.value = false
  }
}

// External-prompt builder
const promptFlavors = ref<PromptFlavors>({})
const showPromptBuilder = ref(false)
const externalPrompt = computed(() =>
  buildExternalPrompt({ spec: aiPrompt.value.trim(), count: aiCount.value, flavors: promptFlavors.value }))

async function copyExternalPrompt(): Promise<void> {
  try {
    await navigator.clipboard.writeText(externalPrompt.value)
    toast.add({ title: 'Copied', description: 'Prompt copied to clipboard.', color: 'success' })
  }
  catch {
    toast.add({ title: 'Copy failed', color: 'error' })
  }
}
</script>

<template>
  <UCard>
    <p class="text-muted mt-0 text-sm">
      <template v-if="aiEnabled">
        Describe what you want and GPT<template v-if="aiModel"> ({{ aiModel }})</template>
        proposes real albums. Example:
        <UKbd value="The best 80s coldwave albums" />. The list lands in the box below as
        <UKbd value="Artist - Album" /> — review or trim it, then <strong>Add all</strong>.
      </template>
      <template v-else>
        Describe what you want — e.g. <UKbd value="The best 80s coldwave albums" />. In-app GPT
        generation is off (no <UKbd value="OPENAI_API_KEY" />), but you can still build a prompt
        below to run in <strong>Claude / ChatGPT</strong>, then paste its
        <UKbd value="Artist - Album" /> list into the box and <strong>Add all</strong>.
      </template>
    </p>
    <UTextarea
      v-model="aiPrompt"
      :rows="2"
      class="w-full mt-3"
      placeholder="The best 80s coldwave albums"
      @keydown.meta.enter="generateList"
      @keydown.ctrl.enter="generateList"
    />
    <div class="flex items-center gap-4 flex-wrap mt-3">
      <UButton v-if="aiEnabled" :loading="aiGenerating" :disabled="!canGenerate" :label="aiGenerating ? 'asking GPT…' : 'Generate list'" @click="generateList" />
      <UFormField label="Count">
        <UInputNumber v-model="aiCount" :min="1" :max="50" class="w-28" />
      </UFormField>
      <span v-if="aiGenerated !== null" class="text-sm text-muted">
        {{ aiGenerated }} album{{ aiGenerated === 1 ? '' : 's' }} ready below — review and Add all
      </span>
      <UKbd v-if="aiEnabled" value="⌘/Ctrl+Enter" class="ms-auto" />
    </div>

    <UCollapsible v-model:open="showPromptBuilder" class="mt-4">
      <UButton
        label="Take it elsewhere — build a prompt for Claude / ChatGPT"
        color="neutral"
        variant="link"
        trailing-icon="i-lucide-chevron-down"
        class="p-0"
      />
      <template #content>
        <p class="text-sm text-muted my-2">
          Run a deeper dive in an external LLM, then paste its <UKbd value="Artist - Album" /> list back into the box.
        </p>
        <div class="flex gap-4 flex-wrap">
          <UCheckbox
            v-for="f in PROMPT_FLAVORS"
            :key="f.key"
            v-model="promptFlavors[f.key]"
            :label="f.label"
          />
        </div>
        <UTextarea :model-value="externalPrompt" :rows="10" readonly class="w-full mt-3 font-mono text-xs" />
        <UButton class="mt-2" color="neutral" variant="soft" :disabled="!aiPrompt.trim()" label="Copy prompt" @click="copyExternalPrompt" />
      </template>
    </UCollapsible>
  </UCard>
</template>
```

- [ ] **Step 2: Verify typecheck**

Run: `cd webapps/lidarr-bulk && pnpm typecheck`
Expected: no errors. If `UInputNumber` is unavailable in the installed version, fall back to `<UInput v-model.number="aiCount" type="number" :min="1" :max="50" />`.

- [ ] **Step 3: Commit**

```bash
git add webapps/lidarr-bulk/app/components/AiDiscoverPanel.vue
git commit -m "feat(lidarr-bulk): AiDiscoverPanel component"
```

---

### Task 9: Rewrite index.vue to compose the new components

**Files:**
- Modify: `webapps/lidarr-bulk/app/pages/index.vue` (full rewrite)

- [ ] **Step 1: Rewrite `index.vue`**

```vue
<script setup lang="ts">
import type { Candidate, Kind, ParsedItem } from '~~/shared/types'

type Tab = 'artist' | 'album' | 'ai'
const tab = ref<Tab>('artist')
const kind = computed<Kind>(() => (tab.value === 'ai' ? 'album' : tab.value))
const blob = ref('')
const deduped = ref(0)
const { job, start, choose } = useJob()

const jobInFlight = computed(() => !!job.value && !job.value.done)

const tabItems = [
  { label: 'Artists', value: 'artist', icon: 'i-lucide-mic-vocal' },
  { label: 'Albums', value: 'album', icon: 'i-lucide-disc-3' },
  { label: 'Discover ✨', value: 'ai', icon: 'i-lucide-sparkles' },
]

async function onStart(items: ParsedItem[], opts: {
  monitorMode: 'all' | 'future'
  dryRun: boolean
  metadataProfileId?: number
  qualityProfileId?: number
  deduped: number
}): Promise<void> {
  deduped.value = opts.deduped
  await start(kind.value, items, opts.monitorMode, {
    dryRun: opts.dryRun,
    metadataProfileId: opts.metadataProfileId,
    qualityProfileId: opts.qualityProfileId,
  })
}

function onChoose(itemId: string, candidate: Candidate | null): void {
  void choose(itemId, candidate)
}

const artistIntro = 'Paste artist names, one per line (or comma/semicolon/tab-separated). Multi-word names stay together. Exact matches add automatically; you only pick when there\'s a real ambiguity.'
const albumIntro = 'Paste albums as "Artist - Album", "Album by Artist", "Artist | Album", or CSV, one per line.'
</script>

<template>
  <div class="space-y-4">
    <UTabs v-model="tab" :items="tabItems" :content="false" />

    <AiDiscoverPanel v-if="tab === 'ai'" v-model:blob="blob" />

    <BulkAddForm
      :kind="kind"
      :job-in-flight="jobInFlight"
      v-model:blob="blob"
      @start="onStart"
    >
      <template #intro>
        <p class="text-sm text-muted mb-3">
          {{ tab === 'artist' ? artistIntro : albumIntro }}
        </p>
      </template>
    </BulkAddForm>

    <JobMonitor v-if="job" :job="job" :deduped="deduped" @choose="onChoose" />
  </div>
</template>
```

- [ ] **Step 2: Verify build + typecheck**

Run: `cd webapps/lidarr-bulk && pnpm typecheck && pnpm build`
Expected: both succeed. Note: `UTabs` `v-model` binds the active `value`; `:content="false"` disables its built-in panels since we render panels ourselves.

- [ ] **Step 3: Manual smoke**

Run: `cd webapps/lidarr-bulk && pnpm dev` and confirm: tabs switch, paste + Add all starts a job, candidate pick/skip works, AI tab shows generate/prompt builder.

- [ ] **Step 4: Commit**

```bash
git add webapps/lidarr-bulk/app/pages/index.vue
git commit -m "refactor(lidarr-bulk): compose index.vue from Nuxt UI components"
```

---

### Task 10: Rewrite login.vue + delete CandidateRow

**Files:**
- Modify: `webapps/lidarr-bulk/app/pages/login.vue` (full rewrite)
- Delete: `webapps/lidarr-bulk/app/components/CandidateRow.vue`

- [ ] **Step 1: Rewrite `login.vue`**

```vue
<script setup lang="ts">
definePageMeta({ layout: false })

const route = useRoute()
const state = reactive({ username: '', password: '' })
const submitting = ref(false)
const toast = useToast()

async function submit(): Promise<void> {
  submitting.value = true
  try {
    await $fetch('/api/login', { method: 'POST', body: { ...state } })
    const next = typeof route.query.next === 'string' ? route.query.next : '/'
    window.location.assign(next)
  }
  catch (e) {
    const err = e as { statusMessage?: string, message?: string, data?: { statusMessage?: string, message?: string } }
    toast.add({
      title: 'Login failed',
      description: err.data?.statusMessage ?? err.data?.message ?? err.statusMessage ?? err.message ?? 'login failed',
      color: 'error',
    })
  }
  finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="min-h-screen flex items-center justify-center bg-default p-4">
    <UCard class="w-full max-w-sm">
      <template #header>
        <h2 class="text-lg font-semibold">lidarr-bulk</h2>
      </template>
      <UForm :state="state" class="space-y-4" @submit="submit">
        <UFormField label="Username" name="username">
          <UInput v-model="state.username" autocomplete="username" autofocus class="w-full" />
        </UFormField>
        <UFormField label="Password" name="password">
          <UInput v-model="state.password" type="password" autocomplete="current-password" class="w-full" />
        </UFormField>
        <UButton
          type="submit"
          :loading="submitting"
          :disabled="!state.username || !state.password"
          :label="submitting ? 'signing in…' : 'sign in'"
          block
        />
      </UForm>
    </UCard>
  </div>
</template>
```

- [ ] **Step 2: Delete the old CandidateRow**

Run: `git rm webapps/lidarr-bulk/app/components/CandidateRow.vue`
Expected: file removed (replaced by `CandidatePicker.vue`).

- [ ] **Step 3: Verify typecheck**

Run: `cd webapps/lidarr-bulk && pnpm typecheck`
Expected: no errors, no remaining references to `CandidateRow`.

- [ ] **Step 4: Commit**

```bash
git add webapps/lidarr-bulk/app/pages/login.vue webapps/lidarr-bulk/app/components/CandidateRow.vue
git commit -m "refactor(lidarr-bulk): Nuxt UI login form; remove CandidateRow"
```

---

### Task 11: Rewrite settings.vue

**Files:**
- Modify: `webapps/lidarr-bulk/app/pages/settings.vue` (full rewrite)

- [ ] **Step 1: Rewrite `settings.vue`**

```vue
<script setup lang="ts">
import type { AppSettings, LidarrProfilesResponse } from '~~/shared/types'

const { data: profiles, error: profilesErr } = await useFetch<LidarrProfilesResponse>('/api/lidarr/profiles')
const { data: settings, refresh } = await useFetch<AppSettings>('/api/settings')
const saving = ref(false)
const toast = useToast()

const rootItems = computed(() => (profiles.value?.rootFolders ?? []).map(r => ({
  label: `${r.path} (${r.accessible ? 'ok' : 'unreachable'})`,
  value: r.path,
})))
const qualityItems = computed(() => (profiles.value?.qualityProfiles ?? []).map(q => ({ label: q.name, value: q.id })))
const metadataItems = computed(() => (profiles.value?.metadataProfiles ?? []).map(m => ({ label: m.name, value: m.id })))
const monitorItems = [
  { label: 'all albums', value: 'all' },
  { label: 'future only', value: 'future' },
]

async function save(): Promise<void> {
  if (!settings.value)
    return
  saving.value = true
  try {
    await $fetch('/api/settings', { method: 'PUT', body: settings.value })
    toast.add({ title: 'Saved', color: 'success' })
    await refresh()
  }
  catch (e) {
    toast.add({ title: 'Save failed', description: (e as Error).message, color: 'error' })
  }
  finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="space-y-4">
    <div>
      <h2 class="text-xl font-semibold">Settings</h2>
      <p class="text-sm text-muted mt-1">
        Defaults applied when adding artists or albums to Lidarr. Persisted to
        <UKbd value="/config/settings.json" /> in the container.
      </p>
    </div>

    <UAlert
      v-if="profilesErr"
      color="error"
      icon="i-lucide-triangle-alert"
      title="Can't reach Lidarr."
      :description="profilesErr.message"
    />

    <UCard v-else-if="profiles && settings">
      <UForm :state="settings" class="space-y-4" @submit="save">
        <UFormField label="Root folder" name="rootFolderPath">
          <USelect v-model="settings.rootFolderPath" :items="rootItems" class="w-full" />
        </UFormField>
        <UFormField label="Quality profile" name="qualityProfileId">
          <USelect v-model="settings.qualityProfileId" :items="qualityItems" class="w-full" />
        </UFormField>
        <UFormField label="Metadata profile" name="metadataProfileId">
          <USelect v-model="settings.metadataProfileId" :items="metadataItems" class="w-full" />
        </UFormField>
        <UFormField label="Monitor mode (default for new adds)" name="monitorMode">
          <USelect v-model="settings.monitorMode" :items="monitorItems" class="w-full" />
        </UFormField>
        <UButton type="submit" :loading="saving" :label="saving ? 'saving…' : 'save'" />
      </UForm>
    </UCard>
  </div>
</template>
```

- [ ] **Step 2: Verify typecheck**

Run: `cd webapps/lidarr-bulk && pnpm typecheck`
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add webapps/lidarr-bulk/app/pages/settings.vue
git commit -m "refactor(lidarr-bulk): Nuxt UI settings form"
```

---

### Task 12: Rewrite history.vue + HistoryEntryCard

**Files:**
- Create: `webapps/lidarr-bulk/app/components/HistoryEntryCard.vue`
- Modify: `webapps/lidarr-bulk/app/pages/history.vue` (full rewrite)

- [ ] **Step 1: Write `HistoryEntryCard.vue`**

```vue
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
          <p class="truncate">{{ itemLabel(item) }}</p>
          <p v-if="item.message" class="text-sm text-muted">{{ item.message }}</p>
        </div>
        <StatusBadge :status="item.status" />
      </div>
    </div>
  </UCard>
</template>
```

- [ ] **Step 2: Rewrite `history.vue`**

```vue
<script setup lang="ts">
import type { HistoryEntry } from '~~/shared/types'

const { data, refresh, pending } = await useFetch<{ entries: HistoryEntry[] }>('/api/history?limit=50')
</script>

<template>
  <div class="space-y-4">
    <div class="flex items-center justify-between">
      <h2 class="text-xl font-semibold">History</h2>
      <UButton color="neutral" variant="soft" :loading="pending" label="refresh" @click="refresh()" />
    </div>

    <UCard v-if="!data?.entries?.length">
      <p class="text-muted">No completed jobs yet. Run a batch on the Add page and it'll show up here.</p>
    </UCard>

    <HistoryEntryCard v-for="entry in data?.entries ?? []" :key="entry.id" :entry="entry" />
  </div>
</template>
```

- [ ] **Step 3: Verify typecheck + build**

Run: `cd webapps/lidarr-bulk && pnpm typecheck && pnpm build`
Expected: both succeed.

- [ ] **Step 4: Commit**

```bash
git add webapps/lidarr-bulk/app/components/HistoryEntryCard.vue webapps/lidarr-bulk/app/pages/history.vue
git commit -m "refactor(lidarr-bulk): Nuxt UI history with HistoryEntryCard"
```

---

### Task 13: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Confirm no stale custom CSS classes remain**

Run: `cd webapps/lidarr-bulk && grep -rn 'class="panel\|class="tab\|class="status\|class="candidate\|class="muted\|class="kbd\|class="row\|class="item\|class="container\|nav.top' app/ || echo "clean"`
Expected: `clean` (all bespoke classes replaced).

- [ ] **Step 2: Full gate**

Run: `cd webapps/lidarr-bulk && pnpm typecheck && pnpm test && pnpm build`
Expected: typecheck clean; all vitest suites pass (incl. new `status-badge`); build succeeds.

- [ ] **Step 3: Manual smoke of every page**

Run: `cd webapps/lidarr-bulk && pnpm dev`
Confirm: login → add (artist/album), candidate pick/skip, AI generate + prompt builder + copy, history view/retry/replay, settings save, color-mode toggle, sign-out.

- [ ] **Step 4: Final commit (if any cleanup needed)**

```bash
git add -A webapps/lidarr-bulk
git commit -m "chore(lidarr-bulk): final Nuxt UI redesign cleanup" || echo "nothing to commit"
```

---

## Self-Review

**Spec coverage:** Foundation/theming → T1. Layout/nav/color-mode → T4. BulkAddForm → T7; AiDiscoverPanel → T8; JobMonitor → T6; CandidatePicker → T5; JobItemRow → T6; StatusBadge → T2+T3; HistoryEntryCard → T12. Pure status fn + test → T2. Form/feedback mapping (toasts, UAlert, UForm) → T7,T8,T10,T11,T12. History/Settings/Login → T10,T11,T12. Testing/verification gates → T13. CSS deletion → T1+T13. All spec sections covered.

**Placeholder scan:** No TBD/TODO; every code step shows complete code; `UInputNumber` fallback explicitly given.

**Type consistency:** `statusBadge`/`StatusBadge`/`BadgeColor` consistent across T2/T3/T6. `JobSnapshot`, `Candidate`, `ParsedItem`, `HistoryEntry`, `AppSettings`, `LidarrProfilesResponse`, `PromptFlavors` all match `shared/types.ts` / `shared/external-prompt.ts`. `useJob().start(kind, items, monitorMode, opts)` signature matches `composables/useJob.ts`. `BulkAddForm` emits `start(items, opts)` consumed by `index.vue` `onStart`. `v-model:blob` consistent across index/BulkAddForm/AiDiscoverPanel.
