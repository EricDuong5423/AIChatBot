<template>
  <div class="bg-white rounded-lg shadow-sm border">
    <!-- Header -->
    <div class="px-5 py-3 border-b flex justify-between items-center">
      <h2 class="font-semibold text-gray-800">Quản lý Buildings</h2>
      <button
        @click="openAdd"
        class="bg-blue-600 text-white px-4 py-1.5 rounded-lg text-sm font-medium hover:bg-blue-700 transition-colors"
      >
        + Thêm tòa nhà
      </button>
    </div>

    <!-- Table -->
    <div class="p-5">
      <div v-if="loading" class="text-center py-16 text-gray-400 text-sm">Đang tải...</div>

      <div v-else-if="error" class="text-center py-16 text-red-400 text-sm">{{ error }}</div>

      <div v-else-if="!buildings.length" class="text-center py-16 text-gray-400 text-sm">
        Chưa có tòa nhà nào. Nhấn <strong>+ Thêm tòa nhà</strong> để bắt đầu.
      </div>

      <table v-else class="w-full text-sm">
        <thead>
          <tr class="text-left text-gray-500 border-b">
            <th class="pb-3 font-medium pr-4">Key</th>
            <th class="pb-3 font-medium pr-4">Tên</th>
            <th class="pb-3 font-medium pr-4">Khoa</th>
            <th class="pb-3 font-medium pr-4 text-center">Tầng</th>
            <th class="pb-3 font-medium">Dịch vụ</th>
            <th class="pb-3 font-medium text-right">Thao tác</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="b in buildings"
            :key="b.key"
            class="border-b last:border-0 hover:bg-gray-50 transition-colors"
          >
            <td class="py-3 pr-4 font-mono font-semibold text-blue-700">{{ b.key }}</td>
            <td class="py-3 pr-4 font-medium">{{ b.ten }}</td>
            <td class="py-3 pr-4 text-gray-500">{{ b.khoa || '—' }}</td>
            <td class="py-3 pr-4 text-center text-gray-500">{{ b.tang ?? '—' }}</td>
            <td class="py-3 text-gray-500 max-w-xs truncate">
              {{ (b.dich_vu || []).join(', ') || '—' }}
            </td>
            <td class="py-3 text-right whitespace-nowrap">
              <button
                @click="openEdit(b)"
                class="text-blue-600 hover:underline text-sm mr-4"
              >
                Sửa
              </button>
              <button
                @click="remove(b.key)"
                class="text-red-500 hover:underline text-sm"
              >
                Xóa
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>

  <BuildingModal
    v-if="showModal"
    :data="editing"
    :api-key="props.apiKey"
    @close="showModal = false"
    @saved="onSaved"
  />
</template>

<script setup>
import { ref, onMounted } from 'vue'
import BuildingModal from './BuildingModal.vue'

const props = defineProps({ apiKey: String })

const buildings = ref([])
const loading = ref(false)
const error = ref('')
const showModal = ref(false)
const editing = ref(null)

onMounted(load)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await fetch('/buildings')
    if (!res.ok) throw new Error(res.statusText)
    buildings.value = await res.json()
  } catch (e) {
    error.value = 'Không thể tải danh sách tòa nhà.'
  } finally {
    loading.value = false
  }
}

function openAdd() {
  editing.value = null
  showModal.value = true
}

function openEdit(b) {
  editing.value = { ...b }
  showModal.value = true
}

async function remove(key) {
  if (!confirm(`Xóa tòa nhà "${key}"?`)) return
  const headers = {}
  if (props.apiKey) headers['X-API-Key'] = props.apiKey
  const res = await fetch(`/buildings/${key}`, { method: 'DELETE', headers })
  if (res.ok) {
    load()
  } else {
    alert('Xóa thất bại. Kiểm tra lại API Key.')
  }
}

function onSaved() {
  showModal.value = false
  load()
}
</script>
