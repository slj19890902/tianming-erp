<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useMobileStore } from "./store";
import { useAuthStore } from "../stores/auth";
import { legacyUrl, mobileApi, type Margin, type Overview } from "./api";
import { money, quantity, safeMessage } from "./format";
import MobileIcon from "./components/MobileIcon.vue";
const mobile = useMobileStore(),
  auth = useAuthStore();
const pendingOrders = ref<number>(),
  ordersAllowed = computed(() =>
    mobile.shell?.search_categories?.includes("orders"),
  );
const overview = ref<Overview | null>(null),
  margin = ref<Margin | null>(null),
  error = ref("");
let controller: AbortController | null = null;
const loading = ref(false);
const summaryAllowed = computed(
  () =>
    ["admin", "boss", "finance"].includes(auth.user?.role || "") &&
    mobile.shell?.management_summary_allowed,
);
const buttons = computed(() =>
  [
    { id: "lookup", title: "现场查询", path: "/mobile/query", icon: "search" },
    { id: "warehouse", title: "仓库", path: "/mobile/warehouse", icon: "box" },
    { id: "incoming", title: "收料", path: "/mobile/receive", icon: "receive" },
    {
      id: "pre_delivery",
      title: "预送货",
      path: legacyUrl("predelivery"),
      icon: "truck",
    },
  ].filter((b) => mobile.allowed(b.id)),
);
function count(key: string) {
  return (
    overview.value?.business_status_counts?.[key] ??
    overview.value?.cards?.find((c) => c.key === key)?.count
  );
}
async function load() {
  controller?.abort();
  controller = new AbortController();
  const current = controller, shell = mobile.shell;
  error.value = "";
  overview.value = null; margin.value = null; pendingOrders.value = undefined;
  loading.value = !!shell;
  if (!shell) return;
  // Independent reads must not make employees wait for the management summary.
  const reads: Promise<void>[] = [];
  const read = async (work: () => Promise<void>) => {
    try { await work(); }
    catch (e) { if (!current.signal.aborted) error.value = safeMessage(e); }
  };
  if (shell.management_summary_allowed) reads.push(read(async () => {
    const value = await mobileApi.overview(current.signal);
    if (!current.signal.aborted) overview.value = value;
  }));
  if (shell.search_categories?.includes("orders")) reads.push(read(async () => {
    const value = await mobileApi.unfulfilled(1, current.signal);
    if (!current.signal.aborted) pendingOrders.value = value.groups[0]?.total;
  }));
  if (summaryAllowed.value && shell.delivery_margin_allowed) reads.push(read(async () => {
    const value = await mobileApi.margin(current.signal);
    if (!current.signal.aborted) margin.value = value;
  }));
  await Promise.all(reads);
  if (controller === current) loading.value = false;
}
watch(() => mobile.shell, load);
onMounted(load);
onBeforeUnmount(() => controller?.abort());
</script>
<template>
  <section class="m-home">
    <div class="m-workheading"><div><h2>今天要做什么？</h2><p>找到货物，直接办事</p></div>
      <button class="m-icon-btn" aria-label="刷新工作台" :disabled="loading" @click="load"><MobileIcon name="refresh" /></button>
    </div>
    <div class="m-grid2">
      <template v-for="button in buttons" :key="button.id">
        <a
          v-if="button.id === 'pre_delivery'"
          :href="button.path"
          class="m-bigbtn"
          ><span class="m-icon-tile"
            ><MobileIcon :name="button.icon" :size="28" /></span
          ><b>{{ button.title }}</b></a
        >
        <RouterLink v-else :to="button.path" class="m-bigbtn"
          ><span class="m-icon-tile"
            ><MobileIcon :name="button.icon" :size="28" /></span
          ><b>{{ button.title }}</b
          ><span
            v-if="button.id === 'incoming' && count('pending_incoming')"
            class="m-badge"
            >{{ quantity(count("pending_incoming")) }}</span
          ></RouterLink
        >
      </template>
    </div>
    <div v-if="overview || pendingOrders != null" class="m-todo">
      <RouterLink v-if="mobile.allowed('incoming') && count('pending_incoming') != null" to="/mobile/receive"
        >待收料 <b>{{ quantity(count("pending_incoming")) }}</b></RouterLink
      ><span v-if="mobile.allowed('incoming') && count('pending_incoming') != null && ordersAllowed">·</span
      ><RouterLink v-if="ordersAllowed" to="/mobile/query?unfulfilled=1"
        >未送齐订单 <b>{{ quantity(pendingOrders) }}</b></RouterLink
      >
    </div>
    <section v-if="summaryAllowed" class="m-card m-summary">
      <header>
        <b>经营摘要</b><span>{{ overview?.month }}</span>
      </header>
      <div class="m-summary-grid">
        <div>
          <b>{{ money(margin?.summary.sales_amount) }}</b
          ><span>销售额</span>
        </div>
        <div>
          <b>{{ money(margin?.summary.material_cost) }}</b
          ><span>材料成本</span>
        </div>
        <div>
          <b>{{ money(margin?.summary.material_margin) }}</b
          ><span>材料毛利</span>
        </div>
        <div>
          <b>{{
            margin?.summary.material_margin_rate == null
              ? "—"
              : `${(Number(margin.summary.material_margin_rate) * 100).toFixed(1)}%`
          }}</b
          ><span>材料毛利率</span>
        </div>
      </div>
    </section>
    <div v-if="error" class="m-status" role="alert">
      {{ error }}<button @click="load">重试</button>
    </div>
    <div v-if="!mobile.shell && !mobile.error" class="m-empty" role="status">
      加载中…
    </div>
  </section>
</template>
