<template>
  <el-container style="height: 100vh">
    <el-aside width="220px" style="background: #1a237e">
      <div style="color: #fff; padding: 20px 16px; font-size: 18px; font-weight: bold">🏗️ BuildMate</div>
      <el-menu :default-active="$route.path" router background-color="#1a237e" text-color="#c5cae9" active-text-color="#fff">
        <el-menu-item index="/dashboard"><el-icon><DataBoard /></el-icon><span>仪表盘</span></el-menu-item>
        <el-menu-item index="/qa"><el-icon><ChatDotRound /></el-icon><span>智能对话</span></el-menu-item>
        <el-menu-item index="/bid-review"><el-icon><Document /></el-icon><span>投标审查</span></el-menu-item>
        <el-menu-item index="/bim"><el-icon><OfficeBuilding /></el-icon><span>BIM 审图</span></el-menu-item>
        <el-menu-item index="/procurement"><el-icon><ShoppingCart /></el-icon><span>采购审批</span></el-menu-item>
        <el-menu-item index="/negotiation"><el-icon><ChatLineRound /></el-icon><span>供应商谈判</span></el-menu-item>
        <el-menu-item index="/teacher"><el-icon><User /></el-icon><span>教师端</span></el-menu-item>
        <el-menu-item index="/history"><el-icon><Clock /></el-icon><span>历史记录</span></el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header style="background:#fff; display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #eee">
        <span>建筑行业智能助手</span>
        <el-button link @click="logout">退出登录</el-button>
      </el-header>
      <el-main style="background:#f0f2f5">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { useRouter } from 'vue-router'
import { useAuthStore } from '../../stores/auth'
const router = useRouter()
const auth = useAuthStore()
function logout() { auth.logout(); router.push('/login') }
</script>
