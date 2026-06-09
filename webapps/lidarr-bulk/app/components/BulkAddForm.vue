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

// Profile pickers populated lazily — only when the user opens "Advanced" — so
// the page loads without hitting Lidarr.
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
  if (submitting.value)
    return 'starting…'
  if (props.jobInFlight)
    return 'wait for current batch…'
  if (dryRun.value)
    return 'Preview only'
  return 'Add all'
})

const monitorItems = [
  { label: 'all albums', value: 'all' },
  { label: 'future only', value: 'future' },
]
const qualityItems = computed(() => [
  { label: '(default)', value: null as number | null },
  ...(profiles.value?.qualityProfiles ?? []).map(p => ({ label: p.name, value: p.id as number | null })),
])
const metadataItems = computed(() => [
  { label: '(default)', value: null as number | null },
  ...(profiles.value?.metadataProfiles ?? []).map(p => ({ label: p.name, value: p.id as number | null })),
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
