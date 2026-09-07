import { defineStore } from 'pinia'
import { v2Api } from '../api/v2'

export const useTaskStore = defineStore('task', {
  state: () => ({ tasks: {} as Record<string, any>, steps: {} as Record<string, any[]>, loading: false }),
  actions: {
    async submit(
      workflow: string,
      projectId: string,
      artifactIds: string[],
      options: Record<string, unknown> = {},
    ) {
      const key = crypto.randomUUID()
      const response = (await v2Api.tasks.submit(
        workflow, projectId, artifactIds, key, options,
      )).data
      this.tasks[response.task.id] = response.task
      return response.task
    },
    async refresh(taskId: string, projectId: string) {
      this.loading = true
      try {
        const [task, steps] = await Promise.all([
          v2Api.tasks.get(taskId, projectId), v2Api.tasks.steps(taskId, projectId),
        ])
        this.tasks[taskId] = task.data
        this.steps[taskId] = steps.data.steps || []
      } finally { this.loading = false }
    },
    async resume(taskId: string, projectId: string, decision: 'approved' | 'rejected', reason = '') {
      const response = await v2Api.tasks.resume(taskId, projectId, decision, reason)
      this.tasks[taskId] = response.data
      return response.data
    },
  },
})
