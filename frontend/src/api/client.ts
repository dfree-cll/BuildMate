import axios from 'axios'

const client = axios.create({ baseURL: '/api/v1', timeout: 120000 })

client.interceptors.request.use((config) => {
  const token = localStorage.getItem('bm_token')
  if (token) config.headers.Authorization = 'Bearer ' + token
  return config
})

// H4：刷新令牌换新访问令牌（并发 401 共享同一次刷新请求）
let refreshing: Promise<string | null> | null = null

export async function refreshAccessToken(): Promise<string | null> {
  const rt = localStorage.getItem('bm_refresh')
  if (!rt) return null
  if (!refreshing) {
    refreshing = fetch('/api/v1/auth/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: rt }),
    })
      .then(async (r) => {
        if (!r.ok) return null
        const j = await r.json()
        if (j?.access_token) {
          localStorage.setItem('bm_token', j.access_token)
          if (j.refresh_token) localStorage.setItem('bm_refresh', j.refresh_token)
          return j.access_token as string
        }
        return null
      })
      .catch(() => null)
      .finally(() => { refreshing = null })
  }
  return refreshing
}

function forceLogout() {
  localStorage.removeItem('bm_token')
  localStorage.removeItem('bm_refresh')
  window.location.href = '/login'
}

client.interceptors.response.use(
  (resp) => resp,
  async (error) => {
    const cfg = error.config || {}
    const url: string = cfg.url || ''
    // 401：先尝试刷新一次再重放请求；刷新失败才强制登出（登录/刷新接口自身除外）
    if (error.response?.status === 401 && !cfg._retried
        && !url.includes('/auth/login') && !url.includes('/auth/refresh')) {
      const token = await refreshAccessToken()
      if (token) {
        cfg._retried = true
        cfg.headers = { ...cfg.headers, Authorization: 'Bearer ' + token }
        return client.request(cfg)
      }
      forceLogout()
    }
    return Promise.reject(error)
  }
)

export default client
