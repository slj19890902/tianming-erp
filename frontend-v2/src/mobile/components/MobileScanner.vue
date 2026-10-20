<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import { Html5Qrcode } from "html5-qrcode";
import MobileSheet from "./MobileSheet.vue";
const emit = defineEmits<{ close: []; decoded: [value: string] }>();
const readerId = `mobile-camera-${Math.random().toString(36).slice(2)}`;
const error = ref(""),
  input = ref("");
let scanner: Html5Qrcode | null = null,
  closed = false,
  starting: Promise<unknown> | null = null;
async function stop() {
  const current = scanner;
  if (!current) return;
  try {
    await starting;
  } catch {
    /* startup refusal still needs cleanup */
  }
  if (scanner === current) scanner = null;
  try {
    if (current.isScanning) await current.stop();
    await current.clear();
  } catch {
    /* already released */
  }
}
async function decoded(value: string) {
  if (closed || !value.trim()) return;
  closed = true;
  await stop();
  emit("decoded", value);
}
function close() {
  closed = true;
  emit("close");
  void stop();
}
onMounted(async () => {
  await nextTick();
  if (closed) return;
  const current = new Html5Qrcode(readerId);
  scanner = current;
  try {
    starting = current.start(
      { facingMode: "environment" },
      { fps: 10, qrbox: { width: 220, height: 220 } },
      decoded,
      () => {},
    );
    await starting;
    if (closed) await stop();
  } catch {
    if (!closed) error.value = "摄像头不可用";
    await stop();
  }
});
onBeforeUnmount(() => {
  closed = true;
  void stop();
});
</script>
<template>
  <MobileSheet title="扫码查询" @close="close"
    ><div :id="readerId" />
    <p v-if="error" role="alert">{{ error }}</p>
    <form class="m-searchrow" @submit.prevent="decoded(input)">
      <input
        v-model="input"
        aria-label="手工输入条码"
        placeholder="输入条码"
        required
      /><button class="m-primary">查询</button>
    </form></MobileSheet
  >
</template>
