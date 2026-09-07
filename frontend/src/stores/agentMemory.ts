import { computed } from 'vue'
import { defineStore } from 'pinia'
import { v2Client } from '../api/v2'
import { useAuthStore } from './auth'
import { useProjectStore } from './project'

export type MemoryAgent = 'qa' | 'bid_review' | 'procurement' | 'negotiation' | 'drawing2bim'
export interface MemoryTurn {
  seq: number; user_text: string; answer: string; result: Record<string, unknown>
}
export interface BimSettings {
  target_model_path: string; floor_code: string; elevation_range: string
  material_name: string; pdf_scale_denominator: number | null
}
export interface MemoryPreferences { note?: string; bim?: BimSettings }
export interface MemoryDetail {
  session_id: string; summary: string; preferences: MemoryPreferences; turns: MemoryTurn[]
}
interface SessionState {
  id: string; loaded: boolean; loading: boolean; generation: number
  sessions: { session_id: string; title: string }[]; detail: MemoryDetail
}
const blank = (id: string): MemoryDetail => ({ session_id: id, summary: '', preferences: {}, turns: [] })

export const useAgentMemoryStore = defineStore('agentMemory', {
  state: () => ({ contexts: {} as Record<string, SessionState> }),
  actions: {
    ensure(key: string) {
      if (!this.contexts[key]) {
        const id = localStorage.getItem(key) || crypto.randomUUID()
        this.contexts[key] = { id, loaded: false, loading: false, generation: 0, sessions: [], detail: blank(id) }
      }
      return this.contexts[key]
    },
    async load(key: string, agent: MemoryAgent, projectId: string | null, selected?: string) {
      const state = this.ensure(key)
      const generation = ++state.generation
      state.loading = true
      try {
        const params = { project_id: projectId }
        const { data } = await v2Client.get(`/memory/${agent}/sessions`, { params })
        if (generation !== state.generation) return
        state.sessions = data.sessions
        const id = selected || localStorage.getItem(key) || data.sessions[0]?.session_id || state.id
        const response = await v2Client.get<MemoryDetail>(`/memory/${agent}/sessions/${encodeURIComponent(id)}`, { params })
        if (generation !== state.generation) return
        state.id = id
        state.detail = response.data
        state.loaded = true
        localStorage.setItem(key, id) // Only an opaque ID; no conversation is stored in the browser.
      } finally {
        if (generation === state.generation) state.loading = false
      }
    },
    newSession(key: string) {
      const state = this.ensure(key)
      ++state.generation
      state.loading = false
      state.id = crypto.randomUUID()
      state.detail = blank(state.id)
      state.loaded = true
      localStorage.setItem(key, state.id)
    },
  },
})

export function useAgentSession(agent: MemoryAgent) {
  const store = useAgentMemoryStore()
  const auth = useAuthStore()
  const project = useProjectStore()
  const key = computed(() => 'bm-memory:' + JSON.stringify([
    auth.user?.tenant_id, auth.user?.user_id, project.currentId || null, agent,
  ]))
  const state = computed(() => store.ensure(key.value))
  return {
    key, state, sessionId: computed(() => state.value.id),
    projectId: computed(() => project.currentId || null),
    refresh: (id?: string) => store.load(key.value, agent, project.currentId || null, id),
    newSession: () => store.newSession(key.value),
    savePreferences: async (preferences: MemoryPreferences) => {
      const currentKey = key.value
      const current = state.value
      const id = current.id
      const response = await v2Client.put<MemoryDetail>(`/memory/${agent}/sessions/${encodeURIComponent(id)}/preferences`, {
        project_id: project.currentId || null, preferences,
      })
      if (key.value === currentKey && current.id === id) current.detail = response.data
    },
  }
}
