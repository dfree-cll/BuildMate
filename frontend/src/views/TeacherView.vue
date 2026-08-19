<template>
  <div>
    <el-card style="margin-bottom:16px">
      <template #header>⏳ 待人工审批采购单</template>
      <el-table :data="pendings" v-loading="loading1">
        <el-table-column prop="order_no" label="单号" />
        <el-table-column prop="material_name" label="材料" />
        <el-table-column prop="quantity" label="数量" />
        <el-table-column label="金额"><template #default="s">{{ s.row.total_amount }} 元</template></el-table-column>
        <el-table-column label="操作">
          <template #default="s">
            <el-button size="small" type="success" @click="approve(s.row)">批准</el-button>
            <el-button size="small" type="danger" @click="reject(s.row)">驳回</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-button style="margin-top:8px" @click="loadPending">刷新</el-button>
    </el-card>

    <el-card>
      <template #header>📚 知识待补队列</template>
      <el-table :data="kpendings" v-loading="loading2">
        <el-table-column prop="question" label="问题" />
        <el-table-column prop="confidence" label="置信度" width="90"><template #default="s">{{ (s.row.confidence * 100).toFixed(0) }}%</template></el-table-column>
        <el-table-column label="操作" width="230">
          <template #default="s">
            <el-button size="small" type="primary" @click="openAnswer(s.row)">补充答案入库</el-button>
            <el-button size="small" @click="resolve(s.row)">仅标记解决</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-button style="margin-top:8px" @click="loadKPending">刷新</el-button>
    </el-card>

    <el-dialog v-model="dialogVisible" :title="'补充答案（将写入知识库，立即可被问答检索）'" width="560px">
      <div style="margin-bottom:8px; color:#666">问：{{ answering?.question }}</div>
      <el-input v-model="answerText" type="textarea" :rows="6" placeholder="输入标准答案（≥5 个字），提交后该问答会作为知识 chunk 入库" />
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" :disabled="answerText.trim().length < 5" @click="submitAnswer">入库并解决</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { pendingOrders, confirmOrder, knowledgePending, knowledgeResolve, knowledgeAnswer } from '../api'

const pendings = ref<any[]>([])
const kpendings = ref<any[]>([])
const loading1 = ref(false)
const loading2 = ref(false)
const dialogVisible = ref(false)
const answering = ref<any>(null)
const answerText = ref('')
const submitting = ref(false)

async function loadPending() {
  loading1.value = true
  try { pendings.value = (await pendingOrders()).data.items || [] } catch (e) {} finally { loading1.value = false }
}
async function loadKPending() {
  loading2.value = true
  try { kpendings.value = (await knowledgePending()).data.items || [] } catch (e) {} finally { loading2.value = false }
}
async function approve(row: any) {
  await confirmOrder(row.order_no, { decision: 'approved', comment: '教师批准' })
  ElMessage.success('已批准'); loadPending()
}
async function reject(row: any) {
  await confirmOrder(row.order_no, { decision: 'rejected', comment: '教师驳回' })
  ElMessage.success('已驳回'); loadPending()
}
async function resolve(row: any) {
  await knowledgeResolve(row.id)
  ElMessage.success('已标记解决'); loadKPending()
}
function openAnswer(row: any) {
  answering.value = row
  answerText.value = ''
  dialogVisible.value = true
}
async function submitAnswer() {
  if (!answering.value) return
  submitting.value = true
  try {
    await knowledgeAnswer(answering.value.id, answerText.value.trim())
    ElMessage.success('答案已入库，问答即刻可检索')
    dialogVisible.value = false
    loadKPending()
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || '提交失败')
  }
  submitting.value = false
}
onMounted(() => { loadPending(); loadKPending() })
</script>
