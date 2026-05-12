<template>
  <div
    class="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4"
    @click.self="$emit('close')"
  >
    <div class="bg-white rounded-xl shadow-xl w-full max-w-lg">
      <div class="px-6 py-4 border-b">
        <h3 class="text-base font-semibold text-gray-900">
          {{ data ? 'Sửa tòa nhà' : 'Thêm tòa nhà' }}
        </h3>
      </div>

      <form @submit.prevent="submit" class="px-6 py-4 space-y-4">
        <div class="grid grid-cols-2 gap-4">
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Key *</label>
            <input
              v-model="form.key"
              required
              :disabled="!!data"
              placeholder="VD: A4"
              class="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-gray-100 disabled:text-gray-500"
            />
          </div>
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Tên tòa nhà *</label>
            <input
              v-model="form.ten"
              required
              placeholder="VD: Tòa nhà A4"
              class="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
        </div>

        <div>
          <label class="block text-sm font-medium text-gray-700 mb-1">Mô tả *</label>
          <textarea
            v-model="form.mo_ta"
            required
            rows="3"
            placeholder="Mô tả chi tiết về tòa nhà..."
            class="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none"
          />
        </div>

        <div class="grid grid-cols-2 gap-4">
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Khoa</label>
            <input
              v-model="form.khoa"
              placeholder="VD: Khoa Điện-Điện tử"
              class="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Số tầng</label>
            <input
              v-model.number="form.tang"
              type="number"
              min="1"
              placeholder="8"
              class="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
        </div>

        <div>
          <label class="block text-sm font-medium text-gray-700 mb-1">
            Dịch vụ
            <span class="font-normal text-gray-400">(mỗi dịch vụ một dòng)</span>
          </label>
          <textarea
            v-model="dichVuText"
            rows="3"
            placeholder="Phòng học&#10;Phòng thí nghiệm&#10;Căng tin"
            class="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none"
          />
        </div>

        <div v-if="error" class="text-sm text-red-600 bg-red-50 rounded-lg px-3 py-2">
          {{ error }}
        </div>

        <div class="flex gap-3 justify-end pt-1 pb-2">
          <button
            type="button"
            @click="$emit('close')"
            class="px-4 py-2 text-sm text-gray-600 hover:text-gray-900 transition-colors"
          >
            Hủy
          </button>
          <button
            type="submit"
            :disabled="saving"
            class="bg-blue-600 text-white px-5 py-2 rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50 transition-colors"
          >
            {{ saving ? 'Đang lưu...' : 'Lưu' }}
          </button>
        </div>
      </form>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive } from 'vue'

const props = defineProps({ data: Object, apiKey: String })
const emit = defineEmits(['close', 'saved'])

const form = reactive({
  key: props.data?.key ?? '',
  ten: props.data?.ten ?? '',
  mo_ta: props.data?.mo_ta ?? '',
  khoa: props.data?.khoa ?? '',
  tang: props.data?.tang ?? null,
})
const dichVuText = ref((props.data?.dich_vu ?? []).join('\n'))
const error = ref('')
const saving = ref(false)

async function submit() {
  error.value = ''
  saving.value = true

  const payload = {
    ...form,
    tang: form.tang || null,
    dich_vu: dichVuText.value
      .split('\n')
      .map((s) => s.trim())
      .filter(Boolean),
  }

  const isEdit = !!props.data
  const url = isEdit ? `/buildings/${props.data.key}` : '/buildings'
  const headers = { 'Content-Type': 'application/json' }
  if (props.apiKey) headers['X-API-Key'] = props.apiKey

  try {
    const res = await fetch(url, {
      method: isEdit ? 'PUT' : 'POST',
      headers,
      body: JSON.stringify(payload),
    })
    const result = await res.json()
    if (!res.ok) {
      error.value = result.detail ?? 'Lỗi không xác định'
      return
    }
    emit('saved')
  } catch {
    error.value = 'Không kết nối được tới server.'
  } finally {
    saving.value = false
  }
}
</script>
