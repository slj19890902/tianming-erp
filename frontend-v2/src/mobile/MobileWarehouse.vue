<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import {
  mobileApi,
  stocktakeUrl,
  type Floor,
  type Area,
  type Location,
} from "./api";
import {
  compactText,
  quantity,
  safeMessage,
  shortCode,
  shortLocation,
} from "./format";
import MobileIcon from "./components/MobileIcon.vue";
const route = useRoute();
const floors = ref<Floor[]>([]),
  floor = ref(""),
  areas = ref<Area[]>([]),
  area = ref(""),
  locations = ref<Location[]>([]),
  selected = ref<Location | null>(null);
const zoom = ref(1),
  loading = ref(false),
  error = ref(""),
  canStocktake = ref(false);
let controller: AbortController | null = null,
  sequence = 0;
const bounds = computed(() => {
  const points = areas.value
    .flatMap((a) => a.points)
    .filter((p) => p.length >= 2 && p.every(Number.isFinite));
  if (!points.length) return "0 0 100 100";
  const xs = points.map((p) => p[0]!),
    ys = points.map((p) => p[1]!),
    left = Math.min(...xs),
    top = Math.min(...ys),
    w = Math.max(...xs) - left,
    h = Math.max(...ys) - top,
    pad = Math.max(w, h) * 0.035;
  return `${left - pad} ${top - pad} ${Math.max(w + pad * 2, 1)} ${Math.max(h + pad * 2, 1)}`;
});
const mapFontSize = computed(() => {
  const b = bounds.value.split(" ").map(Number);
  return Math.max(b[2]!, b[3]!) * 0.032;
});
const activeArea = computed(() =>
  areas.value.find((a) => a.area_code === area.value),
);
const areaBounds = computed(() => {
  const points = activeArea.value?.points || [];
  if (!points.length) return { x: 0, y: 0, w: 1, h: 1 };
  const xs = points.map((p) => p[0]!),
    ys = points.map((p) => p[1]!);
  return {
    x: Math.min(...xs),
    y: Math.min(...ys),
    w: Math.max(...xs) - Math.min(...xs),
    h: Math.max(...ys) - Math.min(...ys),
  };
});
function center(a: Area) {
  return [
    a.points.reduce((n, p) => n + p[0]!, 0) / a.points.length,
    a.points.reduce((n, p) => n + p[1]!, 0) / a.points.length,
  ];
}
function geometry(l: Location) {
  const g = l.geometry!,
    b = areaBounds.value;
  return {
    x: b.x + (b.w * g.left_pct) / 100,
    y: b.y + (b.h * g.top_pct) / 100,
    width: (b.w * g.width_pct) / 100,
    height: (b.h * g.height_pct) / 100,
  };
}
async function loadFloor(code: string) {
  controller?.abort();
  controller = new AbortController();
  const current = controller;
  const token = ++sequence;
  floor.value = code;
  areas.value = [];
  area.value = "";
  locations.value = [];
  selected.value = null;
  zoom.value = 1;
  loading.value = true;
  error.value = "";
  try {
    const data = await mobileApi.overviewMap(code, current.signal);
    if (token !== sequence) return;
    areas.value = data.areas;
    if (
      route.query.area &&
      data.areas.some((a) => a.area_code === route.query.area)
    )
      await openArea(String(route.query.area));
  } catch (e) {
    if (!current.signal.aborted) error.value = safeMessage(e);
  } finally {
    if (token === sequence) loading.value = false;
  }
}
async function openArea(code: string) {
  controller?.abort();
  controller = new AbortController();
  const current = controller;
  const token = ++sequence;
  area.value = code;
  locations.value = [];
  selected.value = null;
  loading.value = true;
  error.value = "";
  try {
    const data = await mobileApi.area(floor.value, code, current.signal);
    if (token !== sequence) return;
    locations.value = data.locations;
    if (route.query.location)
      selected.value =
        data.locations.find(
          (l) => l.location_id === Number(route.query.location),
        ) || null;
  } catch (e) {
    if (!current.signal.aborted) error.value = safeMessage(e);
  } finally {
    if (token === sequence) loading.value = false;
  }
}
function whole() {
  controller?.abort();
  sequence++;
  area.value = "";
  locations.value = [];
  selected.value = null;
  zoom.value = 1;
  loading.value = false;
  error.value = "";
}
async function load() {
  loading.value = true;
  error.value = "";
  try {
    const result = await mobileApi.floors();
    floors.value = result.floors;
    canStocktake.value = result.can_execute || result.can_correct;
    const requested = String(route.query.floor || "");
    const initial =
      result.floors.find((f) => f.floor_code === requested) ||
      result.floors.find((f) => f.floor_number === 3) ||
      result.floors[0];
    if (initial) await loadFloor(initial.floor_code);
    else loading.value = false;
  } catch (e) {
    error.value = safeMessage(e);
    loading.value = false;
  }
}
onMounted(load);
onBeforeUnmount(() => {
  sequence++;
  controller?.abort();
});
</script>
<template>
  <section class="m-warehouse">
    <div class="m-maptools">
      <div class="m-floorseg">
        <button
          v-for="item in floors"
          :key="item.floor_code"
          :class="{ on: floor === item.floor_code }"
          @click="loadFloor(item.floor_code)"
        >
          {{ compactText(item.floor_name) }}
        </button>
      </div>
      <button @click="whole">全图</button
      ><button
        aria-label="缩小地图"
        :disabled="zoom <= 1"
        @click="zoom = Math.max(1, zoom - 0.25)"
      >
        −</button
      ><button
        aria-label="放大地图"
        :disabled="zoom >= 3"
        @click="zoom = Math.min(3, zoom + 0.25)"
      >
        ＋
      </button>
    </div>
    <div v-if="error" class="m-status" role="alert">
      {{ error }}<button @click="load">重试</button>
    </div>
    <div v-if="loading" class="m-map-loading" role="status">加载中…</div>
    <div class="m-mapview" tabindex="0" aria-label="可缩放仓库地图">
      <svg
        :viewBox="bounds"
        :style="{ width: `${zoom * 100}%`, height: `${zoom * 100}%` }"
        role="img"
        aria-label="实测仓库平面图"
      >
        <g
          v-for="item in areas"
          :key="item.feature_id"
          class="m-map-area"
          :class="{ active: area === item.area_code }"
          tabindex="0"
          role="button"
          :aria-label="shortLocation(item.area_name)"
          @click="openArea(item.area_code)"
          @keydown.enter.prevent="openArea(item.area_code)"
          @keydown.space.prevent="openArea(item.area_code)"
        >
          <polygon :points="item.points.map((p) => p.join(',')).join(' ')" />
          <text
            :x="center(item)[0]"
            :y="center(item)[1]"
            :font-size="mapFontSize"
            text-anchor="middle"
            dominant-baseline="central"
          >
            {{ shortLocation(item.area_name) }}
          </text>
        </g>
        <g
          v-for="location in locations.filter((l) => l.geometry)"
          :key="location.location_id"
          class="m-map-location"
          :class="{ active: selected?.location_id === location.location_id }"
          role="button"
          tabindex="0"
          :aria-label="
            shortLocation(
              location.short_location_label || location.location_name,
            )
          "
          @click.stop="selected = location"
          @keydown.enter.prevent="selected = location"
          @keydown.space.prevent="selected = location"
        >
          <rect v-bind="geometry(location)" />
          <title>
            {{
              shortLocation(
                location.short_location_label || location.location_name,
              )
            }}
          </title>
        </g>
      </svg>
    </div>
    <section v-if="area" class="m-location-strip" aria-label="货位列表">
      <button
        v-for="location in locations"
        :key="location.location_id"
        :class="{ on: selected?.location_id === location.location_id }"
        @click="selected = location"
      >
        {{
          shortLocation(location.short_location_label || location.location_name)
        }}
      </button>
    </section>
    <section v-if="selected" class="m-card m-infobar">
      <header>
        <MobileIcon name="pin" :size="20" />
        <h2>
          {{
            shortLocation(
              selected.short_location_label || selected.location_name,
            )
          }}
        </h2>
        <a
          v-if="canStocktake"
          class="m-outline"
          :href="stocktakeUrl(selected.location_id, floor, area)"
          >盘点</a
        >
      </header>
      <div
        v-for="good in selected.goods"
        :key="good.lot_id"
        class="m-info-good"
      >
        <RouterLink
          v-if="good.product_id"
          :to="`/mobile/products/${good.product_id}`"
          ><strong>{{ shortCode(good.product_code) }}</strong>
          {{ compactText(good.customer_short_name || good.customer_name)
          }}<span>{{ compactText(good.product_name) }}</span></RouterLink
        ><span v-else>{{ compactText(good.product_name) }}</span>
        <div>
          <b>{{ quantity(good.quantity_total) }}{{ good.unit }}</b
          ><span
            >可用 {{ quantity(good.quantity_available) }}{{ good.unit }}</span
          >
          <span>盘点数量 {{ quantity(good.stocktake_quantity) }}</span>
        </div>
      </div>
      <div v-if="!selected.goods.length" class="m-empty">空货位</div>
    </section>
    <div v-else-if="!loading && !error && !areas.length" class="m-empty">
      暂无实测地图
    </div>
  </section>
</template>
