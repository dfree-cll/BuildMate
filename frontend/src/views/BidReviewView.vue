<template>
  <el-card>
    <template #header>📄 投标文件四维并行评审</template>
    <AgentMemoryPanel agent="bid_review" :disabled="running" @restored="restore" />
    <p v-if="memoryLoading" class="qa-memory-status" role="status">正在恢复投标审查会话；已选择的文件和已输入文字会保留。</p>
    <p>同一会话中再次上传作为新版本审查，历史问题仅供对比，不替代本次文件依据。</p>
    <el-upload :key="memory.key.value" drag multiple :auto-upload="false" :limit="5" :on-change="onFiles" :on-remove="onRemove"
               :disabled="running"
               accept=".pdf,.docx,.jpg,.jpeg,.png,.bmp,.txt,.md" style="margin-bottom:12px">
      <div style="padding:20px">📎 拖拽或点击上传投标文件（可多选：技术标/商务标/资质分开投，支持 PDF/Word/图片，最多 5 份）</div>
    </el-upload>
    <div v-if="selectedFiles.length" style="margin-bottom:12px; color:#666">
      已选择 {{ selectedFiles.length }} 份：{{ selectedFiles.map(f => f.name).join('、') }}
    </div>
    <el-input v-model="docText" type="textarea" :rows="4" maxlength="20000" :disabled="running"
              placeholder="或直接粘贴投标文件文本（留空则用模拟文档）" style="margin-bottom:12px" />
    <el-button type="primary" :loading="running" :disabled="running || memoryLoading" @click="submit">开始评审</el-button>
    <el-card v-if="result" style="margin-top:16px; background:#f9fbe7">
      <pre style="white-space:pre-wrap">{{ result }}</pre>
    </el-card>
  </el-card>
</template>

<script setup lang="ts">
import { ref, computed, onBeforeUnmount, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { bidSubmit, bidUploadMulti, bidPoll } from '../api'
import { apiErrorMessage } from '../utils/apiError'
import { formatBidReport } from '../utils/bidReport'
import AgentMemoryPanel from '../components/AgentMemoryPanel.vue'
import { useAgentSession, type MemoryDetail } from '../stores/agentMemory'
const memory = useAgentSession('bid_review')
const memoryLoading = computed(() => memory.state.value.loading)
function restore(detail: MemoryDetail) {
  if (running.value) return
  ++pollGeneration
  running.value = false
  result.value = detail.turns[detail.turns.length - 1]?.answer || ''
}

const docText = ref('')
const selectedFiles = ref<File[]>([])   // 多文件列表（技术标/商务标/资质可分开投）
const running = ref(false)
const result = ref('')
let pollGeneration = 0
watch([memory.key, memory.sessionId], () => {
  ++pollGeneration
  running.value = false
  result.value = ''
  // Unsent files/text are a user draft, not conversation memory.  Keep the
  // draft when the durable session finishes hydrating so input cannot vanish
  // between selection and clicking Submit.
}, { flush: 'sync' })
onBeforeUnmount(() => { pollGeneration += 1 })

function onFiles(_file: any, fileList: any[]) {
  selectedFiles.value = fileList.map(f => f.raw).filter(Boolean)
  if (selectedFiles.value.length) {
    docText.value = '[' + selectedFiles.value.map(f => f.name).join('、') + ' 已选择，点击开始评审]'
  }
}
function onRemove(_file: any, fileList: any[]) {
  selectedFiles.value = fileList.map(f => f.raw).filter(Boolean)
}

async function submit() {
  if (running.value || memoryLoading.value) return
  const generation = ++pollGeneration
  running.value = true
  result.value = '⏳ 已提交，四维并行评审中...'
  try {
    let reviewId: string
    if (selectedFiles.value.length) {
      const r = await bidUploadMulti(selectedFiles.value, memory.sessionId.value, memory.projectId.value)
      reviewId = r.data.review_id
    } else {
      const r = await bidSubmit(docText.value, memory.sessionId.value, memory.projectId.value)
      reviewId = r.data.review_id
    }
    for (let i = 0; i < 300; i++) {   // 盖章扫描件解析可达 8-10 分钟，轮询放宽到 10 分钟
      await new Promise(res => setTimeout(res, 2000))
      if (generation !== pollGeneration) return
      const pr = await bidPoll(reviewId)
      if (generation !== pollGeneration) return
      const pj = pr.data
      if (pj.status === 'processing') { result.value = '⏳ 解析+评审中（' + Math.round((i + 1) * 2) + ' 秒，大扫描件需数分钟）...'; continue }
      if (pj.status === 'failed') { result.value = '❌ 评审失败：' + (pj.error || ''); break }
      result.value = formatBidReport(pj, 6)
      break
    }
  } catch (e: any) {
    if (generation !== pollGeneration) return
    result.value = '❌ 评审失败：' + apiErrorMessage(e, '未知错误')
  } finally {
    if (generation === pollGeneration) running.value = false
  }
}
</script>
