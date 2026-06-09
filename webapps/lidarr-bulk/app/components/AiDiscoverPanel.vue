<script setup lang="ts">
import type { PromptFlavors } from '~~/shared/external-prompt'
import type { ParsedItem } from '~~/shared/types'
import { buildExternalPrompt, PROMPT_FLAVORS } from '~~/shared/external-prompt'

const emit = defineEmits<{ 'update:blob': [string] }>()
const toast = useToast()

// Fetched once so we only show the Generate button when the server has an
// OpenAI key configured.
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
    // Drop the result into the album textarea for review before adding.
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

// External-prompt builder: compose a copy-paste prompt for an outside LLM to do
// a deeper dive on the same spec. Pure client-side string work — works even
// when the in-app OpenAI integration is disabled.
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
