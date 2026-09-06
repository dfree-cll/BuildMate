<template>
  <AgentMemoryPanel agent="drawing2bim" :disabled="wallRunning" @restored="restoreBimMemory" />
  <p v-if="bimMemory.state.value.loading" class="qa-memory-status" role="status">正在恢复 BIM 会话；已填写的建模参数不会被覆盖。</p>
  <div style="margin-bottom:12px">
    <el-button :disabled="wallRunning" @click="saveBimMemory">记住当前建模设置</el-button>
    <el-button :disabled="wallRunning || !bimMemory.state.value.detail.preferences.bim" @click="applyBimMemory">填入已记住的设置</el-button>
    <small>仅填入表单；请核对楼层、标高、比例和目标模型后再提交。不会继承旧审批。</small>
  </div>
  <el-card>
    <template #header>🏗️ BIM Agent（图纸 → Revit 交付）</template>
    <el-steps :active="stepActive" align-center finish-status="success" style="margin-bottom:16px">
      <el-step title="上传图纸" description="PDF / DWG / DXF" />
      <el-step title="确定性识别" description="轴网 + 墙柱 + 拓扑" />
      <el-step title="人工审核" description="WallModel / 写入" />
      <el-step title="Revit 交付" description="Bridge + 独立验收" />
    </el-steps>

    <el-card shadow="never" style="margin-bottom:16px; border:1px solid #b3d8ff">
      <template #header>
        <span>🧱 通用墙体建模链路（PDF / DWG / DXF → WallEvidence → WallModel → Revit）</span>
        <el-tag type="success" size="small" style="margin-left:8px">唯一 BIM 入口</el-tag>
        <el-tag type="info" size="small" style="margin-left:8px">国标建模：GB/T 51212 / 51269 / 51301 / 51235</el-tag>
        <el-tag type="warning" size="small" style="margin-left:8px">Revit 2020 交付 · mm</el-tag>
      </template>
      <el-alert v-if="!project.currentId" type="warning" :closable="false" style="margin-bottom:12px"
                title="请先选择项目；如果项目列表为空，可以创建一个演示项目" />
      <div v-if="!project.currentId" style="display:flex; gap:8px; margin-bottom:12px">
        <el-input v-model="wallProjectName" placeholder="演示项目名称" style="max-width:320px" />
        <el-button type="primary" :loading="wallProjectCreating" @click="createWallProject">创建项目</el-button>
      </div>
      <el-upload drag :auto-upload="false" :on-change="onWallFiles" :limit="5" multiple
                 accept=".dwg,.dxf,.pdf" style="margin-bottom:12px">
        <div style="padding:20px">📎 拖拽或点击上传墙体图纸（PDF / DWG / DXF，可多选同一格式族）</div>
      </el-upload>
      <div style="font-size:12px; color:#606266; margin-bottom:12px">
        处理顺序：来源校验 → 轴网/坐标 → 墙柱几何 → 去重与 T/L/Z 拓扑 → 静态审核门禁 → Dry-run → 人工批准 → Revit 2020 Bridge → 独立叠图验收。DWG 需要已配置 ODA Converter。
      </div>
      <el-alert type="info" :closable="false" style="margin-bottom:12px"
                title="本流程按 GB/T 51212-2016、GB/T 51269-2017、GB/T 51301-2018、GB/T 51235-2017 生成模型；单位、坐标、分类编码、构件参数、工程量和来源证据会写入 WallModel 与 Revit 构件信息。墙厚和柱截面优先取图例、构件表和大样，几何只做校验。" />
      <el-form inline label-position="top" style="margin-bottom:4px">
        <el-form-item label="Revit 源模型路径（交付后生成隔离副本）" style="margin-bottom:8px">
          <el-input v-model="wallTargetModelPath" style="width:420px"
                    placeholder="例如：D:\\Tools\\BimRvt\\rvt_out\\project.rvt" />
        </el-form-item>
        <el-form-item label="楼层编码（可选，标高区间可自动判断）" style="margin-bottom:8px">
          <el-input v-model="wallFloorCode" style="width:180px" placeholder="留空自动判断 B1" />
        </el-form-item>
        <el-form-item label="本层标高范围（m）" style="margin-bottom:8px">
          <el-input v-model="wallElevationRange" style="width:180px" placeholder="例如：-6.4~0" />
        </el-form-item>
        <el-form-item label="墙柱材料（项目输入）" style="margin-bottom:8px">
          <el-input v-model="wallMaterialName" style="width:260px" placeholder="例如：钢筋混凝土 C40" />
        </el-form-item>
        <el-form-item label="洞底标高基准" style="margin-bottom:8px">
          <el-select v-model="openingElevationReference" style="width:210px">
            <el-option label="项目 ±0.000" value="project" />
            <el-option label="相对本层底标高" value="level" />
            <el-option label="未核定（按图纸说明判断）" value="unresolved" />
          </el-select>
        </el-form-item>
        <el-form-item label="本次洞底标高校正（可选，m）" style="margin-bottom:8px">
          <el-input v-model="openingElevationCorrections" style="width:330px"
                    placeholder="例如 JD3=-2.200；多个用分号分隔" />
        </el-form-item>
        <el-form-item v-if="wallHasPdf" label="PDF 出图比例（可选覆盖，1:）" style="margin-bottom:8px">
          <el-input-number v-model="wallPdfScaleDenominator" :min="1" :max="5000" :step="10"
                           controls-position="right" placeholder="自动读取标题栏" style="width:180px" />
        </el-form-item>
        <el-form-item label="接续上一交付任务（可选）" style="margin-bottom:8px">
          <el-input v-model="wallContinueTaskId" style="width:330px"
                    placeholder="留空表示新建；可接续最近一次成功交付" />
          <el-button v-if="lastWallTaskId" link type="primary" style="margin-left:6px"
                     @click="wallContinueTaskId = lastWallTaskId">使用最近一次成功交付</el-button>
        </el-form-item>
      </el-form>
      <div style="font-size:12px; color:#606266; margin-bottom:12px">
        洞口按图纸文字说明确定标高基准，并关联表格中的宽×高及洞底标高；若 OCR 漏掉负号，地下层会依据“相对于 ±0.000”的说明自动恢复为负值。校正值只用于本次任务并保留审批记录。
        平面视图只显示切平面经过的洞口，完整洞口请在交付模型的 BM-3D-楼层编码 三维视图查看。
      </div>
      <div v-if="wallElevationPreview" style="font-size:12px; color:#606266; margin:-2px 0 12px">
        <b>标高范围：</b>{{ wallElevationPreview.bottom.toFixed(3) }}m
        ～ {{ wallElevationPreview.top.toFixed(3) }}m，
        本层高度 {{ wallElevationPreview.height.toFixed(3) }}m
        <span v-if="wallElevationPreview.floorCode" style="margin-left:8px">
          · 自动识别为 {{ wallElevationPreview.floorCode === 'B1' ? '地下室一层' : '地上一层' }}（{{ wallElevationPreview.floorCode }}）
        </span>
      </div>
      <div style="font-size:12px; color:#909399; margin-bottom:12px">
        目标模型由 Bridge 在 Revit 2020 当前打开的工作副本中写入；原始 RVT 不会被直接覆盖。请填写本层标高范围（例如 -6.4~0，单位 m）；墙柱底/顶和构件高度均基于该范围生成，Revit 交付参数统一使用毫米（mm）。PDF 默认从主平面标题栏读取比例，有多种比例无法判定时再人工填写覆盖值，DWG/DXF 使用文件单位。
      </div>
      <el-button type="primary" :loading="wallRunning"
                 :disabled="!wallFiles.length || !project.currentId || bimMemory.state.value.loading"
                 @click="submitWallPipeline">启动 BIM Agent</el-button>

      <el-card v-if="wallTask" shadow="never" style="margin-top:16px; background:#f8fbff">
        <template #header>
          <span>任务 {{ wallTask.id }}</span>
          <el-tag :type="wallTaskTagType(wallTask.status)" size="small" style="margin-left:8px">
            {{ wallTaskStatusLabel(wallTask.status) }}
          </el-tag>
          <el-tag v-if="wallStructured.stage" type="info" size="small" style="margin-left:6px">
            {{ wallStageLabel(wallStructured.stage) }}
          </el-tag>
        </template>
        <el-alert v-if="wallError" type="error" :closable="false" :title="wallError" style="margin-bottom:12px" />
        <div v-if="wallTask.result?.answer" style="margin-bottom:12px; white-space:pre-wrap">
          <b>系统反馈：</b>{{ wallTask.result.answer }}
        </div>
        <el-descriptions v-if="wallStructured.pipeline === 'wall_pipeline'" :column="4" border size="small" style="margin-bottom:12px">
          <el-descriptions-item label="墙体">{{ wallStructured.wall_count ?? '-' }}</el-descriptions-item>
          <el-descriptions-item label="剪力墙">{{ wallStructured.shear_wall_count ?? '-' }}</el-descriptions-item>
          <el-descriptions-item label="建筑墙">{{ wallStructured.architectural_wall_count ?? '-' }}</el-descriptions-item>
          <el-descriptions-item label="类型待核定">{{ wallStructured.unresolved_wall_count ?? '-' }}</el-descriptions-item>
          <el-descriptions-item label="柱">{{ wallStructured.column_count ?? '-' }}</el-descriptions-item>
          <el-descriptions-item label="异形柱">{{ wallStructured.irregular_column_count ?? '-' }}</el-descriptions-item>
          <el-descriptions-item label="连梁">{{ wallStructured.beam_count ?? '-' }}</el-descriptions-item>
          <el-descriptions-item label="连接点">{{ wallStructured.junction_count ?? '-' }}</el-descriptions-item>
          <el-descriptions-item label="洞口">{{ wallStructured.opening_count ?? '-' }}</el-descriptions-item>
          <el-descriptions-item label="洞口已匹配">{{ wallStructured.opening_geometry_match_count ?? '-' }}</el-descriptions-item>
          <el-descriptions-item label="具备开洞条件">{{ wallStructured.opening_cut_ready_count ?? '-' }}</el-descriptions-item>
          <el-descriptions-item label="洞口待核定">{{ wallStructured.opening_cut_pending_count ?? '-' }}</el-descriptions-item>
          <el-descriptions-item label="Revit 实际开洞">{{ wallStructured.revit_result?.readback?.opening_semantics?.cut_count ?? '-' }}</el-descriptions-item>
        </el-descriptions>
        <div v-if="wallStructured.modeling_standard" style="font-size:12px; color:#606266; margin-bottom:12px">
          <b>建模标准：</b>{{ wallStructured.modeling_standard.profile || 'cn_gb_bim_delivery_v1' }}
          · {{ (wallStructured.modeling_standard.references || []).join('、') }}
        </div>
        <el-alert v-if="(wallStructured.unresolved_wall_count || 0) > 0"
                  type="warning" :closable="false" style="margin-bottom:12px"
                  title="部分墙体缺少可追溯的结构/建筑语义证据，已标记为‘类型待核定’，不会自动按剪力墙写入 Revit；请核对图层或结构图纸后再审批。" />
        <div v-if="wallStructured.gate?.metrics" style="font-size:12px; color:#606266; margin-bottom:12px">
          <b>图纸规格：</b>已解析 {{ wallStructured.gate.metrics.specification_resolved_count ?? 0 }} 个，
          未解析 {{ wallStructured.gate.metrics.specification_unresolved_count ?? 0 }} 个，
          冲突 {{ wallStructured.gate.metrics.specification_conflict_count ?? 0 }} 个。
          <span style="margin-left:6px">图例/表格/大样原文和来源会写入构件信息。</span>
        </div>
        <div v-if="wallScaleDiagnostic" style="font-size:12px; color:#606266; margin-bottom:12px">
          <b>图纸比例：</b>{{ wallScaleDiagnostic.scale }}（从主平面标题栏识别）
          <span v-if="wallScaleDiagnostic.candidate_denominators?.length" style="margin-left:6px">
            检测到：{{ wallScaleDiagnostic.candidate_denominators.map(value => `1:${value}`).join('、') }}
          </span>
        </div>
        <el-alert v-if="(wallStructured.gate?.metrics?.specification_conflict_count || 0) > 0"
                  type="error" :closable="false" style="margin-bottom:12px"
                  title="图纸规格与几何测量存在冲突，已禁止自动交付；请核对图例/表格/大样后重新审核。" />
        <el-alert v-else-if="(wallStructured.gate?.metrics?.specification_unresolved_count || 0) > 0"
                  type="warning" :closable="false" style="margin-bottom:12px"
                  title="部分构件没有找到可追溯的图例、构件表、大样或明确规格标注；系统保留几何实测值并标记为“规格待核定”，不会自动四舍五入成正式规格。" />
        <el-alert v-if="wallRiskDiagnostics.length" type="warning" :closable="false"
                  style="margin-bottom:12px" title="交付前风险提示">
          <div v-for="item in wallRiskDiagnostics" :key="`${item.code}-${item.element_id || ''}`"
               style="line-height:1.6">{{ item.detail || item.code }}</div>
        </el-alert>
        <el-alert v-if="wallStructured.gate?.status === 'fail'" type="error" :closable="false"
                  :title="'几何审核门禁未通过：' + ((wallStructured.gate.errors || []).join('；') || '请检查原图和配置')"
                  style="margin-bottom:12px" />
        <el-table v-if="wallSteps.length" :data="wallSteps" size="small" style="margin-bottom:12px">
          <el-table-column prop="position" label="#" width="55" />
          <el-table-column prop="name" label="步骤" />
          <el-table-column prop="status" label="状态" width="130" />
          <el-table-column prop="attempts" label="尝试" width="70" />
          <el-table-column prop="error_message" label="错误" />
        </el-table>
        <div v-if="Object.keys(wallArtifactIds).length" style="font-size:12px; margin-bottom:12px">
          <b>可追溯产物：</b>
          <span v-for="(artifactId, filename) in wallArtifactIds" :key="filename" style="margin-left:8px">
            {{ filename }}
            <el-button link type="primary" size="small" @click="downloadWallArtifact(String(artifactId), String(filename))">下载</el-button>
          </span>
        </div>
        <el-alert v-if="wallTask.status === 'succeeded' && (wallStructured.stage === 'delivery_complete' || wallDeliveryRvtPath || wallArtifactIds['revit_output.rvt'])"
                  type="success" :closable="false" style="margin-bottom:12px">
          <template #title>Revit 交付文件已生成（请打开/下载这个文件，不是原始模板）</template>
          <div style="font-size:12px; line-height:1.7">
            <div v-if="wallDeliveryRvtPath">
              <b>当前交付文件：</b>{{ wallDeliveryRvtPath }}
              <el-button link type="primary" size="small" @click="copyWallDeliveryPath">复制路径</el-button>
            </div>
            <div>
              墙 {{ wallDeliveryCounts.walls ?? '-' }} 面、柱 {{ wallDeliveryCounts.columns ?? '-' }} 根、轴网 {{ wallDeliveryCounts.grids ?? '-' }} 条。
              Revit 不会覆盖输入的原始 RVT；请在 Revit 2020 中打开下面的“交付 RVT”，或切换到标题包含当前任务 ID 的隔离工作副本。
            </div>
            <el-button v-if="wallArtifactIds['revit_output.rvt']" type="primary" size="small" style="margin-top:6px"
                       @click="downloadWallArtifact(String(wallArtifactIds['revit_output.rvt']), 'buildmate_delivery.rvt')">
              下载交付 RVT（下载后在 Revit 2020 打开）
            </el-button>
            <el-button v-if="wallArtifactIds['revit_actual_view.png']" link type="primary" size="small" style="margin-top:6px"
                       @click="downloadWallArtifact(String(wallArtifactIds['revit_actual_view.png']), 'revit_actual_view.png')">
              下载 Revit 实际视图
            </el-button>
          </div>
        </el-alert>
        <el-alert v-else-if="wallTask.status === 'succeeded'" type="warning" :closable="false"
                  title="任务状态已成功，但尚未找到 Revit 交付文件；请刷新任务或检查 Bridge 回传。"
                  style="margin-bottom:12px" />
        <el-alert v-if="wallTask.status === 'succeeded' && wallStructured.stage === 'revit_bridge_handoff'"
                  type="warning" :closable="false" style="margin-bottom:12px"
                  title="Bridge 交接物已生成；请确认 Revit 2020 Bridge 已回传实际视图和独立叠图验收结果。" />
        <div v-if="wallTask.status === 'waiting_human' && canWallApprove
          && ['approve_wall_model', 'approve_revit_write'].includes(wallNextAction)">
          <el-button v-if="wallNextAction === 'approve_wall_model'" type="success" :loading="wallRunning"
                     @click="resumeWall('approved')">✅ 批准 WallModel 并执行 Dry-run</el-button>
          <el-button v-if="wallNextAction === 'approve_wall_model'" type="danger" :loading="wallRunning"
                     @click="resumeWall('rejected')">❌ 驳回 WallModel</el-button>
          <el-button v-if="wallNextAction === 'approve_revit_write'" type="success" :loading="wallRunning"
                     @click="resumeWall('approved')">✅ 批准 Revit 写入</el-button>
          <el-button v-if="wallNextAction === 'approve_revit_write'" type="danger" :loading="wallRunning"
                     @click="resumeWall('rejected')">❌ 拒绝 Revit 写入</el-button>
        </div>
        <el-alert v-else-if="wallTask.status === 'waiting_human' && wallNextAction === 'fix_source_or_rules'"
                  type="error" :closable="false"
                  title="确定性门禁未通过：当前没有可审批模型，请更换建筑/结构平面图或调整配置后重新提交。" />
        <el-alert v-else-if="wallTask.status === 'waiting_human'" type="warning" :closable="false"
                  title="任务已暂停等待人工审批，请使用管理员、项目或审核员角色处理。" />
      </el-card>
    </el-card>
  </el-card>
</template>

<script setup lang="ts">
import { ref, computed, onBeforeUnmount, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { apiErrorMessage } from '../utils/apiError'
import { v2Api } from '../api/v2'
import { useAuthStore } from '../stores/auth'
import { useArtifactStore } from '../stores/artifact'
import { useProjectStore } from '../stores/project'
import { useTaskStore } from '../stores/task'
import AgentMemoryPanel from '../components/AgentMemoryPanel.vue'
import { useAgentSession, type MemoryDetail } from '../stores/agentMemory'
const bimMemory = useAgentSession('drawing2bim')

const auth = useAuthStore()
const project = useProjectStore()
const artifactStore = useArtifactStore()
const taskState = useTaskStore()
const canWallApprove = computed(() => ['admin', 'project', 'reviewer'].includes(auth.user?.role || ''))

const wallFiles = ref<File[]>([])
const wallRunning = ref(false)
const wallTaskId = ref('')
const wallError = ref('')
const wallProjectName = ref('BuildMate BIM 演示项目')
const wallProjectCreating = ref(false)
const wallTargetModelPath = ref('data/revit/buildmate_demo.rvt')
const wallFloorCode = ref('')
const wallElevationRange = ref('')
const openingElevationReference = ref<'project' | 'level' | 'unresolved'>('project')
const openingElevationCorrections = ref('')
const wallMaterialName = ref('钢筋混凝土（强度等级未注明）')
const wallPdfScaleDenominator = ref<number | null>(null)
const lastWallTaskId = ref('')
const wallContinueTaskId = ref('')
function restoreBimMemory(detail: MemoryDetail) {
  wallPollGeneration += 1
  wallTaskId.value = ''
  wallError.value = ''
  const last = detail.turns[detail.turns.length - 1]
  lastWallTaskId.value = typeof last?.result.task_id === 'string' ? last.result.task_id : ''
  wallContinueTaskId.value = '' // Continuation remains an explicit user choice.
}
async function saveBimMemory() {
  try {
    await bimMemory.savePreferences({ ...bimMemory.state.value.detail.preferences, bim: {
      target_model_path: wallTargetModelPath.value, floor_code: wallFloorCode.value,
      elevation_range: wallElevationRange.value, material_name: wallMaterialName.value,
      pdf_scale_denominator: wallPdfScaleDenominator.value,
    } })
    ElMessage.success('已保存当前会话的建模设置')
  } catch (e) { ElMessage.error(apiErrorMessage(e, '保存设置失败')) }
}
function applyBimMemory() {
  const settings = bimMemory.state.value.detail.preferences.bim
  if (!settings) return
  wallTargetModelPath.value = settings.target_model_path
  wallFloorCode.value = settings.floor_code
  wallElevationRange.value = settings.elevation_range
  wallMaterialName.value = settings.material_name
  wallPdfScaleDenominator.value = settings.pdf_scale_denominator
  ElMessage.info('已填入历史设置，请核对本次图纸后再提交')
}
let wallPollGeneration = 0
const BIM_TASK_STORAGE_PREFIX = 'buildmate:bim:active-task:'
let wallTaskProjectId = ''

function bimTaskStorageKey() {
  return project.currentId ? `${BIM_TASK_STORAGE_PREFIX}${project.currentId}` : ''
}

function persistWallTaskId(taskId: string) {
  const key = bimTaskStorageKey()
  if (key && taskId) localStorage.setItem(key, taskId)
}

async function restorePersistedWallTask() {
  if (wallTaskId.value) return
  const key = bimTaskStorageKey()
  const taskId = key ? localStorage.getItem(key) : ''
  if (!taskId) return
  wallTaskId.value = taskId
  try {
    const latest = await refreshWallTask(taskId)
    if (!latest) {
      localStorage.removeItem(key)
      wallTaskId.value = ''
      return
    }
    if (['queued', 'running', 'resumed'].includes(String(latest.status))) {
      await waitForWallTask(taskId)
    }
  } catch (error: any) {
    wallError.value = `任务 ${taskId} 已恢复，但暂时无法读取状态：${apiErrorMessage(error, '网络错误')}`
  }
}

onBeforeUnmount(() => {
  // Invalidate any in-flight polling loop so a route change cannot mutate
  // stores or display stale errors after this component has been destroyed.
  wallPollGeneration += 1
})
const wallHasPdf = computed(() => wallFiles.value.some(file => file.name.toLowerCase().endsWith('.pdf')))
type ElevationRangePreview = {
  bottom: number
  top: number
  height: number
  floorCode: string | null
}
function parseElevationRange(value: string): ElevationRangePreview | null {
  const match = String(value || '').replace(/−/g, '-').trim().match(
    /^([+-]?(?:\d+(?:\.\d+)?|\.\d+))\s*(?:m|米)?\s*(?:~|～|至|到)\s*([+-]?(?:\d+(?:\.\d+)?|\.\d+))\s*(?:m|米)?$/i,
  )
  if (!match) return null
  const bottom = Number(match[1])
  const top = Number(match[2])
  if (!Number.isFinite(bottom) || !Number.isFinite(top) || top <= bottom || top - bottom > 30) return null
  const floorCode = bottom < -0.01 && Math.abs(top) <= 0.01
    ? 'B1'
    : Math.abs(bottom) <= 0.01 && top > 0.01 ? '1F' : null
  return { bottom, top, height: top - bottom, floorCode }
}
const wallElevationPreview = computed(() => parseElevationRange(wallElevationRange.value))
const wallTask = computed(() => wallTaskId.value ? taskState.tasks[wallTaskId.value] || null : null)
const wallSteps = computed(() => wallTaskId.value ? taskState.steps[wallTaskId.value] || [] : [])
const wallStructured = computed<Record<string, any>>(() => {
  const value = wallTask.value?.result?.structured_output
  return value && typeof value === 'object' ? value : {}
})
const wallArtifactIds = computed<Record<string, string>>(() => {
  const value = wallStructured.value.artifact_ids || wallStructured.value.failure_artifact_ids
  return value && typeof value === 'object' ? value : {}
})
const wallDeliveryRvtPath = computed(() => String(
  wallStructured.value.revit_result?.readback?.output_path
  || wallStructured.value.revit_result?.readback?.output_rvt_path
  || '',
))
const wallDeliveryCounts = computed<Record<string, number | null>>(() => {
  const value = wallStructured.value.revit_presentation?.visible_counts
  return value && typeof value === 'object' ? value : {}
})
const wallScaleDiagnostic = computed<Record<string, any> | null>(() => {
  const diagnostics = wallStructured.value.gate?.diagnostics
  if (!Array.isArray(diagnostics)) return null
  return diagnostics.find((item: any) => item?.code === 'PDF_SCALE_INFERRED') || null
})
const wallRiskDiagnostics = computed<any[]>(() => {
  const diagnostics = wallStructured.value.gate?.diagnostics
  if (!Array.isArray(diagnostics)) return []
  return diagnostics.filter((item: any) => item?.severity === 'warning'
    && ['COLUMN_DETAIL_SOURCE_MISSING', 'COLUMN_DETAIL_ASSOCIATION_UNRESOLVED',
      'LEVEL_ELEVATION_UNRESOLVED', 'BEAM_SPECIFICATION_UNRESOLVED', 'OPENING_CUT_UNRESOLVED'].includes(item?.code)).slice(0, 8)
})
const wallNextAction = computed(() => String(wallTask.value?.result?.next_action || ''))
const WALL_STAGE_LABELS: Record<string, string> = {
  wall_model_review: '待审核 WallModel',
  revit_dry_run: 'Dry-run 已通过，待批准写入',
  revit_bridge_handoff: '已生成 Bridge 交接物',
  delivery_complete: 'Revit 交付完成',
}

const stepActive = computed(() => {
  if (wallTask.value?.status === 'succeeded') return 3
  // Both persisted HITL gates belong to the third visual step.  Keeping
  // `approve_wall_model` at index 1 makes a successfully recognized model
  // look as if deterministic recognition is still stuck.
  if (wallTask.value?.status === 'waiting_human'
      && ['approve_wall_model', 'approve_revit_write'].includes(wallNextAction.value)) return 2
  if (wallTask.value || wallFiles.value.length || wallRunning.value) return 1
  return 0
})
function wallTaskStatusLabel(status: string) {
  return ({ queued: '排队中', running: '处理中', resumed: '恢复处理中', waiting_human: '等待人工审批',
    succeeded: '已完成', failed: '失败', canceled: '已取消' } as Record<string, string>)[status] || status
}
function wallTaskTagType(status: string) {
  if (status === 'succeeded') return 'success'
  if (status === 'failed' || status === 'canceled') return 'danger'
  if (status === 'waiting_human') return 'warning'
  return 'info'
}
function wallStageLabel(stage: string) { return WALL_STAGE_LABELS[stage] || stage }

function onWallFiles(_file: any, fileList: any[]) {
  wallFiles.value = fileList.map((item: any) => item.raw).filter(Boolean)
  const names = wallFiles.value.map(file => file.name)
  const basement = names.map(name => {
    const latin = name.match(/(?:^|[^A-Z0-9])B(\d+)(?:[^A-Z0-9]|$)/i)
    if (latin) return `B${Number(latin[1])}`
    const chinese = name.match(/地下([一二三四五六七八九十\d]+)层/)
    if (!chinese) return ''
    const digits: Record<string, number> = {
      一: 1, 二: 2, 三: 3, 四: 4, 五: 5,
      六: 6, 七: 7, 八: 8, 九: 9, 十: 10,
    }
    const value = /^\d+$/.test(chinese[1]) ? Number(chinese[1]) : digits[chinese[1]]
    return value ? `B${value}` : ''
  }).find(Boolean)
  // Filename semantics only prefill the operator field; the editable floor
  // code remains the authority sent to the Revit workflow.
  if (basement) wallFloorCode.value = basement
  wallError.value = ''
}
async function createWallProject() {
  if (project.currentId || wallProjectCreating.value) return
  wallProjectCreating.value = true
  try {
    await project.create(wallProjectName.value.trim() || 'BuildMate BIM 演示项目')
    ElMessage.success('演示项目已创建并选中')
  } catch (e: any) {
    ElMessage.error(apiErrorMessage(e, '创建项目失败'))
  } finally { wallProjectCreating.value = false }
}

async function refreshWallTask(taskId: string) {
  if (!project.currentId) return null
  await taskState.refresh(taskId, project.currentId)
  return taskState.tasks[taskId] || null
}

// The task itself is persisted by the backend.  Persisting only its id here
// lets a route change unmount BIM without losing the screen context; returning
// to BIM reloads the task and resumes polling from the durable API.
watch(() => project.currentId, (projectId) => {
  if (!projectId) return
  // A project switch must never display or poll the previous project's task.
  if (wallTaskProjectId && wallTaskProjectId !== projectId) {
    wallPollGeneration += 1
    wallTaskId.value = ''
    wallError.value = ''
  }
  wallTaskProjectId = projectId
  void restorePersistedWallTask()
}, { immediate: true })

async function waitForWallTask(taskId: string) {
  const generation = ++wallPollGeneration
  // A0/A1 engineering sheets may spend several minutes in OCR and geometry
  // processing.  The API persists the real status, so a six-minute client
  // cutoff only creates a misleading error while the worker is still healthy.
  // Keep polling beyond the backend's 20-minute prepare budget and stop only
  // on a genuine terminal state or a long-lived service outage.
  let pollFailures = 0
  for (let attempt = 0; attempt < 900; attempt++) {
    if (generation !== wallPollGeneration) return wallTask.value
    let latest: any = null
    try {
      latest = await refreshWallTask(taskId)
      pollFailures = 0
    } catch (error: any) {
      if (generation !== wallPollGeneration) return wallTask.value
      pollFailures += 1
      // A temporary API restart must not turn a still-running BIM task into
      // a false failure.  Keep the persisted task id and retry with the same
      // bounded polling budget; surface a message only after repeated misses.
      if (pollFailures >= 3) {
        wallError.value = `任务状态暂时无法读取（第 ${pollFailures} 次），正在重试：${
          apiErrorMessage(error, '网络错误')}`
      }
      await new Promise(resolve => setTimeout(resolve, Math.min(5000, 1000 * pollFailures)))
      continue
    }
    if (generation !== wallPollGeneration) return wallTask.value
    // Do not depend only on the optional `terminal` field.  Older API
    // responses and a cached task can omit it even though the persisted
    // status is already final; otherwise the UI keeps polling for six
    // minutes and leaves the stepper on "确定性识别" after Revit delivery.
    const terminalStatus = latest && ['succeeded', 'failed', 'canceled'].includes(latest.status)
    if (latest && (latest.terminal === true || terminalStatus || latest.status === 'waiting_human')) {
      return latest
    }
    await new Promise(resolve => setTimeout(resolve, 2000))
  }
  wallError.value = '任务轮询超过 30 分钟，请到任务时间线查看真实状态。'
  return wallTask.value
}

async function submitWallPipeline() {
  if (bimMemory.state.value.loading) { ElMessage.info('正在恢复 BIM 会话，请稍候'); return }
  if (!project.currentId) { ElMessage.warning('请先选择或创建项目'); return }
  if (!wallFiles.value.length) return
  const suffixes = wallFiles.value.map(file => file.name.toLowerCase().slice(file.name.lastIndexOf('.')))
  const allPdf = suffixes.every(suffix => suffix === '.pdf')
  const allCad = suffixes.every(suffix => suffix === '.dwg' || suffix === '.dxf')
  if (!allPdf && !allCad) { ElMessage.error('PDF 只能和 PDF 一起提交，DWG/DXF 只能和 CAD 文件一起提交，请勿混用'); return }
  const targetModelPath = wallTargetModelPath.value.trim()
  const elevationRangeText = wallElevationRange.value.trim()
  const elevationRange = elevationRangeText ? parseElevationRange(elevationRangeText) : null
  if (elevationRangeText && !elevationRange) {
    ElMessage.error('本层标高范围格式不正确，请使用 -6.4~0（单位：m，顶部必须大于底部）'); return
  }
  let floorCode = wallFloorCode.value.trim().toUpperCase()
  if (!floorCode && elevationRange?.floorCode) floorCode = elevationRange.floorCode
  const materialName = wallMaterialName.value.trim()
  if (!targetModelPath.toLowerCase().endsWith('.rvt') || !floorCode || !materialName) {
    ElMessage.error('请填写有效的 Revit 工作模型路径、墙柱材料；楼层编码可由 -6.4~0 自动判断为 B1'); return
  }
  const pdfScaleDenominator = wallPdfScaleDenominator.value == null
    ? null : Number(wallPdfScaleDenominator.value)
  if (allPdf && pdfScaleDenominator != null
      && (!Number.isFinite(pdfScaleDenominator) || pdfScaleDenominator <= 0)) {
    ElMessage.error('PDF 出图比例必须是正数，或留空由系统读取标题栏'); return
  }
  wallRunning.value = true
  wallError.value = ''
  wallTaskId.value = ''
  try {
    const openingOverrides: Record<string, number> = {}
    for (const entry of openingElevationCorrections.value.split(/[;；\n]+/).filter(value => value.trim())) {
      const match = entry.trim().toUpperCase().replace(/−/g, '-').match(
        /^([A-Z][A-Z0-9_-]{0,63})\s*[=＝]\s*([+-]?(?:\d+(?:\.\d+)?|\.\d+))\s*(?:M|米)?$/,
      )
      if (!match || !Number.isFinite(Number(match[2])) || Math.abs(Number(match[2])) > 1000
          || Object.prototype.hasOwnProperty.call(openingOverrides, match[1])) {
        throw new Error('洞底标高校正请使用 JD3=-2.200 格式，编号不可重复；单位 m')
      }
      openingOverrides[match[1]] = Number(match[2])
    }
    // Upload a small bounded batch in parallel.  Serial uploads make a
    // 50-sheet submission wait for every network round trip, while an
    // unbounded Promise.all can saturate the browser/API and trigger retries.
    const artifacts: any[] = new Array(wallFiles.value.length)
    let nextUploadIndex = 0
    const uploadWorker = async () => {
      while (true) {
        const index = nextUploadIndex++
        if (index >= wallFiles.value.length) return
        artifacts[index] = await artifactStore.upload(
          wallFiles.value[index], project.currentId as string, 'wall_source',
        )
      }
    }
    const uploadConcurrency = Math.min(3, wallFiles.value.length)
    await Promise.all(Array.from({ length: uploadConcurrency }, uploadWorker))
    const options: Record<string, any> = {
      memory_session_id: bimMemory.sessionId.value,
      execute_revit: true,
      bridge_preflight: true,
      revit: { target_model_path: targetModelPath, floor_code: floorCode },
      wall: { material_name: materialName },
      column: { material_name: materialName },
      opening: {
        elevation_reference: openingElevationReference.value,
        sill_elevation_overrides_m: openingOverrides,
      },
      // Filename semantics only select the evidence source role.  Geometry
      // and final coordinates remain determined by the vector pipeline.
      source_roles: wallFiles.value.map(file => {
        const name = file.name.toLowerCase()
        if (/详图|大样|柱表|构件表|配筋详图|detail|schedule/.test(name)) return 'column_detail'
        if (/平面图|plan|floor/.test(name)) return 'plan'
        return 'supplementary'
      }),
      modeling_standard: {
        profile: 'cn_gb_bim_delivery_v1',
        references: ['GB/T 51212-2016', 'GB/T 51269-2017', 'GB/T 51301-2018', 'GB/T 51235-2017'],
        delivery_units: 'mm',
      },
    }
    if (wallContinueTaskId.value.trim()) {
      options.continue_from_task_id = wallContinueTaskId.value.trim()
    }
    if (elevationRange) {
      const levelCode = floorCode.toLowerCase().replace(/[^a-z0-9_-]+/g, '-') || 'main'
      options.level = {
        id: `level-${levelCode}`,
        name: floorCode || 'Main Level',
        elevation_range: elevationRangeText,
        elevation_m: elevationRange.bottom,
        top_elevation_m: elevationRange.top,
        wall_height_m: elevationRange.height,
        elevation_source: 'input',
      }
    }
    if (allPdf) {
      options.pdf_layer_mode = 'structural_auto'
      if (pdfScaleDenominator != null) {
        // An explicit operator value overrides title-block inference. PDF
        // coordinates are points; convert paper scale through millimetres.
        options.coordinate = { scale_to_m: pdfScaleDenominator * 25.4 / 72 / 1000 }
      }
    }
    const task = await taskState.submit('wall_pipeline', project.currentId, artifacts.map(artifact => artifact.id), options)
    wallTaskId.value = task.id
    persistWallTaskId(task.id)
    ElMessage.success('图纸已进入统一 BIM Agent 链路')
    const completed = await waitForWallTask(task.id)
    if (completed?.status === 'succeeded') {
      lastWallTaskId.value = task.id
      wallContinueTaskId.value = task.id
    }
  } catch (e: any) {
    wallError.value = apiErrorMessage(e, '墙体建模任务提交失败')
    ElMessage.error(wallError.value)
  } finally { wallRunning.value = false }
}

async function resumeWall(decision: 'approved' | 'rejected') {
  if (!wallTaskId.value || !project.currentId || !canWallApprove.value) return
  wallRunning.value = true
  wallError.value = ''
  try {
    await taskState.resume(wallTaskId.value, project.currentId, decision,
      decision === 'approved' ? '前端人工审批' : '前端驳回')
    await waitForWallTask(wallTaskId.value)
  } catch (e: any) {
    wallError.value = apiErrorMessage(e, '审批提交失败')
    ElMessage.error(wallError.value)
  } finally { wallRunning.value = false }
}

async function downloadWallArtifact(artifactId: string, filename: string) {
  if (!project.currentId) return
  try {
    const response = await v2Api.artifacts.content(artifactId, project.currentId)
    const url = window.URL.createObjectURL(new Blob([response.data]))
    const anchor = document.createElement('a')
    anchor.href = url; anchor.download = filename; anchor.click()
    window.URL.revokeObjectURL(url)
  } catch (e: any) {
    ElMessage.error('产物下载失败：' + apiErrorMessage(e, '未知错误'))
  }
}

async function copyWallDeliveryPath() {
  if (!wallDeliveryRvtPath.value) return
  try {
    await navigator.clipboard.writeText(wallDeliveryRvtPath.value)
    ElMessage.success('交付 RVT 路径已复制')
  } catch {
    ElMessage.info('请手动复制页面上显示的交付 RVT 路径')
  }
}
</script>
