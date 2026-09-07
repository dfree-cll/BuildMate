import { defineStore } from 'pinia'
import { v2Api } from '../api/v2'

export const useKnowledgeStore = defineStore('knowledge', {
  state: () => ({ hits: [] as any[], retrievalRunId: '', searching: false, indexing: false }),
  actions: {
    async ingest(artifactId: string, projectId: string) {
      this.indexing = true
      try { return (await v2Api.knowledge.ingestArtifact(artifactId, projectId)).data }
      finally { this.indexing = false }
    },
    async search(query: string, tenantId: string, projectId: string) {
      this.searching = true
      try {
        const data = (await v2Api.knowledge.search(query, tenantId, projectId)).data
        this.hits = data.hits || []
        this.retrievalRunId = data.retrieval_run_id
        return data
      } finally { this.searching = false }
    },
  },
})
