import axios, { type AxiosInstance, type InternalAxiosRequestConfig } from 'axios'

const client = axios.create({ baseURL: '/api/v1', timeout: 300000 })   // 5 分钟（大标书上传+解析）

client.interceptors.request.use((config) => {
  const token = localStorage.getItem('bm_token')
  if (token) config.headers.Authorization = 'Bearer ' + token
  return config
})

// H4：刷新令牌换新访问令牌（并发 401 共享同一次刷新请求）
let refreshing: Promise<string | null> | null = null

type NetworkRetryConfig = InternalAxiosRequestConfig & {
  _buildmateNetworkRetryCount?: number
}

const NETWORK_RETRY_DELAYS_MS = [500, 1000, 2000, 4000, 5000]

function networkRetryIsSafe(config: NetworkRetryConfig): boolean {
  const method = (config.method || 'get').toLowerCase()
  if (['get', 'head', 'options'].includes(method)) return true
  if (method !== 'post') return false
  const url = config.url || ''
  // Uploads are content-deduplicated by tenant/project/name/hash, workflow
  // submissions carry Idempotency-Key, and auth calls have no business-side
  // effect.  Other mutations remain operator-controlled and are not replayed.
  return url.includes('/artifacts')
    || url.includes('/workflows')
    || url.includes('/auth/login')
    || url.includes('/auth/refresh')
}

export function isTransientNetworkError(error: unknown): boolean {
  return axios.isAxiosError(error)
    && error.code === 'ERR_NETWORK'
    && !error.response
    && !!error.config
}

export async function retryTransientNetworkError(
  instance: AxiosInstance,
  error: unknown,
) {
  if (!isTransientNetworkError(error) || !axios.isAxiosError(error) || !error.config) {
    return Promise.reject(error)
  }
  const config = error.config as NetworkRetryConfig
  const attempt = config._buildmateNetworkRetryCount || 0
  if (!networkRetryIsSafe(config) || attempt >= NETWORK_RETRY_DELAYS_MS.length) {
    return Promise.reject(error)
  }
  config._buildmateNetworkRetryCount = attempt + 1
  await new Promise(resolve => setTimeout(resolve, NETWORK_RETRY_DELAYS_MS[attempt]))
  return instance.request(config)
}

export async function refreshAccessToken(): Promise<string | null> {
  const rt = localStorage.getItem('bm_refresh')
  if (!rt) return null
  if (!refreshing) {
    // Authentication is a v2 contract.  Legacy business endpoints keep the
    // v1 client below until their v2 replacements are complete.
    refreshing = fetch('/api/v2/auth/refresh', {
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
    if (isTransientNetworkError(error)) {
      return retryTransientNetworkError(client, error)
    }
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
