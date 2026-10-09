import { defineStore } from 'pinia'

export interface WorkTab {
  name: string
  title: string
  path: string
}

/** 工作标签页：与 v1 相同的机制（首页不可关） */
export const useTabsStore = defineStore('tabs', {
  state: () => ({
    activePath: '/',
    tabs: [{ name: 'dashboard', title: '首页', path: '/' }] as WorkTab[],
    dirtyPaths: {} as Record<string, boolean>,
    revisions: {} as Record<string, number>,
  }),
  actions: {
    openTab(tab: WorkTab) {
      if (!this.tabs.some((item) => item.path === tab.path)) {
        this.tabs.push(tab)
      }
      this.activePath = tab.path
    },
    closeTab(path: string) {
      if (path === '/' || this.dirtyPaths[path]) return false
      const index = this.tabs.findIndex((item) => item.path === path)
      if (index < 0) return
      this.tabs.splice(index, 1)
      delete this.dirtyPaths[path]
      this.revisions[path] = (this.revisions[path] || 0) + 1
      if (this.activePath === path) {
        this.activePath = this.tabs[Math.max(0, index - 1)]?.path || '/'
      }
    },
    reset() {
      this.tabs = [{ name: 'dashboard', title: '首页', path: '/' }]
      this.activePath = '/'
      this.dirtyPaths = {}
      this.revisions = {}
    },
    markDirty(path: string, dirty: boolean) {
      this.dirtyPaths[path] = dirty
    },
    keyFor(path: string) {
      return `${path}:${this.revisions[path] || 0}`
    },
  },
})
