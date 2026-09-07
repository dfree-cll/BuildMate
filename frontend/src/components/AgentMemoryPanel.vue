<template>
  <div class="memory-panel">
    <el-select :model-value="memory.sessionId.value" :disabled="disabled || memory.state.value.loading"
               placeholder="历史会话" class="memory-session-select" @change="select">
      <el-option :label="'当前会话 · ' + memory.sessionId.value.slice(0, 8)" :value="memory.sessionId.value" />
      <el-option v-for="session in memory.state.value.sessions.filter(s => s.session_id !== memory.sessionId.value)"
                 :key="session.session_id" :label="session.title" :value="session.session_id" />
    </el-select>
    <el-button :disabled="disabled || memory.state.value.loading" @click="newSession">新建会话</el-button>
    <el-button :disabled="disabled" :loading="memory.state.value.loading" @click="refresh">恢复 / 刷新记录</el-button>
    <el-button text @click="expanded = !expanded">{{ expanded ? '收起记忆' : '查看记忆与偏好' }}</el-button>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <div v-if="expanded" class="memory-detail">
      <p>仅当前账号、项目、Agent 和会话可用。历史结果不是本次证据，也不会代替审批；新会话不自动带入旧会话。</p>
      <el-input v-model="note" type="textarea" :maxlength="2000" placeholder="希望在此会话记住的偏好或背景（不能作为工程规格依据）" />
      <el-button :disabled="disabled" @click="save">保存偏好</el-button>
      <p v-if="memory.state.value.detail.summary">历史摘要：{{ memory.state.value.detail.summary }}</p>
      <details v-for="turn in memory.state.value.detail.turns" :key="turn.seq">
        <summary>{{ turn.seq }}. {{ turn.user_text.slice(0, 100) }}</summary>
        <pre>{{ turn.answer }}</pre>
      </details>
      <p v-if="!memory.state.value.detail.turns.length">此会话暂无完成记录。</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useAgentSession, type MemoryAgent, type MemoryDetail } from '../stores/agentMemory'
import { apiErrorMessage } from '../utils/apiError'
const props = defineProps<{ agent: MemoryAgent; disabled?: boolean }>()
const emit = defineEmits<{ restored: [detail: MemoryDetail] }>()
const memory = useAgentSession(props.agent)
const expanded = ref(false)
const error = ref('')
const note = ref('')
function restored() {
  note.value = memory.state.value.detail.preferences.note || ''
  emit('restored', memory.state.value.detail)
}
async function refresh() {
  error.value = ''
  const key = memory.key.value
  try { await memory.refresh(); if (key === memory.key.value) restored() }
  catch (e) { if (key === memory.key.value) error.value = apiErrorMessage(e, '读取记忆失败，请重试') }
}
async function select(id: string) {
  error.value = ''
  const key = memory.key.value
  try { await memory.refresh(id); if (key === memory.key.value && id === memory.sessionId.value) restored() }
  catch (e) { if (key === memory.key.value) error.value = apiErrorMessage(e, '恢复会话失败') }
}
function newSession() { memory.newSession(); restored() }
async function save() {
  const key = memory.key.value
  const sessionId = memory.sessionId.value
  try {
    await memory.savePreferences({ ...memory.state.value.detail.preferences, note: note.value })
    if (key === memory.key.value && sessionId === memory.sessionId.value) ElMessage.success('偏好已保存，仅影响当前会话')
  } catch (e) { if (key === memory.key.value && sessionId === memory.sessionId.value) error.value = apiErrorMessage(e, '保存记忆失败') }
}
watch(memory.key, () => {
  note.value = ''; error.value = ''; expanded.value = false
  emit('restored', { session_id: '', summary: '', preferences: {}, turns: [] })
  void refresh()
}, { immediate: true })
</script>

<style scoped>
.memory-panel { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 0 0 16px; padding: 12px; background: #f5f5f7; border-radius: 12px; }
.memory-session-select { width: 260px; max-width: 100%; }
.memory-panel > .el-button { margin: 0; }
.memory-detail { width: 100%; margin-top: 12px; color: #515154; font-size: 13px; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; }
details { margin: 10px 0; }
</style>
