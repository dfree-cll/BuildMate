<template>
  <el-card class="qa-workspace">
    <div class="qa-hero">
      <div class="qa-orb" aria-hidden="true"></div>
      <div>
        <div class="eyebrow">BUILD MATE / INTELLIGENCE</div>
        <h1>智能工作台</h1>
        <p>从一个问题开始，连接知识、投标审查、采购与谈判。</p>
      </div>
    </div>
    <div class="qa-shortcuts">
      <el-button round :aria-pressed="!capability" @click="clearCapability">资料问答</el-button>
      <el-button round @click="clearCapability(); input = '查询建筑规范并给出依据'">规范查询</el-button>
      <el-button v-for="(label, key) in capabilityLabels" :key="key" round :aria-pressed="capability === key" @click="openCapability(key)">{{ label }}</el-button>
    </div>
    <div v-if="capability" class="qa-capability-bar">
      <span>当前工作能力：{{ capabilityLabel }}</span>
      <el-button link @click="clearCapability">返回对话</el-button>
    </div>
    <!-- Keep the cache mounted even when returning to chat; only deactivate its child. -->
    <div v-show="capabilityComponent" class="qa-embedded-panel-wrap">
      <KeepAlive><component :is="capabilityComponent" v-if="capabilityComponent" /></KeepAlive>
    </div>
    <p v-if="capability" class="eyebrow">智能助手 · 查资料或切换能力</p>
    <AgentMemoryPanel agent="qa" :disabled="sending" @restored="restore" />
    <p v-if="memoryLoading" class="qa-memory-status" role="status">正在恢复当前会话，恢复完成后即可发送问题；已输入内容会保留。</p>
    <div class="chat-box qa-conversation" :class="{ 'qa-conversation--compact': capability }" ref="chatBox" role="log" aria-label="智能对话记录" :aria-busy="sending || memoryLoading">
      <div v-if="!messages.length" class="qa-empty">
        <span class="eyebrow">ONE WORKSPACE · CLEAR CONTEXT</span>
        <h2>想先解决什么问题？</h2>
        <p>直接提问，或选择上方的专业能力。<br />各业务独立保存记录，重要操作仍需确认。</p>
      </div>
      <div v-for="(m, i) in messages" :key="i" class="chat-row" :class="{ 'chat-row--user': m.role === 'user' }">
        <div class="chat-bubble" :class="{ 'chat-bubble--user': m.role === 'user' }">
          {{ m.content }}
          <div v-if="m.plan" class="qa-task-plan">
            <strong>{{ m.plan.mode === 'composite' ? '复合任务计划' : '任务识别' }} · 尚未执行</strong>
            <span v-for="step in m.plan.steps" :key="step.step">{{ step.step }} · {{ taskDomainLabels[step.domain] }} · {{ taskRouteLabel(step) }}<em v-if="step.missing_parameters.length">待补：{{ step.missing_parameters.map(missingParameterLabel).join('、') }}</em></span>
            <small v-if="m.plan.needs_confirmation">提交前需要人工确认</small>
            <small v-if="m.plan.needs_clarification">缺少必要参数，暂不会创建任务</small>
            <el-button v-if="canConfirmPlan(m)" size="small" type="primary" :loading="m.planSubmitting" @click="confirmPlan(m)">确认并创建任务</el-button>
            <small v-if="m.planSubmitted" class="qa-plan-success">任务已创建，可在任务时间线查看进度</small>
          </div>
          <div v-if="m.actions?.length" class="qa-message-actions">
            <el-button v-for="action in m.actions" :key="action.target" size="small" @click="activateAction(action.target)">{{ action.label }}</el-button>
          </div>
        </div>
      </div>
    </div>
    <form class="qa-composer" @submit.prevent="send">
      <el-input v-model="input" aria-label="输入问题" maxlength="2000" :disabled="sending"
                placeholder="问规范、查资料，或描述您要办理的业务…"
                @compositionstart="onCompositionStart" @compositionend="onCompositionEnd"
                @keydown.enter="onEnter" @keyup.enter="onKeyup" />
      <el-button native-type="submit" type="primary" :loading="sending"
                 :disabled="!canSubmit">发送</el-button>
    </form>
  </el-card>
</template>

<script setup lang="ts">
import { ref, computed, nextTick, onBeforeUnmount, watch, defineAsyncComponent, type Component } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { chatStream } from '../api'
import { v2Api } from '../api/v2'
import { apiErrorMessage } from '../utils/apiError'
import { parseTaskPlan, taskDomainLabels, taskRouteLabel, missingParameterLabel, taskPlanActions, type TaskPlanSummary } from '../utils/taskPlan'
import AgentMemoryPanel from '../components/AgentMemoryPanel.vue'
import { useAgentSession, type MemoryDetail } from '../stores/agentMemory'

const capabilityLabels = { bid_review: '投标审查', procurement: '采购分析', negotiation: '谈判辅助' }
type Capability = keyof typeof capabilityLabels
type ActionTarget = Capability | 'bim'
interface ChatAction { target: ActionTarget; label: string }
interface ChatMessage {
  role: string
  content: string
  actions?: ChatAction[]
  plan?: TaskPlanSummary
  rawPlan?: Record<string, unknown>
  planSubmitting?: boolean
  planSubmitted?: boolean
}
const isCapability = (value: unknown): value is Capability => typeof value === 'string' && Object.hasOwn(capabilityLabels, value)
const messages = ref<ChatMessage[]>([])
const input = ref('')
const sending = ref(false)
const composing = ref(false)
const chatBox = ref<HTMLElement | null>(null)
const memory = useAgentSession('qa')
const route = useRoute()
const router = useRouter()
const capability = ref<Capability | ''>(isCapability(route.query.capability) ? route.query.capability : '')
const capabilityComponents: Record<Capability, Component> = {
  bid_review: defineAsyncComponent(() => import('./BidReviewView.vue')),
  procurement: defineAsyncComponent(() => import('./ProcurementView.vue')),
  negotiation: defineAsyncComponent(() => import('./NegotiationView.vue')),
}
const capabilityComponent = computed(() => capability.value ? capabilityComponents[capability.value] : undefined)
const capabilityLabel = computed(() => capability.value ? capabilityLabels[capability.value] : '')
const memoryLoading = computed(() => memory.state.value.loading)
const canSubmit = computed(() => !sending.value && !memoryLoading.value && Boolean(input.value.trim()))
function asRecord(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : undefined
}
function planArtifactIds(plan: Record<string, unknown>): string[] {
  const steps = Array.isArray(plan.steps) ? plan.steps : []
  return [...new Set(steps.flatMap(step => {
    const parameters = asRecord(asRecord(step)?.parameters)
    const ids = parameters?.artifact_ids
    return Array.isArray(ids) ? ids.filter(id => typeof id === 'string' && id.trim()) as string[] : []
  }))]
}
function canConfirmPlan(message: ChatMessage): boolean {
  return Boolean(message.plan && message.rawPlan && message.plan.mode === 'composite'
    && message.plan.needs_confirmation && !message.plan.needs_clarification
    && !message.planSubmitted && !message.planSubmitting)
}
async function confirmPlan(message: ChatMessage) {
  if (!canConfirmPlan(message) || !message.rawPlan) return
  message.planSubmitting = true
  try {
    await v2Api.tasks.submitComposite(
      message.rawPlan,
      memory.projectId.value || null,
      planArtifactIds(message.rawPlan),
      globalThis.crypto?.randomUUID?.(),
    )
    message.planSubmitted = true
  } catch (error) {
    message.content += `\n❌ 创建任务失败：${apiErrorMessage(error, '请稍后重试')}`
  } finally {
    message.planSubmitting = false
  }
}
watch(() => [route.path, route.query.capability], ([path, value]) => {
  // Clear a stale embedded panel when the user returns to /qa without the
  // capability query.  Leaving the old panel mounted made the page appear to
  // have a second, unrelated composer and was especially confusing after a
  // navigation while a request was in flight.
  capability.value = path === '/qa' && isCapability(value) ? value : ''
})
function openCapability(target: Capability) {
  capability.value = target
  void router.replace({ path: '/qa', query: { ...route.query, capability: target } })
}
function clearCapability() {
  capability.value = ''
  const { capability: _previous, ...query } = route.query
  void router.replace({ path: '/qa', query })
}
function activateAction(target: ActionTarget) {
  if (target === 'bim') void router.push('/bim')
  else openCapability(target)
}
function onCompositionStart() { composing.value = true }
function onCompositionEnd() { composing.value = false }
function onEnter(event: KeyboardEvent) {
  // Chromium/Edge can report the commit key as composing even after the IME
  // has emitted compositionend.  In that case keyup below submits once the
  // text is committed; never submit while the candidate list is active.
  if (composing.value || event.isComposing) return
  event.preventDefault()
  void send()
}
function onKeyup(event: KeyboardEvent) {
  if (event.key !== 'Enter' || composing.value || event.isComposing) return
  // A normal keydown already submitted.  send() is idempotent while the
  // request is active, so this is only a fallback for IME commit events whose
  // keydown was marked composing.
  void send()
}
function actionFromEvent(value: unknown): ChatAction[] {
  if (!value || typeof value !== 'object') return []
  const event = value as { capability?: unknown; action_label?: unknown }
  const target = event.capability
  if (target !== 'bim' && !isCapability(target)) return []
  return [{ target, label: typeof event.action_label === 'string' && event.action_label
    ? event.action_label : target === 'bim' ? '打开 BIM Agent' : capabilityLabels[target] }]
}
// KeepAlive allows streaming across navigation; only logout/unmount or a scope change aborts it.
let streamAbort: AbortController | null = null
let sendGeneration = 0
watch([memory.key, memory.sessionId], () => {
  ++sendGeneration
  streamAbort?.abort()
  messages.value = []
  // Do not erase a draft while the project/session store finishes its
  // asynchronous refresh.  This was the main reason a question could appear
  // to vanish immediately before the user pressed Send.
  sending.value = false
}, { flush: 'sync' })
function restore(detail: MemoryDetail) {
  if (sending.value) return
  messages.value = detail.turns.flatMap(t => {
    const reply: ChatMessage = { role: 'assistant', content: t.answer }
    if (t.result?.task_plan) {
      try {
        reply.plan = parseTaskPlan(t.result.task_plan)
        reply.rawPlan = asRecord(t.result.task_plan)
        reply.actions = taskPlanActions(reply.plan)
      } catch {
        reply.content += '\n历史计划格式已不兼容，请重新描述任务；未执行业务操作。'
      }
    }
    return [{ role: 'user', content: t.user_text }, reply]
  })
}

onBeforeUnmount(() => {
  streamAbort?.abort()
})

async function send() {
  const msg = input.value.trim()
  // The memory panel initially resolves a durable session asynchronously.
  // Do not send against its temporary ID; keep the draft in the input until
  // the session is stable instead of losing the question on hydration.
  if (!msg || sending.value || memoryLoading.value) return
  const generation = ++sendGeneration
  input.value = ''
  messages.value.push({ role: 'user', content: msg })
  messages.value.push({ role: 'assistant', content: '思考中...' })
  const thinking = messages.value[messages.value.length - 1]!
  sending.value = true
  try {
    // Keep the UI usable in embedded/older webviews without AbortController.
    // chatStream has the same fallback; cancellation is an enhancement, not a
    // prerequisite for sending a question.
    streamAbort = typeof globalThis.AbortController === 'function'
      ? new globalThis.AbortController() : null
    let tokenStarted = false
    let streamFailed = false
    await chatStream(memory.sessionId.value, msg, (e) => {
      if (generation !== sendGeneration) return
      if (e.type === 'task_plan') {
        // A malformed/old plan must not abort the whole SSE consumer and leave
        // the composer permanently busy.  Keep the server response visible and
        // ask the user to retry instead.
        try {
          thinking.plan = parseTaskPlan(e.plan)
          thinking.rawPlan = asRecord(e.plan)
          thinking.content = typeof e.message === 'string' ? e.message : '请确认计划并在对应面板补齐资料。'
          thinking.actions = taskPlanActions(thinking.plan)
        } catch {
          thinking.plan = undefined
          thinking.actions = []
          thinking.content = '❌ 任务计划格式无效，请重新描述问题'
        }
      } else if (e.type === 'token') {
        thinking.content = tokenStarted ? thinking.content + e.content : e.content
        tokenStarted = true
      } else if (e.type === 'progress') {
        thinking.content = '⏳ ' + e.stage
      } else if (e.type === 'meta') {
        thinking.content += '\n\n📚 来源：' + (e.sources || []).join('、')
      } else if (e.type === 'guidance') {
        thinking.content = e.message
        thinking.actions = actionFromEvent(e)
        // A late SSE response must never navigate away from an active BIM task.
        if (route.path === '/qa' && isCapability(e.capability)) openCapability(e.capability)
      } else if (e.type === 'pipeline_plan') {
        thinking.content = [e.title, e.intro].filter(Boolean).join('\n')
        thinking.actions = Array.isArray(e.steps) ? e.steps.flatMap(actionFromEvent) : []
      } else if (e.type === 'error') {
        streamFailed = true
        thinking.content = '❌ ' + e.message
      }
    }, streamAbort?.signal, memory.projectId.value)
    if (streamFailed && generation === sendGeneration && !input.value.trim()) {
      input.value = msg
    }
  } catch (e) {
    if (generation === sendGeneration && !(e instanceof Error && e.name === 'AbortError')) {
      thinking.content = '❌ 连接异常：' + apiErrorMessage(e, '请检查网络后重试')
      // Keep the exact draft available for one-click retry instead of making
      // the user retype a long question after a transient backend restart.
      if (!input.value.trim()) input.value = msg
    }
  } finally {
    // Always release the composer, including callback/transport failures and
    // requests cancelled by a session/project switch.
    if (generation === sendGeneration) {
      sending.value = false
      streamAbort = null
      await nextTick()
      chatBox.value?.scrollTo({ top: chatBox.value.scrollHeight })
    }
  }
}
</script>
