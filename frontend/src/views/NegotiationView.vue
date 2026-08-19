<template>
  <el-card>
    <template #header>🤝 供应商谈判（多阶段状态机）</template>
    <el-form inline>
      <el-form-item label="谈判标的"><el-input v-model="material" style="width:180px" /></el-form-item>
      <el-button type="primary" @click="start">开始谈判</el-button>
    </el-form>
    <div style="height:360px; overflow-y:auto; border:1px solid #eee; border-radius:8px; padding:16px; margin-bottom:12px">
      <div v-for="(m, i) in messages" :key="i" style="margin-bottom:12px; display:flex; justify-content: m.role==='user'?'flex-end':'flex-start'">
        <div :style="{maxWidth:'80%', padding:'10px 14px', borderRadius:'10px', background: m.role==='user'?'#1a237e':'#f5f5f5', color: m.role==='user'?'#fff':'#333', whiteSpace:'pre-wrap'}">{{ m.content }}</div>
      </div>
    </div>
    <el-input v-model="input" placeholder="输入报价/方案/条件..." @keyup.enter="send" />
    <el-button type="primary" style="margin-top:8px" @click="send">发送</el-button>
  </el-card>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { negChat } from '../api'

const material = ref('塔吊 QTZ63')
const input = ref('')
const messages = ref<any[]>([])
const sessionId = 'fe-neg-' + Date.now()

async function start() {
  messages.value = []
  const r = await negChat({ message: '开始谈判', material: material.value, session_id: sessionId, reset: true })
  const j = r.data
  messages.value.push({ role: 'assistant', content: '【阶段：' + j.stage + '】\n' + j.reply })
}
async function send() {
  const msg = input.value.trim()
  if (!msg) return
  input.value = ''
  messages.value.push({ role: 'user', content: msg })
  const r = await negChat({ message: msg, material: material.value, session_id: sessionId })
  const j = r.data
  messages.value.push({ role: 'assistant', content: '【阶段：' + j.stage + '】' + (j.done ? '（谈判完成）' : '') + '\n' + j.reply })
}
</script>
