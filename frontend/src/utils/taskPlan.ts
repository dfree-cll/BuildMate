/** Read-only plan cards are never executable commands or approval records. */
export const taskDomainLabels = {
  knowledge: '知识查询', bim: 'BIM 建模', bid: '投标审查 / 查询',
  contract: '合同审核', procurement: '采购分析', negotiation: '谈判辅助',
}
const routeLabels = {
  'rag.answer': '依据资料回答', 'bid.search': '查询标书',
  'workflow.bid_review': '投标审查', 'workflow.contract_review': '合同审核',
  'workflow.procurement': '采购分析', 'workflow.negotiation': '谈判辅助',
  'workflow.wall_pipeline': '独立 BIM 流程', clarify: '需要补充资料',
}
export interface TaskPlanSummary {
  mode: 'single' | 'composite'
  needs_confirmation: boolean
  needs_clarification: boolean
  steps: {
    step: number
    domain: keyof typeof taskDomainLabels
    route: keyof typeof routeLabels
    missing_parameters: string[]
  }[]
}
function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}
export function parseTaskPlan(value: unknown): TaskPlanSummary {
  if (!record(value) || !['single', 'composite'].includes(String(value.mode))
    || typeof value.needs_confirmation !== 'boolean' || typeof value.needs_clarification !== 'boolean'
    || !Array.isArray(value.steps) || !value.steps.length || value.steps.length > 20) {
    throw new Error('任务计划格式无效，请重试')
  }
  for (const [index, step] of value.steps.entries()) {
    if (!record(step) || step.step !== index + 1
      || typeof step.domain !== 'string' || !Object.hasOwn(taskDomainLabels, step.domain)
      || typeof step.route !== 'string' || !Object.hasOwn(routeLabels, step.route)
      || !Array.isArray(step.missing_parameters) || !step.missing_parameters.every(item => typeof item === 'string')) {
      throw new Error('任务步骤格式无效，请重试')
    }
  }
  return value as unknown as TaskPlanSummary
}
export function taskRouteLabel(step: TaskPlanSummary['steps'][number]): string {
  return routeLabels[step.route]
}
export function missingParameterLabel(value: string): string {
  return ({ artifact_ids: '关联文件', project_id: '项目' } as Record<string, string>)[value] || '业务参数'
}
export function taskPlanActions(plan: TaskPlanSummary) {
  const targets = { bid: 'bid_review', procurement: 'procurement', negotiation: 'negotiation', bim: 'bim' } as const
  return [...new Set(plan.steps.map(step => step.domain))].flatMap(domain => {
    if (!Object.hasOwn(targets, domain)) return []
    const target = targets[domain as keyof typeof targets]
    return [{ target, label: `打开${taskDomainLabels[domain]}面板` }]
  })
}
