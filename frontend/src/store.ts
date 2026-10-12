import { defineStore } from 'pinia'
import { api } from './api'

export const useSystem = defineStore('system', {
  state: () => ({
    status: null as any, error: '', busy: false, connected: false,
  }),
  actions: {
    async refresh() {
      try {
        this.status = await api('/api/status')
        this.connected = true
      } catch (error) {
        this.connected = false
        this.error = (error as Error).message
      }
    },
    async command(path: string, body: unknown = {}) {
      this.busy = true
      try {
        const result = await api(path, body)
        await this.refresh()
        return result
      } finally {
        this.busy = false
      }
    },
  },
})
