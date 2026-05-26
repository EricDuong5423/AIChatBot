<template>
  <div class="min-h-screen bg-gray-50">
    <header class="bg-blue-700 text-white px-6 py-4 shadow-md">
      <h1 class="text-xl font-bold">HCMUT Chatbot Dashboard</h1>
    </header>

    <div class="max-w-5xl mx-auto px-4 py-6">
      <!-- API Key -->
      <div class="bg-white rounded-lg shadow-sm border p-4 mb-5 flex items-center gap-3">
        <span class="text-sm font-medium text-gray-600 whitespace-nowrap">API Key</span>
        <input
          v-model="apiKey"
          type="password"
          placeholder="X-API-Key (để trống nếu không cần auth)"
          class="flex-1 border rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
      </div>

      <!-- Tabs -->
      <div class="flex gap-1 mb-5 bg-gray-200 rounded-lg p-1 w-fit">
        <button
          v-for="tab in tabs"
          :key="tab.id"
          @click="activeTab = tab.id"
          :class="
            activeTab === tab.id
              ? 'bg-white shadow text-blue-700'
              : 'text-gray-600 hover:text-gray-800'
          "
          class="px-4 py-2 rounded-md text-sm font-medium transition-all"
        >
          {{ tab.label }}
        </button>
      </div>

      <ChatPanel v-if="activeTab === 'chat'" :api-key="apiKey" />
      <DocsPanel v-if="activeTab === 'docs'" :api-key="apiKey" />
    </div>
  </div>
</template>

<script setup>
import { ref, watch } from 'vue'
import ChatPanel from './components/ChatPanel.vue'
import DocsPanel from './components/DocsPanel.vue'

const STORAGE_KEY = 'hcmut_api_key'
const apiKey = ref(localStorage.getItem(STORAGE_KEY) ?? '')
const activeTab = ref('chat')
const tabs = [
  { id: 'chat', label: 'Chat Test' },
  { id: 'docs', label: 'Knowledge Base' },
]

watch(apiKey, (val) => {
  if (val) localStorage.setItem(STORAGE_KEY, val)
  else localStorage.removeItem(STORAGE_KEY)
})
</script>
