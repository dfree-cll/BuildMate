import { defineStore } from 'pinia'
import { login as apiLogin, logout as apiLogout } from '../api/auth'

// Low：localStorage 中 bm_user 损坏时 JSON.parse 会抛异常导致白屏，安全解析兜底
function loadStoredUser(): any {
  try {
    return JSON.parse(localStorage.getItem('bm_user') || 'null')
  } catch (e) {
    localStorage.removeItem('bm_user')
    return null
  }
}

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: localStorage.getItem('bm_token') || '',
    user: loadStoredUser(),
  }),
  actions: {
    async login(username: string, password: string) {
      const r = await apiLogin(username, password)
      this.token = r.access_token
      this.user = { username, role: r.role, user_id: r.user_id }
      localStorage.setItem('bm_token', r.access_token)
      if (r.refresh_token) localStorage.setItem('bm_refresh', r.refresh_token)
      localStorage.setItem('bm_user', JSON.stringify(this.user))
      return r
    },
    async logout() {
      try { await apiLogout() } catch (e) {}   // 服务端吊销，失败不阻断本地清理
      this.token = ''
      this.user = null
      localStorage.removeItem('bm_token')
      localStorage.removeItem('bm_refresh')
      localStorage.removeItem('bm_user')
    },
  },
})
