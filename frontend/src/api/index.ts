import client, { refreshAccessToken } from './client'

// 统一对话 SSE
// M8：支持外部 AbortSignal（切页/重发时中断）；检查 resp.ok
// H4：401 时先尝试刷新令牌重放一次（与 axios 拦截器行为一致）
export async function chatStream(sessionId: string, message: string, onEvent: (e: any) => void,
                                 signal?: AbortSignal) {
  const doFetch = () => fetch('/api/v1/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + localStorage.getItem('bm_token') },
    body: JSON.stringify({ session_id: sessionId, message }),
    signal,
  })
  let resp = await doFetch()
  if (resp.status === 401) {
    const newToken = await refreshAccessToken()
    if (newToken) resp = await doFetch()
  }
  if (!resp.ok || !resp.body) {
    if (resp.status === 401) {
      localStorage.removeItem('bm_token')
      localStorage.removeItem('bm_refresh')
      window.location.href = '/login'
    }
    onEvent({ type: 'error', message: '请求失败（' + resp.status + '），请稍后重试' })
    return
  }
  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  const lines: string[] = []
  const handle = (raw: string) => {
    for (const line of raw.split('\n')) {
      if (!line.startsWith('data:')) continue
      try { onEvent(JSON.parse(line.slice(5).trim())) } catch (e) {}
    }
  }
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let idx
    while ((idx = buffer.search(/\r?\n/)) >= 0) {
      const line = buffer.slice(0, idx).replace(/\r$/, '')
      buffer = buffer.slice(idx + 1)
      if (line === '') { if (lines.length) { handle(lines.join('\n')); lines.length = 0 } } else lines.push(line)
    }
  }
  if (buffer.trim()) { handle(buffer) }
}

// 投标审查
export const bidSubmit = (docText: string) => client.post('/bid-review/review', { doc_text: docText, session_id: 'fe-' + Date.now() })
export const bidUpload = (file: File) => {
  const fd = new FormData()
  fd.append('file', file)
  return client.post('/bid-review/upload', fd)
}
export const bidPoll = (reviewId: string) => client.get('/bid-review/reviews/' + reviewId)
export const bidList = () => client.get('/bid-review/reviews')

// BIM 审图
export const bimUpload = (file: File) => {
  const fd = new FormData()
  fd.append('file', file)
  return client.post('/bim/upload', fd)
}
export const bimPoll = (reviewId: string) => client.get('/bim/reviews/' + reviewId)
export const bimList = () => client.get('/bim/reviews')

// 采购审批
export const createOrder = (data: any) => client.post('/procurement/orders', data)
export const confirmOrder = (orderNo: string, decision: any) => client.post('/procurement/orders/' + orderNo + '/confirm', decision)
export const pendingOrders = () => client.get('/procurement/pending')
export const myOrders = () => client.get('/procurement/my-orders')

// 谈判
export const negChat = (data: any) => client.post('/negotiation/chat', data)

// 知识待补（教师）
export const knowledgePending = () => client.get('/knowledge/pending')
export const knowledgeResolve = (id: string) => client.post('/knowledge/pending/' + id + '/resolve')
export const knowledgeAnswer = (id: string, answer: string) =>
  client.post('/knowledge/pending/' + id + '/answer', { answer })

// 观测
export const obsStats = () => client.get('/observability/stats')
