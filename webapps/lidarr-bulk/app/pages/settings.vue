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
      <h2 class="text-xl font-semibold">
        Settings
      </h2>
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
