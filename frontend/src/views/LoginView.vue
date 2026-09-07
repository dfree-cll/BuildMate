<template>
  <div class="login-page">
    <el-card class="login-card">
      <div class="login-mark">B</div>
      <h1>BuildMate</h1>
      <p class="login-subtitle">建筑行业智能协作平台</p>
      <el-form :model="form" @submit.prevent="doLogin">
        <el-form-item><el-input v-model="form.username" placeholder="用户名" /></el-form-item>
        <el-form-item><el-input v-model="form.password" type="password" placeholder="密码" show-password /></el-form-item>
        <el-button type="primary" class="login-button" :loading="loading" @click="doLogin">登录</el-button>
        <div class="login-hint">演示账号：管理员 admin · 审核端 reviewer01</div>
      </el-form>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()
const loading = ref(false)
const form = reactive({ username: 'admin', password: 'demo123' })

async function doLogin() {
  loading.value = true
  try {
    await auth.login(form.username, form.password)
    ElMessage.success('登录成功')
    router.push('/dashboard')
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || '登录失败')
  } finally { loading.value = false }
}
</script>

<style scoped>
.login-page {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 24px;
  background: #f5f5f7;
}
.login-card { width: min(400px, 100%); text-align: center; }
.login-mark {
  width: 48px;
  height: 48px;
  margin: 4px auto 18px;
  display: grid;
  place-items: center;
  border-radius: 14px;
  color: #fff;
  background: #1d1d1f;
  font-size: 24px;
  font-weight: 700;
}
h1 { margin: 0; font-size: 28px; letter-spacing: -.03em; }
.login-subtitle { margin: 8px 0 28px; color: #6e6e73; font-size: 14px; }
.login-button { width: 100%; height: 42px; }
.login-hint { margin-top: 16px; color: #6e6e73; font-size: 12px; }
</style>
