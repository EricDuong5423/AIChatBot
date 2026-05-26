<template>
  <div class="bg-white rounded-lg shadow-sm border flex flex-col" style="min-height: 620px">
    <div class="px-5 py-3 border-b flex justify-between items-center">
      <h2 class="font-semibold text-gray-800">Knowledge Base</h2>
      <button
        @click="rebuild"
        :disabled="rebuilding"
        class="text-sm px-3 py-1.5 rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:bg-gray-300 disabled:cursor-not-allowed transition"
      >
        {{ rebuilding ? 'Đang re-index...' : 'Re-index ngay' }}
      </button>
    </div>

    <div class="px-5 py-4 space-y-4">
      <!-- URL crawl -->
      <div class="border rounded-lg p-3 bg-blue-50/50">
        <div class="text-sm font-medium text-gray-700 mb-2">Học từ URL</div>
        <div class="flex gap-2">
          <input
            v-model="urlInput"
            type="url"
            placeholder="https://example.com/bai-viet"
            :disabled="crawling"
            @keydown.enter="crawl"
            class="flex-1 border rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-gray-100"
          />
          <button
            @click="crawl"
            :disabled="crawling || !urlInput.trim()"
            class="px-3 py-1.5 rounded-md bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:bg-gray-300 disabled:cursor-not-allowed"
          >
            {{ crawling ? 'Đang crawl...' : 'Crawl' }}
          </button>
        </div>
        <p class="text-xs text-gray-400 mt-1.5">
          Chỉ hoạt động với static HTML page. SPA (JS render) sẽ báo lỗi → dùng upload .md.
        </p>
      </div>

      <!-- Upload zone -->
      <div
        @drop.prevent="onDrop"
        @dragover.prevent
        @dragenter.prevent
        class="border-2 border-dashed border-gray-300 rounded-lg p-6 text-center hover:border-blue-400 transition"
      >
        <input
          ref="fileInput"
          type="file"
          multiple
          accept=".md,.txt,.pdf"
          @change="onPick"
          class="hidden"
        />
        <p class="text-sm text-gray-600 mb-2">
          Kéo-thả file vào đây, hoặc
          <button
            @click="$refs.fileInput.click()"
            class="text-blue-600 hover:underline font-medium"
          >chọn file</button>
        </p>
        <p class="text-xs text-gray-400">Chấp nhận: .md, .txt, .pdf — tối đa 20 MB/file</p>
      </div>

      <!-- Upload progress -->
      <div v-if="uploading.length" class="space-y-1.5">
        <div
          v-for="u in uploading"
          :key="u.name"
          class="text-xs px-3 py-1.5 rounded-md bg-gray-50 border flex justify-between items-center"
        >
          <span class="font-mono">{{ u.name }}</span>
          <span :class="u.status === 'error' ? 'text-red-500' : 'text-blue-600'">
            {{ u.status === 'uploading' ? 'Đang upload...' : (u.status === 'done' ? '✓ OK' : '✗ ' + u.error) }}
          </span>
        </div>
      </div>

      <!-- Error message -->
      <div v-if="error" class="text-sm px-3 py-2 rounded-md bg-red-50 text-red-700 border border-red-200">
        {{ error }}
      </div>

      <!-- File list -->
      <div>
        <div class="text-xs text-gray-500 mb-2 px-1">
          {{ files.length }} file đã upload
        </div>
        <p v-if="!files.length" class="text-sm text-center text-gray-400 py-8">
          Chưa có file. Upload file đầu tiên ở trên.
        </p>
        <ul v-else class="divide-y rounded-lg border overflow-hidden">
          <li
            v-for="f in files"
            :key="f.filename"
            class="px-4 py-2 flex justify-between items-center hover:bg-gray-50"
          >
            <div class="min-w-0 flex-1">
              <div class="text-sm font-mono truncate">{{ f.filename }}</div>
              <div class="text-xs text-gray-400">
                {{ humanSize(f.size_bytes) }} · {{ formatDate(f.modified_at) }}
              </div>
            </div>
            <button
              @click="del(f.filename)"
              class="ml-3 text-xs text-red-500 hover:text-red-700 px-2 py-1"
            >
              Xóa
            </button>
          </li>
        </ul>
      </div>

      <!-- Rebuild status -->
      <div v-if="rebuildStatus" class="text-xs px-3 py-2 rounded-md bg-gray-50 border text-gray-600">
        <div v-if="rebuildStatus.running">⏳ Đang index lại...</div>
        <div v-else-if="rebuildStatus.last_error" class="text-red-600">
          ✗ Lần re-index gần nhất lỗi: {{ rebuildStatus.last_error }}
        </div>
        <div v-else-if="rebuildStatus.last_at">
          ✓ Re-index gần nhất: {{ rebuildStatus.last_chunks }} chunks lúc {{ formatDate(rebuildStatus.last_at) }}
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'

const props = defineProps({
  apiKey: String,
})

const files = ref([])
const uploading = ref([])
const error = ref('')
const rebuilding = ref(false)
const rebuildStatus = ref(null)
const urlInput = ref('')
const crawling = ref(false)
let pollTimer = null

const authHeaders = () => (props.apiKey ? { 'X-API-Key': props.apiKey } : {})

async function loadList() {
  try {
    const r = await fetch('/docs')
    if (!r.ok) throw new Error('HTTP ' + r.status)
    files.value = await r.json()
  } catch (e) {
    error.value = 'Không load được danh sách: ' + e.message
  }
}

async function loadStatus() {
  try {
    const r = await fetch('/docs/rebuild/status')
    if (r.ok) {
      rebuildStatus.value = await r.json()
      rebuilding.value = rebuildStatus.value.running
    }
  } catch {}
}

function onPick(ev) {
  const files = Array.from(ev.target.files || [])
  if (files.length) uploadAll(files)
  ev.target.value = ''
}

function onDrop(ev) {
  const files = Array.from(ev.dataTransfer.files || [])
  if (files.length) uploadAll(files)
}

async function uploadAll(fileList) {
  error.value = ''
  for (const f of fileList) {
    const slot = { name: f.name, status: 'uploading', error: '' }
    uploading.value.push(slot)
    try {
      const fd = new FormData()
      fd.append('file', f)
      // Skip rebuild giữa các file; chỉ rebuild ở file cuối
      const isLast = f === fileList[fileList.length - 1]
      const url = `/docs/upload?rebuild=${isLast ? 'true' : 'false'}`
      const r = await fetch(url, { method: 'POST', headers: authHeaders(), body: fd })
      if (!r.ok) {
        const detail = (await r.json().catch(() => ({}))).detail || r.statusText
        throw new Error(detail)
      }
      slot.status = 'done'
    } catch (e) {
      slot.status = 'error'
      slot.error = e.message
    }
  }
  await loadList()
  await loadStatus()
  // Clear uploaded list after 3s
  setTimeout(() => { uploading.value = uploading.value.filter(u => u.status === 'error') }, 3000)
}

async function crawl() {
  const url = urlInput.value.trim()
  if (!url) return
  if (!/^https?:\/\//i.test(url)) {
    error.value = 'URL phải bắt đầu bằng http:// hoặc https://'
    return
  }
  crawling.value = true
  error.value = ''
  try {
    const r = await fetch('/docs/crawl-url', {
      method: 'POST',
      headers: { ...authHeaders(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, rebuild: true }),
    })
    if (!r.ok) {
      const detail = (await r.json().catch(() => ({}))).detail || r.statusText
      throw new Error(detail)
    }
    const data = await r.json()
    urlInput.value = ''
    // Hiển thị thành công như upload
    uploading.value.push({ name: data.filename + ` (${data.extracted_chars} chars)`, status: 'done', error: '' })
    setTimeout(() => { uploading.value = uploading.value.filter(u => u.status === 'error') }, 3000)
    await loadList()
    await loadStatus()
  } catch (e) {
    error.value = 'Crawl lỗi: ' + e.message
  } finally {
    crawling.value = false
  }
}

async function del(filename) {
  if (!confirm(`Xóa "${filename}"?`)) return
  try {
    const r = await fetch(`/docs/${encodeURIComponent(filename)}`, {
      method: 'DELETE',
      headers: authHeaders(),
    })
    if (!r.ok) {
      const detail = (await r.json().catch(() => ({}))).detail || r.statusText
      throw new Error(detail)
    }
    await loadList()
    await loadStatus()
  } catch (e) {
    error.value = 'Xóa lỗi: ' + e.message
  }
}

async function rebuild() {
  rebuilding.value = true
  error.value = ''
  try {
    const r = await fetch('/docs/rebuild', { method: 'POST', headers: authHeaders() })
    if (!r.ok) {
      const detail = (await r.json().catch(() => ({}))).detail || r.statusText
      throw new Error(detail)
    }
    await loadStatus()
  } catch (e) {
    error.value = 'Re-index lỗi: ' + e.message
    rebuilding.value = false
  }
}

function humanSize(b) {
  if (b < 1024) return b + ' B'
  if (b < 1024 * 1024) return (b / 1024).toFixed(1) + ' KB'
  return (b / 1024 / 1024).toFixed(2) + ' MB'
}

function formatDate(iso) {
  if (!iso) return ''
  return new Date(iso).toLocaleString('vi-VN', { hour12: false })
}

onMounted(() => {
  loadList()
  loadStatus()
  pollTimer = setInterval(loadStatus, 3000)
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})
</script>
