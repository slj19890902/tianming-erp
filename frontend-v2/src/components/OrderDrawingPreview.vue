<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { orderApi } from '../api/client'
const props = defineProps<{ source: string; title?: string }>()
const emit = defineEmits<{ close: [] }>()
const contentURL = ref(''), contentType = ref(''), busy = ref(false), error = ref('')
let serial = 0
function clear() { if (contentURL.value) URL.revokeObjectURL(contentURL.value); contentURL.value = '' }
watch(() => props.source, async source => {
  const current = ++serial; clear(); error.value = ''; busy.value = Boolean(source)
  if (!source) return
  try {
    const content = await orderApi.drawingContent(source)
    if (current !== serial) return
    contentType.value = content.type; contentURL.value = URL.createObjectURL(content)
  } catch (cause) { if (current === serial) error.value = cause instanceof Error ? cause.message : '图纸读取失败' }
  finally { if (current === serial) busy.value = false }
}, { immediate: true })
onBeforeUnmount(() => { serial++; clear() })
</script>
<template>
  <el-dialog :model-value="Boolean(source)" :title="title || '查看图纸'" width="min(900px, 94vw)" @close="emit('close')">
    <p v-if="busy" role="status">正在读取图纸…</p>
    <p v-else-if="error" role="alert">{{ error }}</p>
    <iframe v-else-if="contentURL && contentType === 'application/pdf'" :src="contentURL" title="订单PDF图纸预览" style="width:100%;height:65vh;border:0" />
    <img v-else-if="contentURL && contentType.startsWith('image/')" :src="contentURL" alt="订单图纸预览" style="display:block;max-width:100%;max-height:65vh;margin:auto" />
    <p v-else-if="contentURL">此历史图纸暂不能在页面内预览，<a :href="contentURL" download="历史图纸">下载原图纸</a>。</p>
    <template #footer><el-button @click="emit('close')">关闭图纸</el-button></template>
  </el-dialog>
</template>
