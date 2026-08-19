<template>
  <el-card>
    <template #header>🛒 采购审批（规则引擎 + LLM 双轨）</template>
    <el-form inline>
      <el-form-item label="材料"><el-input v-model="form.material_name" style="width:160px" /></el-form-item>
      <el-form-item label="数量"><el-input-number v-model="form.quantity" :min="1" /></el-form-item>
      <el-form-item label="单价"><el-input-number v-model="form.unit_price" :min="1" :precision="2" /></el-form-item>
      <el-button type="primary" :loading="submitting" @click="submit">提交采购单</el-button>
    </el-form>
    <el-alert v-if="order" :title="'采购单 ' + order.order_no + '｜总金额 ' + order.total_amount + ' 元｜AI 结论：' + order.ai_verdict" type="info" style="margin-bottom:12px" />
    <el-alert v-if="order && order.ai_conclusion?.issues?.length" :title="order.ai_conclusion.issues.join('；')" type="warning" style="margin-bottom:12px" />
    <template v-if="order">
      <el-alert v-if="order.needs_human_approval && !canApprove" type="warning" :closable="false"
                title="该单已进入人工审批，请通知管理员/教师在「教师端」处理" style="margin-top:12px" />
      <el-button v-if="order.needs_human_approval && canApprove" type="success" @click="confirm('approved')">✅ 批准</el-button>
      <el-button v-if="order.needs_human_approval && canApprove" type="danger" @click="confirm('rejected')">❌ 驳回</el-button>
      <el-alert v-if="confirmResult" :title="confirmResult" type="success" style="margin-top:12px" />
    </template>
  </el-card>
</template>

<script setup lang="ts">
import { reactive, ref, computed } from 'vue'
import { ElMessage } from 'element-plus'
import { createOrder, confirmOrder } from '../api'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
// 审批按钮只对 admin/teacher 显示（服务端 require_role 同款校验，此处仅 UI 层避免点了才 403）
const canApprove = computed(() => ['admin', 'teacher'].includes(auth.user?.role || ''))

const form = reactive({ material_name: '螺纹钢 HRB400', quantity: 100, unit_price: 3600 })
const order = ref<any>(null)
const submitting = ref(false)
const confirmResult = ref('')

async function submit() {
  submitting.value = true
  confirmResult.value = ''
  try {
    const r = await createOrder({ ...form, session_id: 'fe-po-' + Date.now() })
    order.value = r.data
    ElMessage.success('已提交')
  } catch (e: any) { ElMessage.error(e.response?.data?.detail || e.message) } finally { submitting.value = false }
}

async function confirm(decision: string) {
  try {
    // operator 由服务端从登录态强制取值，客户端不再传（历史遗留参数已删）
    const r = await confirmOrder(order.value.order_no, { decision, comment: decision === 'approved' ? '同意采购' : '价格不合理' })
    confirmResult.value = '✅ 审批结果：' + r.data.final_verdict + '\n' + r.data.message
    order.value = null
  } catch (e: any) { ElMessage.error(e.response?.data?.detail || e.message) }
}
</script>
