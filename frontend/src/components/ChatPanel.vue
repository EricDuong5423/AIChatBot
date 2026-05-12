<template>
  <div class="bg-white rounded-lg shadow-sm border flex flex-col" style="height: 620px">
    <!-- Header -->
    <div class="px-5 py-3 border-b flex justify-between items-center">
      <h2 class="font-semibold text-gray-800">Chat Test</h2>
      <button @click="clear" class="text-sm text-gray-400 hover:text-red-500 transition-colors">
        Xóa lịch sử
      </button>
    </div>

    <!-- Messages -->
    <div ref="messagesEl" class="flex-1 overflow-y-auto px-5 py-4 space-y-3">
      <p v-if="!messages.length" class="text-center text-sm text-gray-400 mt-10">
        Bắt đầu chat với HCMUT Assistant...
      </p>

      <template v-for="(msg, i) in messages" :key="i">
        <!-- User -->
        <div v-if="msg.role === 'user'" class="flex justify-end">
          <div class="bg-blue-600 text-white rounded-2xl rounded-tr-sm px-4 py-2 max-w-sm text-sm leading-relaxed">
            {{ msg.content }}
          </div>
        </div>

        <!-- Navigation event -->
        <div v-else-if="msg.isNav" class="flex justify-start">
          <div class="bg-amber-50 border border-amber-200 text-amber-800 rounded-2xl rounded-tl-sm px-4 py-2 max-w-sm text-sm">
            <span class="font-semibold">Điều hướng →</span> {{ msg.content }}
          </div>
        </div>

        <!-- Bot text -->
        <div v-else class="flex justify-start">
          <div class="bg-gray-100 text-gray-800 rounded-2xl rounded-tl-sm px-4 py-2 max-w-md text-sm leading-relaxed whitespace-pre-wrap">
            {{ msg.content }}
          </div>
        </div>
      </template>

      <!-- Typing indicator -->
      <div v-if="loading" class="flex justify-start">
        <div class="bg-gray-100 rounded-2xl rounded-tl-sm px-4 py-2 text-sm text-gray-400 italic">
          Đang trả lời...
        </div>
      </div>
    </div>

    <!-- Input -->
    <div class="px-5 py-3 border-t flex gap-2">
      <input
        v-model="input"
        type="text"
        placeholder="Nhập tin nhắn..."
        :disabled="loading"
        @keydown.enter="send"
        class="flex-1 border rounded-xl px-4 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-gray-50"
      />
      <button
        @click="send"
        :disabled="loading || !input.trim()"
        class="bg-blue-600 text-white px-5 py-2 rounded-xl text-sm font-medium hover:bg-blue-700 disabled:opacity-40 transition-colors"
      >
        Gửi
      </button>
    </div>
  </div>
</template>

<script setup>
import { ref, nextTick } from 'vue'

const props = defineProps({ apiKey: String })

const messages = ref([])
const input = ref('')
const loading = ref(false)
const messagesEl = ref(null)
const history = ref([])

async function send() {
  const msg = input.value.trim()
  if (!msg || loading.value) return
  input.value = ''

  messages.value.push({ role: 'user', content: msg })
  history.value.push({ role: 'user', content: msg })
  loading.value = true
  await scrollDown()

  try {
    const headers = { 'Content-Type': 'application/json' }
    if (props.apiKey) headers['X-API-Key'] = props.apiKey

    const res = await fetch('/chat', {
      method: 'POST',
      headers,
      body: JSON.stringify({ message: msg, history: history.value.slice(0, -1) }),
    })

    const data = await res.json()

    if (!res.ok) {
      messages.value.push({
        role: 'assistant',
        content: `Lỗi ${res.status}: ${data.detail || res.statusText}`,
      })
      history.value.pop()
      return
    }

    if (data.type === 'navigation') {
      const dest = data.content?.destination_building ?? JSON.stringify(data.content)
      messages.value.push({ role: 'assistant', content: dest, isNav: true })
      history.value.push({ role: 'assistant', content: `[Navigation: ${dest}]` })
    } else {
      messages.value.push({ role: 'assistant', content: data.content })
      history.value.push({ role: 'assistant', content: data.content })
    }
  } catch {
    messages.value.push({ role: 'assistant', content: 'Không kết nối được tới server.' })
    history.value.pop()
  } finally {
    loading.value = false
    await scrollDown()
  }
}

function clear() {
  messages.value = []
  history.value = []
}

async function scrollDown() {
  await nextTick()
  if (messagesEl.value) messagesEl.value.scrollTop = messagesEl.value.scrollHeight
}
</script>
