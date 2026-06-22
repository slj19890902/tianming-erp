import { defineStore } from 'pinia'

export interface WorkTab {
  name: string
  title: string
  path: string
}

export const useTabsStore = defineStore('tabs', {
  state: () => ({
    activePath: '/',
    tabs: [{ name: 'dashboard', title: '首页工作台', path: '/' }] as WorkTab[],
  }),
  actions: {
    openTab(tab: WorkTab) {
      if (!this.tabs.some((item) => item.path === tab.path)) {
        this.tabs.push(tab)
      }
      this.activePath = tab.path
    },
    closeTab(path: string) {
      if (path === '/') return
      const index = this.tabs.findIndex((item) => item.path === path)
      if (index < 0) return
      this.tabs.splice(index, 1)
      if (this.activePath === path) {
        this.activePath = this.tabs[Math.max(0, index - 1)]?.path || '/'
      }
    },
  },
})
