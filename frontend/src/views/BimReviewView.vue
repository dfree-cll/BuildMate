<template>
  <el-card>
    <template #header>🏗️ BIM 模型合规审查</template>
    <el-upload drag :auto-upload="false" :on-change="onFile" :limit="1" accept=".ifc" style="margin-bottom:12px">
      <div style="padding:20px">📎 拖拽或点击上传 IFC 模型文件（IFC2X3 / IFC4）</div>
    </el-upload>
    <el-button type="primary" :loading="running" :disabled="!selectedFile" @click="submit">开始审查</el-button>

    <el-card v-if="result" style="margin-top:16px" :body-style="{padding:'16px'}">
      <template #header>
        审查结果
        <el-tag :type="riskTag" style="margin-left:8px">风险：{{ result.risk_level || '-' }}</el-tag>
        <el-tag :type="result.verdict === 'pass' ? 'success' : 'warning'" style="margin-left:4px">
          {{ result.verdict === 'pass' ? '通过' : '建议复核' }}
        </el-tag>
      </template>

      <el-descriptions :column="2" border size="small" style="margin-bottom:12px">
        <el-descriptions-item label="Schema">{{ result.file?.schema || '-' }}</el-descriptions-item>
        <el-descriptions-item label="建筑">{{ result.file?.building?.name || '未定义' }}</el-descriptions-item>
        <el-descriptions-item label="构件总数">{{ result.file?.total_elements ?? 0 }}</el-descriptions-item>
        <el-descriptions-item label="空间数">{{ result.file?.total_spaces ?? 0 }}</el-descriptions-item>
      </el-descriptions>

      <div v-if="Object.keys(result.file?.elements_count || {}).length" style="margin-bottom:12px">
        <strong>构件统计：</strong>
        <el-tag v-for="(n, t) in result.file.elements_count" :key="t" style="margin:2px" type="info">{{ t }} × {{ n }}</el-tag>
      </div>

      <div v-if="result.rule_issues?.length" style="margin-bottom:12px">
        <strong>⚠️ 规则检查：</strong>
        <div v-for="(i, idx) in result.rule_issues" :key="idx" style="margin:4px 0 0 8px">
          <el-tag size="small" :type="i.priority === 'high' ? 'danger' : (i.priority === 'medium' ? 'warning' : 'info')">{{ i.priority }}</el-tag>
          {{ i.description }}
        </div>
      </div>

      <div v-if="result.observations?.length" style="margin-bottom:12px">
        <strong>📋 审查观察：</strong>
        <div v-for="(o, idx) in result.observations" :key="idx" style="margin:4px 0 0 8px">• {{ o }}</div>
      </div>

      <div v-if="result.suggestions?.length" style="margin-bottom:12px">
        <strong>💡 改进建议：</strong>
        <div v-for="(s, idx) in result.suggestions" :key="idx" style="margin:4px 0 0 8px">• {{ s }}</div>
      </div>

      <div v-if="result.summary"><strong>结论：</strong>{{ result.summary }}</div>
    </el-card>

    <el-card v-if="history.length" style="margin-top:16px">
      <template #header>历史审查</template>
      <el-table :data="history" size="small">
        <el-table-column prop="file_name" label="文件" />
        <el-table-column prop="status" label="状态" width="100" />
        <el-table-column prop="created_at" label="时间" width="180" />
      </el-table>
    </el-card>
  </el-card>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { bimUpload, bimPoll, bimList } from '../api'

const selectedFile = ref<File | null>(null)
const running = ref(false)
const result = ref<any>(null)
const history = ref<any[]>([])

const riskTag = computed(() =>
  result.value?.risk_level === 'high' ? 'danger' : (result.value?.risk_level === 'medium' ? 'warning' : 'success'))

function onFile(file: any) {
  selectedFile.value = file.raw || file
}

async function loadHistory() {
  try { history.value = (await bimList()).data.items || [] } catch (e) {}
}

async function submit() {
  if (!selectedFile.value) return
  running.value = true
  result.value = null
  try {
    const r = await bimUpload(selectedFile.value)
    const reviewId = r.data.review_id
    for (let i = 0; i < 30; i++) {
      await new Promise(res => setTimeout(res, 1000))
      const pr = await bimPoll(reviewId)
      const pj = pr.data
      if (pj.status === 'processing') continue
      if (pj.status === 'failed') { ElMessage.error('审查失败：' + (pj.error || '')); break }
      result.value = pj
      break
    }
    if (!result.value) ElMessage.warning('审查仍在进行，请稍后刷新历史查看')
    loadHistory()
  } catch (e: any) {
    ElMessage.error('提交失败：' + (e.response?.data?.detail || e.message))
  }
  running.value = false
}

onMounted(loadHistory)
</script>
