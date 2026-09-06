<template>
  <el-container class="app-shell">
    <el-aside class="app-sidebar">
      <div class="app-brand"><span class="app-brand-mark">B</span><span class="app-brand-name">BuildMate</span></div>
      <el-menu class="app-menu" :default-active="$route.path" router>
        <el-menu-item index="/dashboard" aria-label="仪表盘" title="仪表盘"><el-icon><DataBoard /></el-icon><span>仪表盘</span></el-menu-item>
        <el-menu-item v-if="isAdmin" index="/knowledge" aria-label="项目知识库" title="项目知识库"><el-icon><Collection /></el-icon><span>项目知识库</span></el-menu-item>
        <el-menu-item v-if="isAdmin" index="/tasks" aria-label="任务时间线" title="任务时间线"><el-icon><List /></el-icon><span>任务时间线</span></el-menu-item>
        <el-menu-item index="/qa" aria-label="智能工作台" title="智能工作台"><el-icon><ChatDotRound /></el-icon><span>智能工作台</span></el-menu-item>
        <el-menu-item index="/bim" aria-label="BIM Agent" title="BIM Agent"><el-icon><OfficeBuilding /></el-icon><span>BIM Agent</span></el-menu-item>
        <el-menu-item v-if="canReview" index="/review" aria-label="审核中心" title="审核中心"><el-icon><User /></el-icon><span>审核中心</span></el-menu-item>
        <el-menu-item index="/history" aria-label="历史记录" title="历史记录"><el-icon><Clock /></el-icon><span>历史记录</span></el-menu-item>
      </el-menu>
    </el-aside>
    <el-container class="app-main">
      <el-header class="app-header">
        <span class="app-header-title">BuildMate <small>AI WORKSPACE</small></span>
        <div class="app-header-actions">
          <el-select v-model="project.currentId" class="app-project-select" placeholder="选择项目" @change="project.select">
            <el-option v-for="item in project.projects" :key="item.id" :label="item.name" :value="item.id" />
          </el-select>
          <el-button link @click="logout">退出登录</el-button>
        </div>
      </el-header>
      <el-main class="app-content">
        <router-view v-slot="{ Component }">
          <KeepAlive include="BimReviewView,QAChatView">
            <component :is="Component" />
          </KeepAlive>
        </router-view>
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../../stores/auth'
import { useProjectStore } from '../../stores/project'
const router = useRouter()
const auth = useAuthStore()
const project = useProjectStore()
const isAdmin = computed(() => auth.user?.role === 'admin')
const canReview = computed(() => ['admin', 'reviewer'].includes(auth.user?.role || ''))
onMounted(() => project.load())
async function logout() {
  await auth.logout()
  await router.push('/login')
}
</script>
