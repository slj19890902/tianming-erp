<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { mobilePage } from "./navigation";
import { useAuthStore } from "../stores/auth";
import { mobileApi, backendUrl, type Product, type Unfulfilled } from "./api";
import {
  safeMessage,
  shortLocation,
  mobileQrRoute,
  shortCode,
  compactText,
  quantity,
} from "./format";
import ProductRow from "./components/ProductRow.vue";
import MobileIcon from "./components/MobileIcon.vue";
import MobileScanner from "./components/MobileScanner.vue";
const router = useRouter(),
  route = useRoute();
const auth = useAuthStore();
const tab = ref(["product", "board", "location"].includes(String(route.query.tab)) ? String(route.query.tab) : "product"),
  query = ref(String(route.query.q || "")),
  length = ref(String(route.query.length || "")),
  width = ref(String(route.query.width || "")),
  height = ref(String(route.query.height || "")),
  productTolerance = ref([5,10,20,50,100].includes(Number(route.query.tolerance)) ? Number(route.query.tolerance) : 20),
  tolerance = ref(5),
  searchMode = ref<"keyword" | "dimensions">(route.query.mode === "dimensions" ? "dimensions" : "keyword");
const orderItems = ref<Unfulfilled[]>([]);
const products = ref<Product[]>([]),
  locations = ref<
    {
      location_id: number;
      location_name: string;
    short_location_label?: string;
    employee_location_name?: string;
      floor_code: string;
      area_code: string;
    }[]
  >([]);
const loading = ref(false),
  error = ref(""),
  searched = ref(false),
  scanner = ref(false),
  page = ref(mobilePage(route.query.page)),
  total = ref(0);
let controller: AbortController | null = null;
function remember(nextPage: number, mode = searchMode.value) {
  void router.replace({ path: "/mobile/query", query: route.query.unfulfilled === "1"
    ? { unfulfilled: "1", page: String(nextPage) }
    : { tab: tab.value, mode, q: query.value.trim() || undefined, length: length.value || undefined,
        width: width.value || undefined, height: height.value || undefined,
        tolerance: String(productTolerance.value), page: String(nextPage) } });
}
async function loadOrders(nextPage = 1) {
  page.value = nextPage;
  total.value = 0;
  remember(nextPage);
  controller?.abort();
  controller = new AbortController();
  const current = controller;
  loading.value = true;
  error.value = "";
  searched.value = true;
  orderItems.value = [];
  try {
    const result = await mobileApi.unfulfilled(nextPage, current.signal);
    if (!current.signal.aborted) {
      const lastPage = Math.max(1, Math.ceil((result.groups[0]?.total || 0) / 12));
      if (nextPage > lastPage) { await loadOrders(lastPage); return; }
      orderItems.value = result.groups[0]?.items || [];
      total.value = result.groups[0]?.total || 0;
      page.value = result.groups[0]?.page || nextPage;
    }
  } catch (e) {
    if (!current.signal.aborted) error.value = safeMessage(e);
  } finally {
    if (controller === current) loading.value = false;
  }
}
async function search(nextPage = 1, mode = searchMode.value) {
  if (tab.value === "location" || mode === "keyword") { if (!query.value.trim()) return; }
  searchMode.value = mode;
  page.value = nextPage;
  remember(nextPage, mode);
  controller?.abort();
  controller = new AbortController();
  const current = controller;
  loading.value = true;
  error.value = "";
  products.value = [];
  locations.value = [];
  searched.value = true;
  total.value = 0;
  try {
    if (tab.value === "location") {
      const result = await mobileApi.locations(
        query.value.trim(),
        current.signal,
        nextPage,
      );
      if (!current.signal.aborted) {
        const lastPage = Math.max(1, Math.ceil(result.total / 12));
        if (nextPage > lastPage) { await search(lastPage, mode); return; }
        locations.value = result.items;
        total.value = result.total;
        page.value = nextPage;
      }
    } else {
      const result = await mobileApi.products(
        new URLSearchParams({
          ...(mode === "dimensions" ? { dimension_basis: "finished", sort_by: "dimension_match",
            length: length.value, width: width.value, ...(height.value ? { height: height.value } : {}),
            tolerance_mm: String(productTolerance.value) } : { q: query.value.trim() }),
          page: String(nextPage), page_size: "12",
        }),
        current.signal,
      );
      if (!current.signal.aborted) {
        const lastPage = Math.max(1, Math.ceil(result.total / 12));
        if (nextPage > lastPage) { await search(lastPage, mode); return; }
        products.value = result.items;
        total.value = result.total;
        page.value = nextPage;
      }
    }
  } catch (e) {
    if (!current.signal.aborted) error.value = safeMessage(e);
  } finally {
    if (controller === current) loading.value = false;
  }
}
function findDimensions() {
  if (tab.value === "product") {
    controller?.abort();
    loading.value = false;
    if (![length.value, width.value].every(v => Number.isFinite(Number(v)) && Number(v) > 0) ||
        (height.value && !(Number.isFinite(Number(height.value)) && Number(height.value) > 0))) {
      error.value = "请输入有效成品长宽，高可留空"; return;
    }
    void search(1, "dimensions"); return;
  }
  if (!(Number(length.value) > 0 && Number(width.value) > 0)) {
    error.value = "请输入有效长宽";
    return;
  }
  void router.push({
    path: "/mobile/recognize",
    query: {
      length: length.value,
      width: width.value,
      tolerance: String(tolerance.value),
    },
  });
}
function scanned(value: string) {
  scanner.value = false;
  // Preserve the existing scanner route for signed production/warehouse QR tokens.
  if (/^(https?:\/\/|\/)/.test(value)) {
    const target = mobileQrRoute(
      value,
      window.location.origin,
      backendUrl("/"),
    );
    if (!target) {
      error.value = "条码地址无效";
      return;
    }
    window.location.assign(backendUrl(target));
    return;
  }
  query.value = value.trim();
  void search(1, "keyword");
}
watch(tab, () => {
  controller?.abort();
  loading.value = false;
  total.value = 0;
  page.value = 1;
  products.value = [];
  locations.value = [];
  error.value = "";
  searched.value = false;
});
watch(() => auth.user?.id, () => {
  controller?.abort(); loading.value = false;
  products.value = []; locations.value = []; orderItems.value = [];
  total.value = 0; searched.value = false;
});
if (route.query.unfulfilled === "1") void loadOrders(page.value);
else if (query.value || (searchMode.value === "dimensions" && length.value && width.value)) void search(page.value);
onBeforeUnmount(() => controller?.abort());
</script>
<template>
  <section>
    <template v-if="route.query.unfulfilled === '1'"
      ><h2>未送齐订单</h2>
      <div v-if="error" class="m-status" role="alert">{{ error }}<button @click="loadOrders(page)">重试</button></div>
      <div v-if="loading" class="m-empty">加载中…</div>
      <div v-else-if="!orderItems.length && !error" class="m-empty">暂无未送齐订单</div>
      <RouterLink
        v-for="item in orderItems"
        :key="item.order_item_id"
        :to="
          item.product_id
            ? { path: `/mobile/products/${item.product_id}`, query: { from: route.fullPath } }
            : '/mobile/query'
        "
        class="m-product-row"
        ><strong class="m-code">{{ shortCode(item.product_code) }}</strong
        ><span class="m-product-mid"
          ><b>{{ compactText(item.product_name) }}</b
          ><span>{{ compactText(item.customer_name) }}</span
          ><span>{{ compactText(item.specification) }}</span></span
        ><span class="m-product-stock"
          ><b>{{ quantity(item.remaining_quantity) }}</b
          ><span>未送齐</span></span
        ></RouterLink
      >
      <div v-if="total > 12" class="m-pagination">
        <button :disabled="loading || page === 1" @click="loadOrders(page - 1)">
          上一页</button
        ><span>{{ page }} / {{ Math.ceil(total / 12) }}</span
        ><button :disabled="loading || page * 12 >= total" @click="loadOrders(page + 1)">
          下一页
        </button>
      </div></template
    >
    <template v-else
      ><div class="m-seg" role="tablist" aria-label="查询方式">
        <button
          v-for="t in [
            { id: 'product', label: '找产品' },
            { id: 'board', label: '认纸板' },
            { id: 'location', label: '查货位' },
          ]"
          :key="t.id"
          role="tab"
          :aria-selected="tab === t.id"
          :class="{ on: tab === t.id }"
          @click="tab = t.id"
        >
          {{ t.label }}
        </button>
      </div>
      <form
        v-if="tab !== 'location'"
        class="m-card m-sizecard"
        @submit.prevent="findDimensions"
      >
        <h2><MobileIcon name="ruler" />{{ tab === "product" ? "成品大致尺寸" : "片料尺寸" }}</h2>
        <div :class="['m-sizeinputs', { 'm-carton-inputs': tab === 'product' }]">
          <label
            >长<input
              v-model="length"
              inputmode="decimal"
              type="number"
              min="0.01"
              step="0.01"
              required
              :aria-label="tab === 'product' ? '成品长' : '纸板长'" /></label
          ><span>×</span
          ><label
            >宽<input
              v-model="width"
              inputmode="decimal"
              type="number"
              min="0.01"
              step="0.01"
              required
              :aria-label="tab === 'product' ? '成品宽' : '纸板宽'" /></label
          ><label v-if="tab === 'product'">高<input v-model="height" inputmode="decimal" type="number" min="0.01" step="0.01" placeholder="可留空" aria-label="成品高" /></label><span>mm</span>
        </div>
        <label v-if="tab === 'product'" class="m-size-tolerance">允许误差 ± <select v-model="productTolerance" aria-label="成品尺寸误差"><option v-for="value in [5, 10, 20, 50, 100]" :key="value" :value="value">{{ value }} mm</option></select></label>
        <button class="m-primary m-wide" :disabled="loading">{{ tab === 'product' ? '找产品' : '认纸板' }}</button>
      </form>
      <form
        v-if="tab !== 'board'"
        class="m-searchrow"
        @submit.prevent="search(1, 'keyword')"
      >
        <input
          v-model="query"
          :placeholder="tab === 'location' ? '货位简称' : '短码、客户或品名'"
          aria-label="查询内容"
          maxlength="100"
        /><button class="m-primary" :disabled="loading">查询</button
        ><button
          class="m-icon-btn m-scan"
          type="button"
          aria-label="扫码"
          @click="scanner = true"
        >
          <MobileIcon name="scan" />
        </button>
      </form>
      <div v-if="error" class="m-status" role="alert">{{ error }}<button @click="search(page)">重试</button></div>
      <div v-else-if="loading" class="m-empty" role="status">查询中…</div>
      <div
        v-else-if="searched && !products.length && !locations.length"
        class="m-empty"
      >
        无匹配结果
      </div>
      <div class="m-result-list">
        <ProductRow
          v-for="product in products"
          :key="product.id"
          :product="product"
        /><RouterLink
          v-for="location in locations"
          :key="location.location_id"
          class="m-location-result"
          :to="{
            path: '/mobile/warehouse',
            query: {
              floor: location.floor_code,
              area: location.area_code,
              location: location.location_id,
            },
          }"
          ><MobileIcon name="pin" />{{ shortLocation(location.short_location_label || location.employee_location_name || location.location_name)
          }}<MobileIcon name="chevron"
        /></RouterLink>
      </div>
      <div v-if="total > 12 && (products.length || locations.length)" class="m-pagination">
        <button :disabled="loading || page === 1" @click="search(page - 1)">上一页</button
        ><span>{{ page }} / {{ Math.ceil(total / 12) }}</span
        ><button :disabled="loading || page * 12 >= total" @click="search(page + 1)">
          下一页
        </button>
      </div> </template
    ><MobileScanner
      v-if="scanner"
      @close="scanner = false"
      @decoded="scanned"
    />
  </section>
</template>
