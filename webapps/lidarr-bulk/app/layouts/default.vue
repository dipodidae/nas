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
// Re-check on every route change so login/logout flips the chrome immediately.
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
