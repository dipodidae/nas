<script setup lang="ts">
import type { AppSettings, LidarrProfilesResponse } from '~~/shared/types'

const { data: profiles, error: profilesErr } = await useFetch<LidarrProfilesResponse>(
  '/api/lidarr/profiles',
)
const { data: settings, refresh } = await useFetch<AppSettings>('/api/settings')
const saving = ref(false)
const saved = ref(false)
const errorMsg = ref('')

async function save(): Promise<void> {
  if (!settings.value)
    return
  saving.value = true
  errorMsg.value = ''
  saved.value = false
  try {
    await $fetch('/api/settings', { method: 'PUT', body: settings.value })
    saved.value = true
    await refresh()
  }
  catch (e) {
    errorMsg.value = (e as Error).message
  }
  finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="container">
    <h2>Settings</h2>
    <p class="muted">
      Defaults applied when adding artists or albums to Lidarr. Persisted to
      <span class="kbd">/config/settings.json</span> in the container.
    </p>

    <div v-if="profilesErr" class="panel">
      <strong style="color:var(--err)">Can't reach Lidarr.</strong>
      <p class="meta">{{ profilesErr.message }}</p>
    </div>

    <div v-else-if="profiles && settings" class="panel">
      <label>
        Root folder
        <select v-model="settings.rootFolderPath">
          <option v-for="r in profiles.rootFolders" :key="r.id" :value="r.path">
            {{ r.path }} ({{ r.accessible ? 'ok' : 'unreachable' }})
          </option>
        </select>
      </label>
      <label style="display:block; margin-top:12px">
        Quality profile
        <select v-model="settings.qualityProfileId">
          <option v-for="q in profiles.qualityProfiles" :key="q.id" :value="q.id">
            {{ q.name }}
          </option>
        </select>
      </label>
      <label style="display:block; margin-top:12px">
        Metadata profile
        <select v-model="settings.metadataProfileId">
          <option v-for="m in profiles.metadataProfiles" :key="m.id" :value="m.id">
            {{ m.name }}
          </option>
        </select>
      </label>
      <label style="display:block; margin-top:12px">
        Monitor mode (default for new adds)
        <select v-model="settings.monitorMode">
          <option value="all">all albums</option>
          <option value="future">future only</option>
        </select>
      </label>
      <div class="row" style="margin-top:16px">
        <button :disabled="saving" @click="save">
          {{ saving ? 'saving…' : 'save' }}
        </button>
        <span v-if="saved" class="meta" style="color:var(--ok)">saved</span>
        <span v-if="errorMsg" class="meta" style="color:var(--err)">{{ errorMsg }}</span>
      </div>
    </div>
  </div>
</template>
