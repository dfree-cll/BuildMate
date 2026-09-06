<template>
  <div class="knowledge-page">
    <h2>项目知识库</h2>
    <el-alert v-if="!project.currentId" title="请先在顶部选择或创建项目" type="warning" :closable="false" />
    <el-card>
      <template #header>录入项目资料</template>
      <el-upload :auto-upload="false" :show-file-list="false" :on-change="onFile">
        <el-button :loading="artifact.uploading || knowledge.indexing" :disabled="!project.currentId">选择并建立索引</el-button>
      </el-upload>
      <div class="hint">支持 PDF、DOCX、TXT、Markdown、IFC；扫描版 PDF 会尝试 OCR，仍无法识别时明确失败。</div>
    </el-card>
    <el-card style="margin-top:16px">
      <template #header>有据检索</template>
      <el-input v-model="query" placeholder="例如：墙体厚度和轴网检查有哪些要求？" @keyup.enter="search">
        <template #append><el-button :loading="knowledge.searching" @click="search">检索</el-button></template>
      </el-input>
      <el-empty v-if="searched && !knowledge.hits.length" description="没有找到足够证据，系统已拒绝猜测" />
      <el-card v-for="hit in knowledge.hits" :key="hit.chunk_id" shadow="never" class="hit">
        <div class="source">{{ hit.source_name }}<span v-if="hit.page_no"> · 第 {{ hit.page_no }} 页</span></div>
        <div>{{ hit.content }}</div>
        <div class="score">综合相关度 {{ Number(hit.score).toFixed(3) }} · 证据 {{ hit.chunk_id.slice(0, 12) }}</div>
      </el-card>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '../stores/auth'
import { useArtifactStore } from '../stores/artifact'
import { useKnowledgeStore } from '../stores/knowledge'
import { useProjectStore } from '../stores/project'

const auth = useAuthStore()
const artifact = useArtifactStore()
const knowledge = useKnowledgeStore()
const project = useProjectStore()
const query = ref('')
const searched = ref(false)

async function onFile(upload: any) {
  if (!project.currentId) return
  try {
    const saved = await artifact.upload(upload.raw, project.currentId, 'knowledge_document')
    await knowledge.ingest(saved.id, project.currentId)
    ElMessage.success('资料已建立可追溯索引')
  } catch (error: any) { ElMessage.error(error.response?.data?.error?.message || '资料入库失败') }
}

async function search() {
  if (!query.value.trim() || !project.currentId) return
  searched.value = true
  const tenantId = auth.user?.tenant_id || 'tenant_default'
  try { await knowledge.search(query.value.trim(), tenantId, project.currentId) }
  catch (error: any) { ElMessage.error(error.response?.data?.error?.message || '检索失败') }
}
</script>

<style scoped>
.hint,.score{color:#7b8494;font-size:12px;margin-top:10px}.hit{margin-top:12px}.source{font-weight:600;margin-bottom:8px;color:#324a75}
</style>
