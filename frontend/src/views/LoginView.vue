<template>
  <div style="height:100vh; display:flex; align-items:center; justify-content:center; background:linear-gradient(135deg,#1a237e,#283593)">
    <el-card style="width:400px">
      <h2 style="text-align:center; color:#1a237e">🏗️ BuildMate 建筑行业智能助手</h2>
      <el-form :model="form" @submit.prevent="doLogin">
        <el-form-item><el-input v-model="form.username" placeholder="用户名" /></el-form-item>
        <el-form-item><el-input v-model="form.password" type="password" placeholder="密码" show-password /></el-form-item>
        <el-button type="primary" style="width:100%" :loading="loading" @click="doLogin">登录</el-button>
        <div style="text-align:center; margin-top:8px; color:#999; font-size:12px">测试账号：admin / demo123</div>
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
