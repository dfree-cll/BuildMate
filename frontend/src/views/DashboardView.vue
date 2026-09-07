<template>
  <div class="dashboard-page">
    <div class="dashboard-heading">
      <div class="dashboard-eyebrow">BUILDMATE / CONTROL CENTER</div>
      <h1>工作台</h1>
      <p>从问题到交付，统一管理你的工程智能任务。</p>
    </div>
    <el-row :gutter="20" class="dashboard-primary">
      <el-col v-for="card in primaryCards" :key="card.path" :xs="24" :sm="12">
        <button class="dashboard-card dashboard-card--primary" type="button" @click="go(card.path)">
          <span class="dashboard-card-icon"><el-icon><component :is="card.icon" /></el-icon></span>
          <span class="dashboard-card-content"><strong>{{ card.title }}</strong><small>{{ card.desc }}</small></span>
          <el-icon class="dashboard-arrow"><ArrowRight /></el-icon>
        </button>
      </el-col>
    </el-row>
    <section v-if="secondaryCards.length" class="dashboard-secondary">
      <div class="dashboard-section-title">管理与审核</div>
      <el-row :gutter="16">
        <el-col v-for="card in secondaryCards" :key="card.path" :xs="24" :sm="12" :md="8">
          <button class="dashboard-card dashboard-card--secondary" type="button" @click="go(card.path)">
            <el-icon class="dashboard-secondary-icon"><component :is="card.icon" /></el-icon>
            <span><strong>{{ card.title }}</strong><small>{{ card.desc }}</small></span>
          </button>
        </el-col>
      </el-row>
    </section>
    <el-card v-if="stats || statsError" class="dashboard-stats">
      <template #header>运行概览 · 近 24 小时 <el-tag v-if="statsError" type="warning" size="small">暂时不可用</el-tag></template>
      <el-alert v-if="statsError" :title="statsError" type="warning" :closable="false" show-icon />
      <el-descriptions v-else-if="stats" :column="1" border>
        <el-descriptions-item label="调用次数">{{ stats.calls }}</el-descriptions-item>
        <el-descriptions-item label="总耗时(ms)">{{ Math.round(stats.total_ms) }}</el-descriptions-item>
        <el-descriptions-item label="估算成本($)">{{ stats.est_cost_usd }}</el-descriptions-item>
      </el-descriptions>
    </el-card>
  </div>
</template>
<script setup lang="ts">
import { computed, ref, onMounted, type Component } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowRight, ChatDotRound, OfficeBuilding, Collection, List, User } from '@element-plus/icons-vue'
import { obsStats } from '../api'
import { useAuthStore } from '../stores/auth'
const router = useRouter()
const auth = useAuthStore()
type Card = { icon: Component; title: string; desc: string; path: string; roles?: string[] }
const primaryCards: Card[] = [
  { icon: ChatDotRound, title: '智能工作台', desc: '规范、标书、采购与谈判，一句话开始任务', path: '/qa' },
  { icon: OfficeBuilding, title: 'BIM Agent', desc: '图纸识别、模型审核与 Revit 交付', path: '/bim' },
]
const allSecondary: Card[] = [
  { icon: Collection, title: '项目知识库', desc: '资料入库与证据检索', path: '/knowledge', roles: ['admin'] },
  { icon: List, title: '任务时间线', desc: '查看任务、失败和审批状态', path: '/tasks', roles: ['admin'] },
  { icon: User, title: '审核中心', desc: '处理待审批业务与知识待补项', path: '/review', roles: ['admin', 'reviewer'] },
]
const secondaryCards = computed(() => allSecondary.filter(c => !c.roles || c.roles.includes(auth.user?.role || '')))
const stats = ref<{ calls: number; total_ms: number; est_cost_usd: number } | null>(null)
const statsError = ref('')
function go(path: string) { void router.push(path) }
onMounted(async () => {
  try { stats.value = (await obsStats()).data }
  catch { statsError.value = '运行统计暂时无法加载，请稍后重试。' }
})
</script>
<style scoped>
.dashboard-heading { margin: 8px 0 30px; }.dashboard-eyebrow { color: var(--bm-blue); font-size: 10px; font-weight: 700; letter-spacing: .18em; } h1 { margin: 8px 0 5px; font-size: 36px; letter-spacing: -.05em; }.dashboard-heading p { margin: 0; color: var(--bm-muted); }
.dashboard-card { width: 100%; display: flex; align-items: center; gap: 16px; text-align: left; border: 1px solid var(--bm-line); color: var(--bm-ink); background: rgba(255,255,255,.82); cursor: pointer; transition: transform .2s ease, box-shadow .2s ease, border-color .2s ease; }.dashboard-card:hover,.dashboard-card:focus-visible { transform: translateY(-3px); border-color: rgba(0,113,227,.3); box-shadow: 0 14px 34px rgba(0,0,0,.08); outline: none; }.dashboard-card--primary { min-height: 154px; padding: 26px; margin-bottom: 20px; border-radius: 20px; }.dashboard-card-icon { display: grid; place-items: center; width: 50px; height: 50px; border-radius: 15px; color: #fff; background: linear-gradient(145deg,#4da8ff,#0071e3); font-size: 24px; }.dashboard-card-content,.dashboard-card--secondary span { display: flex; flex-direction: column; gap: 6px; }.dashboard-card strong { font-size: 18px; }.dashboard-card small { color: var(--bm-muted); font-size: 13px; line-height: 1.5; }.dashboard-arrow { margin-left: auto; color: var(--bm-blue); }.dashboard-secondary { margin-top: 20px; }.dashboard-section-title { margin: 0 0 12px; color: var(--bm-muted); font-size: 13px; font-weight: 600; }.dashboard-card--secondary { min-height: 86px; padding: 18px; margin-bottom: 16px; border-radius: 15px; }.dashboard-secondary-icon { color: var(--bm-blue); font-size: 20px; }.dashboard-card--secondary strong { font-size: 14px; }.dashboard-stats { margin-top: 16px; }
@media (max-width: 600px) { h1 { font-size: 30px; }.dashboard-card--primary { min-height: 126px; padding: 20px; } }
@media (prefers-reduced-motion: reduce) { .dashboard-card:hover, .dashboard-card:focus-visible { transform: none; } }
</style>
