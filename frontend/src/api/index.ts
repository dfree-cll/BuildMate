import client, { refreshAccessToken } from './client'

// 统一对话 SSE
// M8：支持外部 AbortSignal（切页/重发时中断）；检查 resp.ok
// H4：401 时先尝试刷新令牌重放一次（与 axios 拦截器行为一致）
export async function chatStream(sessionId: string, message: string, onEvent: (e: any) => void,
                                 signal?: AbortSignal, projectId?: string | null) {
  // Keep a transport-level upper bound.  The backend also has a workflow
  // timeout, but this protects the browser when a proxy or a crashed worker
  // leaves an SSE connection open without events.
  // Some lightweight unit-test runtimes (and older embedded browsers) do not
  // expose AbortController.  The stream remains functional there; modern
  // browsers get the full timeout/cancellation behavior.
  const requestController = typeof globalThis.AbortController === 'function'
    ? new globalThis.AbortController() : null
  let timedOut = false
  const timeoutId = typeof globalThis.setTimeout === 'function'
    ? globalThis.setTimeout(() => {
      timedOut = true
      requestController?.abort()
    }, 120000)
    : undefined
  const abortRequest = () => requestController?.abort()
  if (signal) {
    if (signal.aborted) requestController?.abort()
    else signal.addEventListener('abort', abortRequest, { once: true })
  }
  const doFetch = () => fetch('/api/v1/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + localStorage.getItem('bm_token') },
    body: JSON.stringify({ session_id: sessionId, message, project_id: projectId || null }),
    signal: requestController?.signal ?? signal,
  })
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined
  try {
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
    reader = resp.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    const lines: string[] = []
    let eventCount = 0
    const handle = (raw: string) => {
      const data = raw.split('\n').filter(line => line.startsWith('data:'))
        .map(line => line.slice(5).trimStart()).join('\n')
      if (!data) return // SSE comments/heartbeats contain no data field.
      let event
      try { event = JSON.parse(data) }
      catch { throw new Error('服务端返回的对话事件格式无效，请重试') }
      if (!event || typeof event !== 'object' || typeof event.type !== 'string') {
        throw new Error('服务端返回的对话事件缺少类型，请重试')
      }
      // Do not swallow UI callback errors as if they were malformed JSON.
      eventCount += 1
      onEvent(event)
    }
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let idx
      while ((idx = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, idx).replace(/\r$/, '')
        buffer = buffer.slice(idx + 1)
        if (line === '') { if (lines.length) { handle(lines.join('\n')); lines.length = 0 } } else lines.push(line)
      }
    }
    buffer += decoder.decode()
    if (buffer.trim()) lines.push(buffer)
    if (lines.length) handle(lines.join('\n'))
    if (eventCount === 0) {
      throw new Error('服务端未返回任何对话结果，请检查服务状态后重试')
    }
  } catch (error) {
    if (timedOut && !signal?.aborted) {
      throw new Error('对话请求超时，请检查服务状态后重试')
    }
    throw error
  } finally {
    reader?.releaseLock()
    if (timeoutId !== undefined && typeof globalThis.clearTimeout === 'function') {
      globalThis.clearTimeout(timeoutId)
    }
    signal?.removeEventListener('abort', abortRequest)
  }
}

// 投标审查
export const bidSubmit = (docText: string, sessionId: string, projectId: string | null) =>
  client.post('/bid-review/review', { doc_text: docText, session_id: sessionId, project_id: projectId })
// 多文件上传（技术标/商务标/资质分开投，PDF/Word/图片混合）
export const bidUploadMulti = (files: File[], sessionId: string, projectId: string | null) => {
  const fd = new FormData()
  for (const f of files) fd.append('files', f)
  fd.append('session_id', sessionId)
  if (projectId) fd.append('project_id', projectId)
  return client.post('/bid-review/upload-multi', fd)
}
export const bidPoll = (reviewId: string) => client.get('/bid-review/reviews/' + reviewId)
export const bidList = () => client.get('/bid-review/reviews')

// 采购审批
export const createOrder = (data: any) => client.post('/procurement/orders', data)
export const confirmOrder = (orderNo: string, decision: any) => client.post('/procurement/orders/' + orderNo + '/confirm', decision)
export const pendingOrders = () => client.get('/procurement/pending')
export const myOrders = () => client.get('/procurement/my-orders')

// 谈判
export const negChat = (data: any) => client.post('/negotiation/chat', data)
export const negMinutes = (data: any) => client.post('/negotiation/minutes', data)

// 知识待补（审核端）
export const knowledgePending = () => client.get('/knowledge/pending')
export const knowledgeResolve = (id: string) => client.post('/knowledge/pending/' + id + '/resolve')
export const knowledgeAnswer = (id: string, answer: string) =>
  client.post('/knowledge/pending/' + id + '/answer', { answer })

// 观测
export const obsStats = () => client.get('/observability/stats')
