<template>
  <el-card>
    <template #header>🤝 供应商谈判（多阶段状态机）</template>
    <AgentMemoryPanel agent="negotiation" :disabled="sending" @restored="restore" />
    <el-form inline>
      <el-form-item label="谈判标的"><el-input v-model="material" style="width:180px" /></el-form-item>
      <el-button type="primary" :disabled="sending" @click="start">开始新谈判</el-button>
    </el-form>
    <div class="chat-box">
      <div v-for="(m, i) in messages" :key="i" class="chat-row" :class="{ 'chat-row--user': m.role === 'user' }">
        <div class="chat-bubble" :class="{ 'chat-bubble--user': m.role === 'user' }">{{ m.content }}</div>
      </div>
    </div>
    <el-input v-model="input" maxlength="2000" :disabled="sending" placeholder="输入报价/方案/条件..."
              @compositionstart="onCompositionStart" @compositionend="onCompositionEnd"
              @keydown.enter="onEnter" @keyup.enter="onKeyup" />
    <el-button type="primary" style="margin-top:8px" :loading="sending"
               :disabled="sending || memory.state.value.loading || !input.trim()" @click="send">发送</el-button>
    <el-button type="warning" :disabled="sending" style="margin-top:8px; margin-left:8px" @click="genMinutes">📋 生成会议纪要</el-button>
  </el-card>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { negChat, negMinutes } from '../api'
import { apiErrorMessage } from '../utils/apiError'
import AgentMemoryPanel from '../components/AgentMemoryPanel.vue'
import { useAgentSession, type MemoryDetail } from '../stores/agentMemory'

const material = ref('塔吊 QTZ63')
const input = ref('')
const messages = ref<{ role: string; content: string }[]>([])
const memory = useAgentSession('negotiation')
const sending = ref(false)
const composing = ref(false)
let generation = 0
watch([memory.key, memory.sessionId], ([key], [previousKey]) => {
  ++generation
  messages.value = []
  if (key !== previousKey) material.value = ''
  sending.value = false
}, { flush: 'sync' })
function onCompositionStart() { composing.value = true }
function onCompositionEnd() { composing.value = false }
function onEnter(event: KeyboardEvent) {
  if (composing.value || event.isComposing) return
  event.preventDefault()
  void send()
}
function onKeyup(event: KeyboardEvent) {
  if (event.key !== 'Enter' || composing.value || event.isComposing) return
  void send()
}
function restore(detail: MemoryDetail) {
  if (sending.value) return
  messages.value = detail.turns.flatMap(t => [{ role: 'user', content: t.user_text }, { role: 'assistant', content: t.answer }])
  const last = detail.turns[detail.turns.length - 1]
  if (typeof last?.result.material === 'string') material.value = last.result.material
}

async function start() {
  if (sending.value) return
  memory.newSession()
  messages.value = []
  await sendMessage('开始谈判', true)
}
async function send() {
  const msg = input.value.trim()
  if (!msg || sending.value || memory.state.value.loading) return
  const requestGeneration = generation + 1
  input.value = ''
  messages.value.push({ role: 'user', content: msg })
  const accepted = await sendMessage(msg)
  if (!accepted && requestGeneration === generation && !input.value.trim()) input.value = msg
}
async function sendMessage(message: string, reset = false) {
  sending.value = true
  const requestGeneration = ++generation
  let accepted = false
  try {
    const { data: j } = await negChat({ message, material: material.value, session_id: memory.sessionId.value,
                                      project_id: memory.projectId.value, reset })
    if (requestGeneration === generation) {
      messages.value.push({ role: 'assistant', content: '【阶段：' + j.stage + '】\n' + j.reply })
      accepted = true
    }
  } catch (e) { if (requestGeneration === generation) ElMessage.error(apiErrorMessage(e, '谈判请求失败')) }
  finally { if (requestGeneration === generation) sending.value = false }
  return accepted
}
async function genMinutes() {
  if (sending.value) return
  const requestGeneration = ++generation
  sending.value = true
  try {
    const r = await negMinutes({ message: '', material: material.value, session_id: memory.sessionId.value,
                                 project_id: memory.projectId.value })
    const j = r.data
    if (requestGeneration !== generation) return
    if (j.minutes) messages.value.push({ role: 'assistant', content: j.reply })
    else ElMessage.warning(j.reply)
  } catch (e) { if (requestGeneration === generation) ElMessage.error(apiErrorMessage(e, '会议纪要生成失败')) }
  finally { if (requestGeneration === generation) sending.value = false }
}
</script>
