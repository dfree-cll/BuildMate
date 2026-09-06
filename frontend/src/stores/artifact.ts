import { defineStore } from 'pinia'
import { v2Api } from '../api/v2'

export const useArtifactStore = defineStore('artifact', {
  state: () => ({ items: [] as any[], uploading: false, activeUploads: 0 }),
  actions: {
    async upload(file: File, projectId: string, kind = 'document') {
      this.activeUploads += 1
      this.uploading = true
      try {
        const artifact = (await v2Api.artifacts.upload(file, projectId, kind)).data
        this.items.unshift(artifact)
        return artifact
      } finally {
        this.activeUploads = Math.max(0, this.activeUploads - 1)
        this.uploading = this.activeUploads > 0
      }
    },
  },
})
