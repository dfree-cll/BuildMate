<template>
  <div>
    <h2>仪表盘</h2>
    <el-row :gutter="16">
      <el-col :span="6" v-for="card in cards" :key="card.path">
        <el-card style="cursor:pointer; margin-bottom:16px" @click="$router.push(card.path)">
          <div style="font-size:32px">{{ card.icon }}</div>
          <div style="font-weight:bold; margin:8px 0">{{ card.title }}</div>
          <div style="color:#999; font-size:12px">{{ card.desc }}</div>
        </el-card>
      </el-col>
    </el-row>
    <el-card v-if="stats">
      <template #header>LLM 调用统计（近24小时）</template>
      <el-descriptions :column="3" border>
        <el-descriptions-item label="调用次数">{{ stats.calls }}</el-descriptions-item>
        <el-descriptions-item label="总耗时(ms)">{{ Math.round(stats.total_ms) }}</el-descriptions-item>
        <el-descriptions-item label="估算成本($)">{{ stats.est_cost_usd }}</el-descriptions-item>
      </el-descriptions>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { obsStats } from '../api'
const cards = [
  { icon: '💬', title: '智能对话', desc: '建材价格/规范/政策问答', path: '/qa' },
  { icon: '📄', title: '投标审查', desc: '四维并行评审', path: '/bid-review' },
  { icon: '🛒', title: '采购审批', desc: '规则+LLM 双轨审核', path: '/procurement' },
  { icon: '🤝', title: '供应商谈判', desc: '多阶段状态机', path: '/negotiation' },
  { icon: '🏗️', title: 'BIM 审图', desc: 'IFC 模型合规审查', path: '/bim' },
]
const stats = ref<any>(null)
onMounted(async () => { try { stats.value = (await obsStats()).data } catch (e) {} })
</script>
