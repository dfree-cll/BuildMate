import assert from 'node:assert/strict'
import { test } from 'node:test'
import { runInNewContext } from 'node:vm'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

const compiled = await build({
  entryPoints: [fileURLToPath(new URL('../src/utils/taskPlan.ts', import.meta.url))],
  bundle: true, write: false, platform: 'node', format: 'cjs',
})
const module = { exports: {} }
runInNewContext(compiled.outputFiles[0].text, { module, exports: module.exports })
const { parseTaskPlan, taskPlanActions, taskRouteLabel, missingParameterLabel } = module.exports
const plan = () => ({
  mode: 'composite', needs_confirmation: true, needs_clarification: true,
  steps: [
    { step: 1, domain: 'contract', route: 'workflow.contract_review', missing_parameters: ['artifact_ids'] },
    { step: 2, domain: 'negotiation', route: 'workflow.negotiation', missing_parameters: [] },
    { step: 3, domain: 'bim', route: 'clarify', missing_parameters: ['project_id'] },
  ],
})

test('registered plan renders human-readable labels and only available panel actions', () => {
  const parsed = parseTaskPlan(plan())
  assert.equal(taskRouteLabel(parsed.steps[0]), '合同审核')
  assert.equal(missingParameterLabel('artifact_ids'), '关联文件')
  assert.deepEqual(Array.from(taskPlanActions(parsed), action => action.target), ['negotiation', 'bim'])
})

test('malformed plan, unknown route and out-of-order steps fail closed', () => {
  const invalid = [null, {}, { ...plan(), steps: [] }, { ...plan(), needs_confirmation: 'yes' }]
  for (const patch of [
    { domain: 'admin' }, { route: 'run_script' }, { missing_parameters: null }, { step: 7 },
  ]) {
    invalid.push({ ...plan(), steps: [{ ...plan().steps[0], ...patch }] })
  }
  for (const value of invalid) assert.throws(() => parseTaskPlan(value), /任务/)
})

test('large plans are rejected and duplicate panel actions are deduplicated', () => {
  assert.throws(() => parseTaskPlan({ ...plan(), steps: Array(21).fill(plan().steps[0]) }), /任务/)
  const repeated = plan()
  repeated.steps.push({ ...repeated.steps[1], step: 4 })
  assert.equal(taskPlanActions(parseTaskPlan(repeated)).length, 2)
})
