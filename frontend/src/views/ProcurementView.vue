<template>
  <AgentMemoryPanel agent="procurement" :disabled="submitting || approving" />
  <p v-if="memoryLoading" class="qa-memory-status" role="status">正在恢复采购会话；当前填写的采购明细会保留。</p>
  <el-card>
    <template #header>🛒 采购审批（多品类 + 行情对比 + 分级审批）</template>

    <!-- 多品类表单 -->
    <div v-for="(it, idx) in items" :key="idx" style="display:flex; gap:8px; margin-bottom:8px; flex-wrap:wrap">
      <el-input v-model="it.material_name" placeholder="材料（如螺纹钢）" style="width:150px" />
      <el-input v-model="it.spec" placeholder="规格" style="width:100px" />
      <el-input-number v-model="it.quantity" :min="1" placeholder="数量" />
      <el-select v-model="it.unit" style="width:90px">
        <el-option v-for="u in units" :key="u" :label="u" :value="u" />
      </el-select>
      <el-input-number v-model="it.unit_price" :min="0.01" :precision="2" placeholder="单价(元)" />
      <el-input v-model="it.supplier" placeholder="供应商(可选)" style="width:140px" />
      <el-button type="danger" link @click="removeItem(idx)">删除</el-button>
    </div>
    <el-button size="small" @click="addItem" style="margin-bottom:12px">＋ 添加品类</el-button>
    <el-button type="primary" :loading="submitting" :disabled="submitting || approving || memoryLoading"
               @click="submit" style="margin-bottom:12px; margin-left:8px">提交采购单</el-button>

    <el-alert v-if="order" :title="'采购单 ' + order.order_no + '｜总金额 ' + order.total_amount + ' 元｜AI 结论：' + order.ai_verdict"
              type="info" style="margin-bottom:12px" />
    <el-alert v-if="order && order.ai_conclusion?.issues?.length" :title="order.ai_conclusion.issues.join('；')"
              type="warning" style="margin-bottom:12px" />

    <!-- 品类级明细 -->
    <el-table v-if="order?.ai_conclusion?.per_item?.length" :data="order.ai_conclusion.per_item" size="small"
              style="margin-bottom:12px">
      <el-table-column prop="material_name" label="材料" />
      <el-table-column prop="spec" label="规格" width="90" />
      <el-table-column prop="quantity" label="数量" width="70" />
      <el-table-column prop="unit_price" label="单价" width="90" />
      <el-table-column prop="amount" label="金额" width="100" />
      <el-table-column label="行情偏离" width="110">
        <template #default="s">
          <span v-if="s.row.deviation != null" :style="{ color: Math.abs(s.row.deviation) > 0.3 ? 'red' : 'green' }">
            {{ (s.row.deviation * 100).toFixed(1) }}%
          </span>
          <span v-else style="color:#999">无行情</span>
        </template>
      </el-table-column>
      <el-table-column label="判定" width="80">
        <template #default="s">
          <el-tag :type="s.row.verdict === 'pass' ? 'success' : 'warning'" size="small">{{ s.row.verdict }}</el-tag>
        </template>
      </el-table-column>
    </el-table>

    <template v-if="order">
      <el-alert v-if="order.needs_human_approval && !canApprove" type="warning" :closable="false"
                title="该单已进入人工审批，请通知管理员或审核员在「审核端」处理" style="margin-top:12px" />
      <el-button v-if="order.needs_human_approval && canApprove" :disabled="approving" type="success" @click="confirm('approved')">✅ 批准</el-button>
      <el-button v-if="order.needs_human_approval && canApprove" :disabled="approving" type="danger" @click="confirm('rejected')">❌ 驳回</el-button>
      <el-alert v-if="confirmResult" :title="confirmResult" type="success" style="margin-top:12px" />
    </template>
  </el-card>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { createOrder, confirmOrder } from '../api'
import { useAuthStore } from '../stores/auth'
import { apiErrorMessage } from '../utils/apiError'
import AgentMemoryPanel from '../components/AgentMemoryPanel.vue'
import { useAgentSession } from '../stores/agentMemory'
const memory = useAgentSession('procurement')
const memoryLoading = computed(() => memory.state.value.loading)

const auth = useAuthStore()
const canApprove = computed(() => ['admin', 'reviewer'].includes(auth.user?.role || ''))

const units = ['吨', '米', '个', '立方米', '袋', '项', '套', '张']
const items = ref([{ material_name: '螺纹钢', quantity: 100, unit_price: 3600, unit: '吨', spec: 'Φ25', supplier: '' }])
const order = ref<any>(null)
const submitting = ref(false)
const approving = ref(false)
const confirmResult = ref('')
let generation = 0
watch([memory.key, memory.sessionId], () => {
  ++generation
  // Keep unsent rows while the persisted session id is restored.  Clearing
  // them here made a fully entered order disappear just before submission.
  order.value = null
  confirmResult.value = ''
  submitting.value = false
  approving.value = false
}, { flush: 'sync' })

function addItem() { items.value.push({ material_name: '', quantity: 1, unit_price: 0, unit: '吨', spec: '', supplier: '' }) }
function removeItem(idx: number) { items.value.splice(idx, 1) }

async function submit() {
  if (submitting.value || approving.value || memoryLoading.value) return
  if (!items.value.length) { ElMessage.warning('请至少填写一个品类'); return }
  const requestGeneration = ++generation
  submitting.value = true
  confirmResult.value = ''
  try {
    const r = await createOrder({ items: items.value, session_id: memory.sessionId.value, project_id: memory.projectId.value })
    if (requestGeneration !== generation) return
    order.value = r.data
    ElMessage.success(r.data.needs_human_approval ? '已提交，进入人工审批' : '已提交，低金额品类快速审批通过')
  } catch (e) {
    if (requestGeneration === generation) ElMessage.error(apiErrorMessage(e, '采购提交失败'))
  } finally { if (requestGeneration === generation) submitting.value = false }
}

async function confirm(decision: string) {
  if (!order.value || approving.value || submitting.value) return
  const requestGeneration = ++generation
  approving.value = true
  try {
    const r = await confirmOrder(order.value.order_no, { decision, comment: decision === 'approved' ? '同意采购' : '价格不合理' })
    if (requestGeneration !== generation) return
    confirmResult.value = '✅ 审批结果：' + r.data.final_verdict + '\n' + r.data.message
    order.value = null
  } catch (e) {
    if (requestGeneration === generation) ElMessage.error(apiErrorMessage(e, '采购审批失败'))
  } finally { if (requestGeneration === generation) approving.value = false }
}
</script>
