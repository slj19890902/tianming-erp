<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { useRoute } from "vue-router";
import {
  mobileApi,
  backendUrl,
  stocktakeUrl,
  type ProductDetail,
  type Position,
} from "./api";
import {
  compactText,
  dimensions,
  quantity,
  safeMessage,
  shortCode,
  shortLocation,
} from "./format";
import { useMobileStore } from "./store";
import MobileIcon from "./components/MobileIcon.vue";
import MobileSheet from "./components/MobileSheet.vue";
const route = useRoute(),
  mobile = useMobileStore();
const detail = ref<ProductDetail | null>(null),
  loading = ref(false),
  error = ref(""),
  drawingOpen = ref(false),
  drawingIndex = ref(0),
  drawingError = ref(false),
  locationOpen = ref(false);
let controller: AbortController | null = null;
const product = computed(() => detail.value?.product),
  production = computed(() => detail.value?.production);
const stock = computed(() => detail.value?.inventory.summary?.finished);
const positions = computed(() => {
  const groups = new Map<number, Position>();
  for (const row of detail.value?.inventory.items || []) {
    if (row.inventory_type !== "finished" || row.shared_confirmed) continue;
    const old = groups.get(row.location_id);
    if (old) {
      old.actual += row.actual;
      old.available += row.available;
    } else groups.set(row.location_id, { ...row });
  }
  return [...groups.values()];
});
const mainLocations = computed(() =>
  [
    ...new Set(
      positions.value.map((p) =>
        shortLocation(p.location_label)
          .replace(/\s*(左|右)\d+.*$/, "")
          .trim(),
      ),
    ),
  ].join("、"),
);
const flow = computed(
  () =>
    production.value?.process_steps
      ?.map((s) => compactText(s.label))
      .join(" → ") ||
    compactText(production.value?.production_process) ||
    "—",
);
const drawings = computed(
  () =>
    product.value?.drawings?.items.filter((d) => backendUrl(d.preview_url)) ||
    [],
);
const drawing = computed(() => drawings.value[drawingIndex.value]);
const drawingSrc = computed(() =>
  drawing.value ? backendUrl(drawing.value.preview_url) : "",
);
async function load() {
  controller?.abort();
  controller = new AbortController();
  const current = controller;
  detail.value = null;
  error.value = "";
  loading.value = true;
  drawingOpen.value = false;
  locationOpen.value = false;
  try {
    const result = await mobileApi.product(
      String(route.params.id),
      current.signal,
    );
    if (!current.signal.aborted) detail.value = result;
  } catch (e) {
    if (!current.signal.aborted) error.value = safeMessage(e);
  } finally {
    if (controller === current) loading.value = false;
  }
}
function openDrawing() {
  drawingError.value = false;
  drawingIndex.value = 0;
  drawingOpen.value = true;
}
function stocktake() {
  if (positions.value.length === 1)
    window.location.assign(stocktakeUrl(positions.value[0]!.location_id));
  else locationOpen.value = true;
}
watch(() => route.params.id, load, { immediate: true });
onBeforeUnmount(() => controller?.abort());
</script>
<template>
  <section>
    <div v-if="loading" class="m-empty" role="status">加载中…</div>
    <div v-else-if="error" class="m-status" role="alert">
      {{ error }}<button @click="load">重试</button>
    </div>
    <template v-else-if="product">
      <div class="m-codehead">
        <strong>{{ shortCode(product.product_code) }}</strong
        ><span class="m-tag">{{ product.is_active ? "在用" : "停用" }}</span>
      </div>
      <h2 class="m-product-name">{{ compactText(product.product_name) }}</h2>
      <div class="m-product-sub">
        客户 <b>{{ compactText(product.customer_name) }}</b> ·
        {{ compactText(product.specification) }}
      </div>
      <section class="m-card m-stockcard">
        <span class="m-label">可用库存</span>
        <div class="m-stock-number">
          {{ quantity(stock?.available) }}
          <small>{{ stock?.unit || "" }}</small>
        </div>
        <div class="m-stock-locations">
          <div
            v-for="position in positions"
            :key="position.location_id"
            class="m-stock-location"
          >
            <MobileIcon name="pin" :size="18" /><b>{{
              shortLocation(position.location_label)
            }}</b
            ><span
              >实存 <b>{{ quantity(position.actual) }}</b> / 可用
              <b>{{ quantity(position.available) }}</b></span
            >
          </div>
        </div>
        <div class="m-ministats">
          <div
            v-for="(item, key) in detail?.inventory.source_summary"
            :key="key"
          >
            <b>{{ quantity(item.actual) }}</b
            ><span>{{ compactText(item.label) }}</span>
          </div>
        </div>
      </section>
      <section class="m-card m-craft">
        <h2>工艺信息</h2>
        <dl>
          <div class="full">
            <dt>工艺流程</dt>
            <dd class="m-flow">{{ flow }}</dd>
          </div>
          <div>
            <dt>模具位置</dt>
            <dd>
              {{
                production?.molds
                  ?.map((m) =>
                    shortLocation(
                      m.location_short_label ||
                        m.short_label ||
                        m.location_label,
                    ),
                  )
                  .join("、") || "—"
              }}
            </dd>
          </div>
          <div>
            <dt>印刷图纸</dt>
            <dd>
              {{
                drawings.length ? `图-${shortCode(product.product_code)}` : "—"
              }}
            </dd>
          </div>
          <div>
            <dt>结合方式</dt>
            <dd>{{ compactText(production?.joining_method) || "—" }}</dd>
          </div>
          <div>
            <dt>成品位置</dt>
            <dd>{{ mainLocations || "—" }}</dd>
          </div>
          <div>
            <dt>压线尺寸</dt>
            <dd>
              {{
                production?.crease_values_mm?.some((v) => v != null)
                  ? production.crease_values_mm
                      .map((v) => quantity(v))
                      .join(" / ")
                  : "—"
              }}
            </dd>
          </div>
          <div>
            <dt>片料尺寸</dt>
            <dd>
              {{
                dimensions(
                  production?.report_length_mm,
                  production?.report_width_mm,
                )
              }}
            </dd>
          </div>
        </dl>
      </section>
      <div class="m-actions">
        <button
          class="m-outline"
          :disabled="!drawings.length"
          @click="openDrawing"
        >
          <MobileIcon name="doc" />查看图纸</button
        ><button
          v-if="
            mobile.entry('warehouse')?.can_execute ||
            mobile.entry('warehouse')?.can_correct
          "
          class="m-primary"
          :disabled="!positions.length"
          @click="stocktake"
        >
          盘点
        </button>
      </div>
    </template>
    <MobileSheet
      v-if="drawingOpen"
      :title="`印刷图纸 图-${shortCode(product?.product_code)}`"
      @close="drawingOpen = false"
      ><div v-if="drawings.length > 1" class="m-drawing-tabs">
        <button
          v-for="(item, i) in drawings"
          :key="item.id"
          :class="{ on: drawingIndex === i }"
          @click="
            drawingIndex = i;
            drawingError = false;
          "
        >
          图-{{ shortCode(product?.product_code) }} · {{ i + 1 }}
        </button>
      </div>
      <div v-if="drawingError" class="m-status">图纸加载失败</div>
      <img
        v-else
        :src="drawingSrc"
        :alt="`图-${shortCode(product?.product_code)}`"
        class="m-drawing"
        @error="drawingError = true"
    /></MobileSheet>
    <MobileSheet
      v-if="locationOpen"
      title="选择盘点货位"
      @close="locationOpen = false"
      ><a
        v-for="position in positions"
        :key="position.location_id"
        class="m-location-result"
        :href="stocktakeUrl(position.location_id)"
        >{{ shortLocation(position.location_label)
        }}<b>{{ quantity(position.actual) }}</b></a
      ></MobileSheet
    >
  </section>
</template>
