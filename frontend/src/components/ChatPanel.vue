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

        <!-- Bot text (markdown rendered) -->
        <div v-else class="flex justify-start">
          <div
            class="bg-gray-100 text-gray-800 rounded-2xl rounded-tl-sm px-4 py-2 max-w-md text-sm leading-relaxed md-bubble"
            v-html="renderMd(msg.content)"
          ></div>
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
import { marked } from 'marked'
import DOMPurify from 'dompurify'

// Markdown options: GFM + tự convert newline thành <br>
marked.use({ gfm: true, breaks: true })

function renderMd(text) {
  if (!text) return ''
  const html = marked.parse(text)
  // Sanitize HTML — chặn XSS từ AI output (links với javascript:, script tags, etc.)
  return DOMPurify.sanitize(html, {
    ALLOWED_TAGS: ['p', 'br', 'strong', 'em', 'b', 'i', 'u', 's', 'code', 'pre',
      'ul', 'ol', 'li', 'a', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
      'blockquote', 'hr', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'span', 'del'],
    ALLOWED_ATTR: ['href', 'target', 'rel', 'title'],
    ALLOW_DATA_ATTR: false,
  })
}

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

<style scoped>
/* Style cho markdown bubble — ghi đè reset Tailwind cho text bot */
.md-bubble :deep(p) { margin: 0 0 0.5em 0; }
.md-bubble :deep(p:last-child) { margin-bottom: 0; }
.md-bubble :deep(ul),
.md-bubble :deep(ol) { padding-left: 1.25rem; margin: 0.25rem 0; }
.md-bubble :deep(ul) { list-style: disc; }
.md-bubble :deep(ol) { list-style: decimal; }
.md-bubble :deep(li) { margin: 0.15rem 0; }
.md-bubble :deep(strong) { font-weight: 600; }
.md-bubble :deep(em) { font-style: italic; }
.md-bubble :deep(a) { color: #2563eb; text-decoration: underline; }
.md-bubble :deep(code) {
  background: rgba(0, 0, 0, 0.06);
  padding: 0.1em 0.3em;
  border-radius: 0.25rem;
  font-family: 'SF Mono', Menlo, Consolas, monospace;
  font-size: 0.85em;
}
.md-bubble :deep(pre) {
  background: rgba(0, 0, 0, 0.05);
  padding: 0.5rem 0.75rem;
  border-radius: 0.5rem;
  overflow-x: auto;
  margin: 0.5rem 0;
}
.md-bubble :deep(pre code) { background: transparent; padding: 0; }
.md-bubble :deep(h1),
.md-bubble :deep(h2),
.md-bubble :deep(h3) { font-weight: 600; margin: 0.5rem 0 0.25rem; }
.md-bubble :deep(h1) { font-size: 1.05em; }
.md-bubble :deep(h2) { font-size: 1.0em; }
.md-bubble :deep(h3) { font-size: 0.95em; }
.md-bubble :deep(blockquote) {
  border-left: 3px solid #cbd5e1;
  padding-left: 0.6rem;
  color: #475569;
  margin: 0.4rem 0;
}
.md-bubble :deep(table) {
  border-collapse: collapse;
  margin: 0.4rem 0;
  font-size: 0.9em;
}
.md-bubble :deep(th),
.md-bubble :deep(td) {
  border: 1px solid #d1d5db;
  padding: 0.2rem 0.5rem;
}
.md-bubble :deep(hr) { border: 0; border-top: 1px solid #e5e7eb; margin: 0.5rem 0; }
</style>
