<template>
  <el-card>
    <template #header>💬 智能对话（RAG + 真实 LLM）</template>
    <div style="height:480px; overflow-y:auto; border:1px solid #eee; border-radius:8px; padding:16px; margin-bottom:12px" ref="chatBox">
      <div v-for="(m, i) in messages" :key="i" style="margin-bottom:12px; display:flex; justify-content: m.role==='user'?'flex-end':'flex-start'">
        <div :style="{maxWidth:'80%', padding:'10px 14px', borderRadius:'10px', background: m.role==='user'?'#1a237e':'#f5f5f5', color: m.role==='user'?'#fff':'#333', whiteSpace:'pre-wrap'}">{{ m.content }}</div>
      </div>
    </div>
    <el-input v-model="input" placeholder="输入问题，如：西安螺纹钢多少钱 / GB50010 抗震等级" @keyup.enter="send" />
    <el-button type="primary" style="margin-top:8px" :loading="sending" @click="send">发送</el-button>
  </el-card>
</template>

<script setup lang="ts">
import { ref, nextTick, onBeforeUnmount } from 'vue'
import { chatStream } from '../api'

const messages = ref<any[]>([])
const input = ref('')
const sending = ref(false)
const chatBox = ref<any>(null)
const sessionId = 'fe-qa-' + Date.now()
let streamAbort: AbortController | null = null   // M8：切页时中断进行中的 SSE

onBeforeUnmount(() => {
  streamAbort?.abort()
})

async function send() {
  const msg = input.value.trim()
  if (!msg || sending.value) return
  input.value = ''
  messages.value.push({ role: 'user', content: msg })
  const thinking = { role: 'assistant', content: '思考中...' }
  messages.value.push(thinking)
  sending.value = true
  streamAbort = new AbortController()
  try {
    await chatStream(sessionId, msg, (e) => {
      if (e.type === 'token') {
        thinking.content = thinking.content === '思考中...' ? e.content : thinking.content + e.content
      } else if (e.type === 'progress') {
        thinking.content = '⏳ ' + e.stage
      } else if (e.type === 'meta') {
        thinking.content += '\n\n📚 来源：' + (e.sources || []).join('、')
      } else if (e.type === 'error') {
        thinking.content = '❌ ' + e.message
      }
    }, streamAbort.signal)
  } catch (e: any) {
    if (e?.name !== 'AbortError') {
      thinking.content = '❌ 连接异常：' + e.message
    }
  }
  sending.value = false
  await nextTick()
  chatBox.value?.scrollTo({ top: chatBox.value.scrollHeight })
}
</script>
