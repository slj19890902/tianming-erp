<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import { mobileApi, warehouseAreaUrl, type Floor, type Area } from "./api";
import { compactText, safeMessage, shortLocation } from "./format";
const route = useRoute();
const floors = ref<Floor[]>([]), floor = ref(""), areas = ref<Area[]>([]);
const viewport = ref<HTMLElement>(), zoom = ref(1), rotated = ref(false);
const loading = ref(false), error = ref("");
let controller: AbortController | null = null, sequence = 0;
const box = computed(() => {
  const points = areas.value.flatMap(a => a.points).filter(p => p.length >= 2 && p.every(Number.isFinite));
  if (!points.length) return { x: 0, y: 0, w: 100, h: 100 };
  const xs = points.map(p => p[0]!), ys = points.map(p => -p[1]!);
  const x = Math.min(...xs), y = Math.min(...ys), w = Math.max(...xs) - x, h = Math.max(...ys) - y;
  const pad = Math.max(w, h) * .02;
  return { x: x - pad, y: y - pad, w: Math.max(w + 2 * pad, 1), h: Math.max(h + 2 * pad, 1) };
});
const bounds = computed(() => {
  const b = box.value;
  return rotated.value ? `${-b.y-b.h} ${b.x} ${b.h} ${b.w}` : `${b.x} ${b.y} ${b.w} ${b.h}`;
});
const fontSize = computed(() => Math.max(box.value.w, box.value.h) * .028);
function center(a: Area) { return [a.points.reduce((n,p) => n+p[0]!,0)/a.points.length, -a.points.reduce((n,p) => n+p[1]!,0)/a.points.length]; }
function fit() {
  zoom.value = 1;
  const el = viewport.value, b = box.value;
  if (el) rotated.value = Math.min(el.clientWidth/b.h, el.clientHeight/b.w) > Math.min(el.clientWidth/b.w, el.clientHeight/b.h);
}
async function loadFloor(code: string) {
  controller?.abort(); controller = new AbortController(); const current = controller, token = ++sequence;
  floor.value = code; areas.value = []; zoom.value = 1; loading.value = true; error.value = "";
  try {
    const data = await mobileApi.overviewMap(code, current.signal);
    if (token !== sequence) return;
    areas.value = data.areas;
    const requested = String(route.query.area || "");
    if (requested && data.areas.some(a => a.area_code === requested)) {
      window.location.assign(warehouseAreaUrl(code, requested, String(route.query.location || "")));
      return;
    }
    await nextTick(); fit();
  } catch (e) { if (!current.signal.aborted) error.value = safeMessage(e); }
  finally { if (token === sequence) loading.value = false; }
}
async function load() {
  loading.value = true; error.value = "";
  try {
    const result = await mobileApi.floors(); floors.value = result.floors;
    const first = result.floors.find(f => f.floor_code === route.query.floor) || result.floors.find(f => f.floor_number === 3) || result.floors[0];
    if (first) await loadFloor(first.floor_code); else loading.value = false;
  } catch (e) { error.value = safeMessage(e); loading.value = false; }
}
onMounted(load);
onBeforeUnmount(() => { sequence++; controller?.abort(); });
</script>
<template>
  <section class="m-warehouse">
    <div class="m-maptools">
      <div class="m-floorseg"><button v-for="item in floors" :key="item.floor_code" :class="{on: floor === item.floor_code}" @click="loadFloor(item.floor_code)">{{ compactText(item.floor_name) }}</button></div>
      <button @click="fit">全图</button><button aria-label="旋转地图" @click="rotated = !rotated">↻</button><button aria-label="缩小地图" :disabled="zoom <= 1" @click="zoom = Math.max(1,zoom-.25)">−</button><button aria-label="放大地图" :disabled="zoom >= 3" @click="zoom = Math.min(3,zoom+.25)">＋</button>
    </div>
    <div v-if="error" class="m-status" role="alert">{{ error }}<button @click="load">重试</button></div>
    <div v-if="loading" class="m-map-loading" role="status">加载中…</div>
    <div ref="viewport" class="m-mapview" tabindex="0" aria-label="可缩放仓库地图">
      <svg :viewBox="bounds" :style="{width: `${zoom*100}%`, height: `${zoom*100}%`}" role="img" aria-label="实测仓库平面图">
        <g :transform="rotated ? 'rotate(90)' : undefined">
          <a v-for="item in areas" :key="item.feature_id" :href="warehouseAreaUrl(floor,item.area_code)" class="m-map-area" :aria-label="shortLocation(item.area_name)">
            <polygon :points="item.points.map(p => `${p[0]},${-p[1]!}`).join(' ')" />
            <text :x="center(item)[0]" :y="center(item)[1]" :font-size="fontSize" :transform="rotated ? `rotate(-90 ${center(item).join(' ')})` : undefined" text-anchor="middle" dominant-baseline="central">{{ shortLocation(item.area_name) }}</text>
          </a>
        </g>
      </svg>
      <div v-if="!loading && !error && !areas.length" class="m-empty">暂无实测地图</div>
    </div>
  </section>
</template>
