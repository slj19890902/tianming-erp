<script setup lang="ts">
withDefaults(defineProps<{ name: string; size?: number }>(), { size: 24 });
const paths: Record<string, string> = {
  search: "M21 21l-4.3-4.3 M18 11a7 7 0 1 1-14 0 7 7 0 0 1 14 0",
  home: "M3 11.5 12 4l9 7.5 M5.5 10v10h13V10",
  box: "M3 8l9-4.5L21 8v8l-9 4.5L3 16Z M3 8l9 4.5L21 8 M12 12.5V20",
  grid: "M4 4h6v6H4Z M14 4h6v6h-6Z M4 14h6v6H4Z M14 14h6v6h-6Z",
  truck:
    "M2.5 6h12v11h-12Z M14.5 10h4l3 3.5V17h-7 M8 18a2 2 0 1 1-4 0 2 2 0 0 1 4 0 M20 18a2 2 0 1 1-4 0 2 2 0 0 1 4 0",
  receive: "M12 3v11 M7.5 9.5 12 14l4.5-4.5 M4 19h16",
  back: "m15 5-7 7 7 7",
  chevron: "m9 5 7 7-7 7",
  scan: "M4 8V4h4 M16 4h4v4 M20 16v4h-4 M8 20H4v-4 M4 12h16",
  ruler: "M3 17 17 3l4 4L7 21Z M8 16l2-2 M11 13l2-2 M14 10l2-2",
  pin: "M12 21S5 15 5 10a7 7 0 0 1 14 0c0 5-7 11-7 11Z M14 10a2 2 0 1 1-4 0 2 2 0 0 1 4 0",
  doc: "M5 3h9l5 5v13H5Z M14 3v6h5 M8 13h8 M8 17h6",
  more: "M5 12h.01 M12 12h.01 M19 12h.01",
  refresh: "M20 10a8 8 0 1 0-2 8 M20 4v6h-6",
  close: "m6 6 12 12 M6 18 18 6",
};
</script>
<template>
  <svg
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.8"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <path :d="paths[name] || paths.box" />
  </svg>
</template>
