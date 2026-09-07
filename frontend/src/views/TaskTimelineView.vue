<template>
  <div>
    <h2>任务时间线</h2>
    <el-card>
      <el-input v-model="taskId" placeholder="输入任务 ID">
        <template #append><el-button :loading="tasks.loading" @click="load">查看</el-button></template>
      </el-input>
      <template v-if="task">
        <el-descriptions :column="2" border style="margin-top:16px">
          <el-descriptions-item label="任务">{{ task.id }}</el-descriptions-item>
          <el-descriptions-item label="状态">{{ task.status }}</el-descriptions-item>
          <el-descriptions-item label="工作流">{{ task.workflow }}</el-descriptions-item>
          <el-descriptions-item label="版本">{{ task.version }}</el-descriptions-item>
        </el-descriptions>
        <div v-if="task.status === 'waiting_human'" style="margin-top:16px">
          <el-button type="primary" :loading="tasks.loading" @click="resume('approved')">批准并继续</el-button>
          <el-button type="danger" :loading="tasks.loading" @click="resume('rejected')">拒绝并取消</el-button>
        </div>
        <el-timeline style="margin-top:20px">
          <el-timeline-item v-for="step in steps" :key="step.id" :timestamp="step.started_at || ''" :type="step.status === 'succeeded' ? 'success' : step.status === 'failed' ? 'danger' : 'primary'">
            <strong>{{ step.name }}</strong> · {{ step.status }} · 第 {{ step.attempts }} 次尝试
            <div v-if="step.error_message" style="color:#c0392b">{{ step.error_message }}</div>
          </el-timeline-item>
        </el-timeline>
      </template>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useProjectStore } from '../stores/project'
import { useTaskStore } from '../stores/task'
const project = useProjectStore()
const tasks = useTaskStore()
const taskId = ref('')
const task = computed(() => tasks.tasks[taskId.value])
const steps = computed(() => tasks.steps[taskId.value] || [])
async function load() {
  if (!taskId.value || !project.currentId) return
  try { await tasks.refresh(taskId.value, project.currentId) }
  catch (error: any) { ElMessage.error(error.response?.data?.error?.message || '任务不存在或无权访问') }
}
async function resume(decision: 'approved' | 'rejected') {
  if (!taskId.value || !project.currentId) return
  try {
    await tasks.resume(taskId.value, project.currentId, decision)
    await tasks.refresh(taskId.value, project.currentId)
    ElMessage.success(decision === 'approved' ? '已批准，任务将继续执行' : '已拒绝，任务已取消')
  } catch (error: any) {
    ElMessage.error(error.response?.data?.error?.message || '审批失败')
  }
}
</script>
