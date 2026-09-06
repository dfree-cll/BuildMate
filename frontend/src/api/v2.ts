import axios from 'axios'
import { isTransientNetworkError, refreshAccessToken, retryTransientNetworkError } from './client'

export const v2Client = axios.create({ baseURL: '/api/v2', timeout: 120000 })

v2Client.interceptors.request.use((config) => {
  const token = localStorage.getItem('bm_token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  config.headers['X-Trace-ID'] ||= crypto.randomUUID()
  return config
})

v2Client.interceptors.response.use(
  response => response,
  async error => {
    if (isTransientNetworkError(error)) {
      return retryTransientNetworkError(v2Client, error)
    }
    const config = error.config || {}
    const url: string = config.url || ''
    // Memory/project requests use v2.  They must share the same access-token
    // refresh behavior as v1; otherwise an expired token redirects the user
    // while they are typing and makes the Send button appear broken.
    if (error.response?.status === 401 && !config._retried
        && !url.includes('/auth/login') && !url.includes('/auth/refresh')) {
      const token = await refreshAccessToken()
      if (token) {
        config._retried = true
        config.headers = { ...config.headers, Authorization: `Bearer ${token}` }
        return v2Client.request(config)
      }
      localStorage.removeItem('bm_token')
      localStorage.removeItem('bm_refresh')
      localStorage.removeItem('bm_user')
      window.location.href = '/login'
    }
    return Promise.reject(error)
  },
)

export const v2Api = {
  chat: {
    taskPreview: (content: string, projectId: string | null, artifactIds: string[] = [], signal?: AbortSignal) =>
      v2Client.post('/chat/tasks/preview', {
        content, project_id: projectId, artifact_ids: artifactIds,
      }, { timeout: 5000, signal }),
  },
  projects: {
    list: () => v2Client.get('/projects'),
    create: (name: string) => v2Client.post('/projects', { name }),
  },
  artifacts: {
    upload: (file: File, projectId: string, kind = 'document') => {
      const form = new FormData()
      form.append('file', file)
      form.append('project_id', projectId)
      form.append('kind', kind)
      return v2Client.post('/artifacts', form)
    },
    content: (artifactId: string, projectId: string) =>
      v2Client.get(`/artifacts/${artifactId}/content`, {
        params: { project_id: projectId }, responseType: 'blob',
      }),
  },
  knowledge: {
    ingestArtifact: (artifactId: string, projectId: string, sourceType = 'document') =>
      v2Client.post('/knowledge/documents/from-artifact', {
        artifact_id: artifactId, project_id: projectId,
        scope: 'project', source_type: sourceType,
      }),
    search: (query: string, tenantId: string, projectId: string) =>
      v2Client.post('/knowledge/search', {
        query, tenant_id: tenantId, project_id: projectId,
        scope: 'project', top_k: 8, filters: {},
      }),
  },
  tasks: {
    submitComposite: (
      plan: Record<string, unknown>,
      projectId: string | null,
      artifactIds: string[] = [],
      idempotencyKey?: string,
    ) => v2Client.post('/workflows/composite', {
      plan, project_id: projectId, input_artifact_ids: artifactIds,
    }, idempotencyKey ? { headers: { 'Idempotency-Key': idempotencyKey } } : undefined),
    submit: (
      workflow: string,
      projectId: string,
      artifactIds: string[],
      idempotencyKey: string,
      options: Record<string, unknown> = {},
    ) =>
      v2Client.post('/workflows', {
        workflow, project_id: projectId, input_artifact_ids: artifactIds,
        options,
      }, { headers: { 'Idempotency-Key': idempotencyKey } }),
    get: (taskId: string, projectId: string) =>
      v2Client.get(`/tasks/${taskId}`, { params: { project_id: projectId } }),
    steps: (taskId: string, projectId: string) =>
      v2Client.get(`/tasks/${taskId}/steps`, { params: { project_id: projectId } }),
    resume: (taskId: string, projectId: string, decision: 'approved' | 'rejected', reason = '') =>
      v2Client.post(`/tasks/${taskId}/resume`, {
        project_id: projectId, decision, reason,
      }),
  },
}
