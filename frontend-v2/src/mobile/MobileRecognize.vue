<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useAuthStore } from "../stores/auth";
import { mobilePage } from "./navigation";
import { mobileApi, type Product } from "./api";
import { safeMessage } from "./format";
import ProductRow from "./components/ProductRow.vue";
const route = useRoute(),
  router = useRouter(),
  auth = useAuthStore();
const tolerance = ref(Number(route.query.tolerance ?? 5)),
  items = ref<Product[]>([]),
  error = ref(""),
  loading = ref(false),
  page = ref(mobilePage(route.query.page)),
  total = ref(0);
let controller: AbortController | null = null;
async function load(nextPage = 1) {
  page.value = nextPage;
  controller?.abort();
  controller = new AbortController();
  const current = controller;
  items.value = [];
  error.value = "";
  total.value = 0;
  if (
    !(
      Number(route.query.length) > 0 &&
      Number(route.query.width) > 0 &&
      Number.isFinite(tolerance.value) &&
      tolerance.value >= 0 &&
      tolerance.value <= 100
    )
  ) {
    loading.value = false;
    error.value = "长宽或容差无效";
    return;
  }
  loading.value = true;
  try {
    const result = await mobileApi.products(
      new URLSearchParams({
        dimension_basis: "report",
        length: String(route.query.length),
        width: String(route.query.width),
        tolerance_mm: String(tolerance.value),
        page: String(nextPage),
        page_size: "12",
      }),
      current.signal,
    );
    if (!current.signal.aborted) {
      const lastPage = Math.max(1, Math.ceil(result.total / 12));
      if (nextPage > lastPage) { await load(lastPage); return; }
      items.value = result.items;
      total.value = result.total;
      page.value = nextPage;
      void router.replace({
        query: { ...route.query, tolerance: String(tolerance.value), page: String(nextPage) },
      });
    }
  } catch (e) {
    if (!current.signal.aborted) error.value = safeMessage(e);
  } finally {
    if (controller === current) loading.value = false;
  }
}
watch(
  () => [route.query.length, route.query.width],
  () => load(mobilePage(route.query.page)),
  { immediate: true },
);
watch(() => auth.user?.id, () => {
  controller?.abort(); loading.value = false; items.value = []; total.value = 0; error.value = "";
});
onBeforeUnmount(() => controller?.abort());
</script>
<template>
  <section>
    <form class="m-card m-measure" @submit.prevent="load()">
      <b>{{ route.query.length }} × {{ route.query.width }} <small>mm</small></b
      ><label
        >容差 ±<input
          v-model.number="tolerance"
          aria-label="尺寸容差"
          type="number"
          min="0"
          max="100"
          step="1"
          @change="load()"
        />mm</label
      >
    </form>
    <div v-if="loading" class="m-empty" role="status">查询中…</div>
    <div v-else-if="error" class="m-status" role="alert">
      {{ error }}<button @click="load(page)">重试</button>
    </div>
    <div v-else-if="!items.length" class="m-empty">无匹配产品</div>
    <div class="m-result-list">
      <ProductRow
        v-for="product in items"
        :key="product.id"
        :product="product"
      />
    </div>
    <div v-if="total > 12" class="m-pagination">
      <button :disabled="loading || page === 1" @click="load(page - 1)">上一页</button
      ><span>{{ page }} / {{ Math.ceil(total / 12) }}</span
      ><button :disabled="loading || page * 12 >= total" @click="load(page + 1)">
        下一页
      </button>
    </div>
  </section>
</template>
