import client, { refreshAccessToken } from './client'

export async function login(username: string, password: string) {
  const r = await client.post('/auth/login', { username, password })
  return r.data
}

// H4：服务端吊销（jti 黑名单 + token_version bump），失败不阻断本地登出
export async function logout() {
  try { await client.post('/auth/logout') } catch (e) {}
}

export { refreshAccessToken }
