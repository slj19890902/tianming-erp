import { defineStore } from "pinia";
import { ref } from "vue";
import { mobileApi, type Shell } from "./api";
export const useMobileStore = defineStore("mobile", () => {
  const shell = ref<Shell | null>(null);
  const error = ref("");
  let inflight: Promise<void> | null = null;
  let generation = 0;
  const allowed = (id: string) =>
    !!shell.value?.entries.some((e) => e.id === id);
  const entry = (id: string) => shell.value?.entries.find((e) => e.id === id);
  async function load() {
    if (inflight) return inflight;
    const token = ++generation;
    inflight = (async () => {
      try {
        const result = await mobileApi.shell();
        if (token === generation) {
          shell.value = result;
          error.value = "";
        }
      } catch (e) {
        if (token === generation) {
          shell.value = null;
          error.value = e instanceof Error ? e.message : "加载失败";
        }
      } finally {
        if (token === generation) inflight = null;
      }
    })();
    return inflight;
  }
  function clear() {
    generation++;
    inflight = null;
    shell.value = null;
    error.value = "";
  }
  return { shell, error, allowed, entry, load, clear };
});
