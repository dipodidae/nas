<script setup lang="ts">
definePageMeta({ layout: false })

const route = useRoute()
const username = ref('')
const password = ref('')
const error = ref('')
const submitting = ref(false)

async function submit(): Promise<void> {
  submitting.value = true
  error.value = ''
  try {
    await $fetch('/api/login', {
      method: 'POST',
      body: { username: username.value, password: password.value },
    })
    const next = typeof route.query.next === 'string' ? route.query.next : '/'
    // Hard navigation so all in-memory state (including any stale auth caches)
    // is rebuilt fresh against the new session cookie.
    window.location.assign(next)
  }
  catch (e) {
    const err = e as {
      statusMessage?: string
      message?: string
      data?: { statusMessage?: string, message?: string }
    }
    const msg = err.data?.statusMessage
      ?? err.data?.message
      ?? err.statusMessage
      ?? err.message
      ?? 'login failed'
    error.value = msg
    console.error('[login] failed:', e)
  }
  finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="container" style="max-width:400px; padding-top:80px">
    <h2 style="margin-top:0">
      lidarr-bulk
    </h2>
    <form class="panel" @submit.prevent="submit">
      <label>
        Username
        <input v-model="username" autocomplete="username" autofocus>
      </label>
      <label style="display:block; margin-top:12px">
        Password
        <input v-model="password" type="password" autocomplete="current-password">
      </label>
      <div class="row" style="margin-top:16px">
        <button :disabled="submitting || !username || !password">
          {{ submitting ? 'signing in…' : 'sign in' }}
        </button>
        <span v-if="error" class="meta" style="color:var(--err)">{{ error }}</span>
      </div>
    </form>
  </div>
</template>
