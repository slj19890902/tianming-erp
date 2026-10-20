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
let generation = 0;
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
      id: "predelivery",
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
  const token = ++generation;
  error.value = "";
  overview.value = null;
  margin.value = null;
  pendingOrders.value = undefined;
  if (!mobile.shell) return;
  try {
    if (mobile.shell.management_summary_allowed) {
      const response = await mobileApi.overview();
      if (token === generation) overview.value = response;
    }
    if (ordersAllowed.value) {
      const result = await mobileApi.unfulfilled();
      if (token === generation) pendingOrders.value = result.groups[0]?.total;
    }
  } catch (e) {
    if (token === generation) error.value = safeMessage(e);
  }
  // Follow the server's explicit cost entitlement. No financial request for employees.
  if (summaryAllowed.value && mobile.shell?.delivery_margin_allowed) {
    try {
      const result = await mobileApi.margin();
      if (token === generation) margin.value = result;
    } catch (e) {
      if (token === generation) error.value = safeMessage(e);
    }
  }
}
watch(() => mobile.shell, load);
onMounted(load);
onBeforeUnmount(() => {
  generation++;
});
</script>
<template>
  <section class="m-home">
    <div class="m-grid2">
      <template v-for="button in buttons" :key="button.id">
        <a
          v-if="button.id === 'predelivery'"
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
      <RouterLink v-if="mobile.allowed('incoming')" to="/mobile/receive"
        >待收料 <b>{{ quantity(count("pending_incoming")) }}</b></RouterLink
      ><span v-if="mobile.allowed('incoming') && ordersAllowed">·</span
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
