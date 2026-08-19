<template>
  <el-card>
    <template #header>📄 投标文件四维并行评审</template>
    <el-upload drag :auto-upload="false" :on-change="onFile" :limit="1" accept=".pdf" style="margin-bottom:12px">
      <div style="padding:20px">📎 拖拽或点击上传 PDF 投标文件（或粘贴文本）</div>
    </el-upload>
    <el-input v-model="docText" type="textarea" :rows="4" placeholder="或直接粘贴投标文件文本（留空则用模拟文档）" style="margin-bottom:12px" />
    <el-button type="primary" :loading="running" @click="submit">开始评审</el-button>
    <el-card v-if="result" style="margin-top:16px; background:#f9fbe7">
      <pre style="white-space:pre-wrap">{{ result }}</pre>
    </el-card>
  </el-card>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { bidSubmit, bidUpload, bidPoll } from '../api'

const docText = ref('')
const selectedFile = ref<File | null>(null)   // 独立 ref 保存上传文件（修复：原 _file 挂错对象）
const running = ref(false)
const result = ref('')

function onFile(file: any) {
  selectedFile.value = file.raw || file
  docText.value = '[文件: ' + file.name + ' 已选择，点击开始评审]'
}

async function submit() {
  running.value = true
  result.value = '⏳ 已提交，四维并行评审中...'
  try {
    let reviewId: string
    if (selectedFile.value) {
      const r = await bidUpload(selectedFile.value)
      reviewId = r.data.review_id
    } else {
      const r = await bidSubmit(docText.value)
      reviewId = r.data.review_id
    }
    for (let i = 0; i < 45; i++) {
      await new Promise(res => setTimeout(res, 2000))
      const pr = await bidPoll(reviewId)
      const pj = pr.data
      if (pj.status === 'processing') { result.value = '⏳ 评审中（' + (i + 1) + '）...'; continue }
      if (pj.status === 'failed') { result.value = '❌ 评审失败：' + (pj.error || ''); break }
      let html = '🏆 综合得分：' + pj.weighted_score + ' / 100\n\n📊 各维度：\n'
      for (const d of pj.dimensions || []) html += '  • ' + d.dimension + '：' + d.score + ' 分\n'
      if (pj.summary) html += '\n📝 ' + (pj.summary.overall_comment || '') + '\n✅ 结论：' + (pj.summary.recommendation || '')
      if (pj.issues?.length) { html += '\n\n⚠️ 风险问题：\n'; for (const iss of pj.issues.slice(0, 5)) html += '  • [' + iss.priority + '] ' + iss.description + '\n' }
      result.value = html
      break
    }
  } catch (e: any) {
    result.value = '❌ 评审失败：' + (e.response?.data?.detail || e.message)
  }
  running.value = false
}
</script>
