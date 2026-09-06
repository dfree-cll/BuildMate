<template>
  <div>
    <h2>审核中心</h2>
    <el-card style="margin-bottom:16px">
      <template #header>待人工审批采购单</template>
      <el-table :data="pendings" v-loading="loadingOrders">
        <el-table-column prop="order_no" label="单号" />
        <el-table-column prop="material_name" label="材料" />
        <el-table-column prop="quantity" label="数量" />
        <el-table-column label="金额"><template #default="scope">{{ scope.row.total_amount }} 元</template></el-table-column>
        <el-table-column label="操作">
          <template #default="scope">
            <el-button size="small" type="success" @click="approve(scope.row)">批准</el-button>
            <el-button size="small" type="danger" @click="reject(scope.row)">驳回</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-button style="margin-top:8px" @click="loadPending">刷新</el-button>
    </el-card>

    <el-card>
      <template #header>知识待补队列</template>
      <el-table :data="knowledgePendings" v-loading="loadingKnowledge">
        <el-table-column prop="question" label="问题" />
        <el-table-column prop="confidence" label="置信度" width="90"><template #default="scope">{{ (scope.row.confidence * 100).toFixed(0) }}%</template></el-table-column>
        <el-table-column label="操作" width="230">
          <template #default="scope">
            <el-button size="small" type="primary" @click="openAnswer(scope.row)">补充答案入库</el-button>
            <el-button size="small" @click="resolve(scope.row)">仅标记解决</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-button style="margin-top:8px" @click="loadKnowledgePending">刷新</el-button>
    </el-card>

    <el-dialog v-model="dialogVisible" title="补充答案" width="560px">
      <div style="margin-bottom:8px; color:#666">问：{{ answering?.question }}</div>
      <el-input v-model="answerText" type="textarea" :rows="6" placeholder="输入标准答案（至少 5 个字）" />
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" :disabled="answerText.trim().length < 5" @click="submitAnswer">入库并解决</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { confirmOrder, knowledgeAnswer, knowledgePending, knowledgeResolve, pendingOrders } from '../api'

const pendings = ref<any[]>([])
const knowledgePendings = ref<any[]>([])
const loadingOrders = ref(false)
const loadingKnowledge = ref(false)
const dialogVisible = ref(false)
const answering = ref<any>(null)
const answerText = ref('')
const submitting = ref(false)

async function loadPending() {
  loadingOrders.value = true
  try { pendings.value = (await pendingOrders()).data.items || [] }
  finally { loadingOrders.value = false }
}

async function loadKnowledgePending() {
  loadingKnowledge.value = true
  try { knowledgePendings.value = (await knowledgePending()).data.items || [] }
  finally { loadingKnowledge.value = false }
}

async function approve(row: any) {
  await confirmOrder(row.order_no, { decision: 'approved', comment: '审核批准' })
  ElMessage.success('已批准')
  await loadPending()
}

async function reject(row: any) {
  await confirmOrder(row.order_no, { decision: 'rejected', comment: '审核驳回' })
  ElMessage.success('已驳回')
  await loadPending()
}

async function resolve(row: any) {
  await knowledgeResolve(row.id)
  ElMessage.success('已标记解决')
  await loadKnowledgePending()
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
    ElMessage.success('答案已入库')
    dialogVisible.value = false
    await loadKnowledgePending()
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail || '提交失败')
  } finally {
    submitting.value = false
  }
}

onMounted(() => { loadPending(); loadKnowledgePending() })
</script>
