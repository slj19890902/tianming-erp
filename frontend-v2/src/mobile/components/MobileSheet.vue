<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
const props = defineProps<{ title: string }>(),
  emit = defineEmits<{ close: [] }>();
const dialog = ref<HTMLDialogElement>();
let previous: HTMLElement | null = null;
onMounted(() => {
  previous = document.activeElement as HTMLElement;
  dialog.value?.showModal();
});
onBeforeUnmount(() => {
  dialog.value?.close();
  previous?.focus();
});
</script>
<template>
  <dialog
    ref="dialog"
    class="m-native-sheet"
    :aria-label="props.title"
    @cancel.prevent="emit('close')"
    @click="
      (e) => {
        if (e.target === dialog) emit('close');
      }
    "
  >
    <section class="m-sheet">
      <header>
        <h2>{{ title }}</h2>
        <button class="m-icon-btn" aria-label="关闭" @click="emit('close')">
          ×
        </button>
      </header>
      <slot />
    </section>
  </dialog>
</template>
