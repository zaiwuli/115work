import { defineStore } from 'pinia'
import { api } from './api'

let statusRequest: Promise<void> | undefined

export const useSystem = defineStore('system', {
  state: () => ({
    status: null as any, error: '', busy: false, connected: false, refreshVersion: 0,
    autoRefresh: localStorage.getItem('115work-auto-refresh') !== 'false',
  }),
  actions: {
    setAutoRefresh(value: boolean) {
      this.autoRefresh = value
      localStorage.setItem('115work-auto-refresh', String(value))
    },
    async refreshPage() {
      await this.refresh()
      this.refreshVersion++
    },
    async refresh() {
      if (statusRequest) return statusRequest
      statusRequest = (async () => { try {
        this.status = await api('/api/status')
        this.connected = true
      } catch (error) {
        this.connected = false
        this.error = (error as Error).message
      } })()
      try { await statusRequest }
      finally { statusRequest = undefined }
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
