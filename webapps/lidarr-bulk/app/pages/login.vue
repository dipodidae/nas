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
    // Hard navigation so all in-memory state (including any stale auth caches)
    // is rebuilt fresh against the new session cookie.
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
        <h2 class="text-lg font-semibold">
          lidarr-bulk
        </h2>
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
