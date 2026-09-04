<script setup lang="ts">
// No account menu and no sign-out: this app has no login of its own. The
// session that gets you here belongs to the one tinyauth door at SWAG, and
// signing out of it is done at auth.<domain>, not from a per-app header.
// ADR-0034.
const colorMode = useColorMode()

const navItems = computed(() => [
  { label: 'Add', icon: 'i-lucide-plus', to: '/' },
  { label: 'History', icon: 'i-lucide-history', to: '/history' },
  { label: 'Settings', icon: 'i-lucide-settings', to: '/settings' },
])

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
        </div>
      </UContainer>
    </header>
    <UContainer class="py-6">
      <slot />
    </UContainer>
  </div>
</template>
