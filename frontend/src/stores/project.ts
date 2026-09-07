import { defineStore } from 'pinia'
import { v2Api } from '../api/v2'

export const useProjectStore = defineStore('project', {
  state: () => ({
    projects: [] as any[],
    currentId: localStorage.getItem('bm_project_id') || '',
    loading: false,
  }),
  getters: {
    current: state => state.projects.find(item => item.id === state.currentId) || null,
  },
  actions: {
    async load() {
      this.loading = true
      try {
        this.projects = (await v2Api.projects.list()).data.projects || []
        if (this.currentId && !this.projects.some(item => item.id === this.currentId)) {
          this.currentId = ''
          localStorage.removeItem('bm_project_id')
        }
        if (!this.currentId && this.projects.length) this.select(this.projects[0].id)
      } finally { this.loading = false }
    },
    select(id: string) {
      this.currentId = id
      localStorage.setItem('bm_project_id', id)
    },
    async create(name: string) {
      const project = (await v2Api.projects.create(name)).data
      this.projects.unshift(project)
      this.select(project.id)
      return project
    },
  },
})
