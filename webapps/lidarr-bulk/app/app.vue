<script setup lang="ts">
interface Me { authRequired: boolean, user: { name: string } | null }

const route = useRoute()
const me = ref<Me | null>(null)

async function loadMe(): Promise<void> {
  try {
    me.value = await $fetch<Me>('/api/me')
  }
  catch {
    me.value = null
  }
}

// Re-check on every route change so login/logout flips the chrome immediately.
watch(() => route.path, loadMe, { immediate: true })

async function logout(): Promise<void> {
  await $fetch('/api/logout', { method: 'POST' })
  await loadMe()
  await navigateTo('/login', { replace: true })
}
</script>

<template>
  <div>
    <nav v-if="route.path !== '/login'" class="top">
      <NuxtLink to="/" :class="{ active: route.path === '/' }">
        lidarr-bulk
      </NuxtLink>
      <NuxtLink to="/history" :class="{ active: route.path === '/history' }">
        history
      </NuxtLink>
      <NuxtLink to="/settings" :class="{ active: route.path === '/settings' }">
        settings
      </NuxtLink>
      <span v-if="me?.user" style="margin-left:auto" class="muted">
        {{ me.user.name }}
        <button class="secondary" style="margin-left:8px" @click="logout">
          sign out
        </button>
      </span>
    </nav>
    <NuxtPage />
  </div>
</template>
