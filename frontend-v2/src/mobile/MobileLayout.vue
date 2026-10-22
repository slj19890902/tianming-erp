<script setup lang="ts">
import { computed, onMounted, watch } from "vue";
import { useRoute } from "vue-router";
import { useMobileStore } from "./store";
import { useAuthStore } from "../stores/auth";
import MobileIcon from "./components/MobileIcon.vue";
import { safeMessage } from "./format";
import "./mobile.css";
import { mobileParent, mobileSection } from "./navigation";
const route = useRoute(),
  mobile = useMobileStore(),
  auth = useAuthStore();
const title = computed(() => String(route.meta.title || "天明包装"));
const back = computed(() => mobileParent(route.path, route.query.from));
const activeSection = computed(() => mobileSection(route.path, route.query.from));
const nav = computed(() =>
  [
    { title: "首页", path: "/mobile", icon: "home", allowed: true },
    {
      title: "查询",
      path: "/mobile/query",
      icon: "search",
      allowed: mobile.allowed("lookup"),
    },
    {
      title: "仓库",
      path: "/mobile/warehouse",
      icon: "box",
      allowed: mobile.allowed("warehouse"),
    },
    { title: "更多", path: "/mobile/more", icon: "more", allowed: true },
  ].filter((n) => n.allowed),
);
watch(
  () => auth.user?.id,
  () => {
    mobile.clear();
    if (auth.user) void mobile.load();
  },
);
watch(
  () => route.fullPath,
  () => {
    document.title = `${title.value} · 天明包装`;
  },
);
onMounted(() => {
  void mobile.load();
  document.title = `${title.value} · 天明包装`;
});
</script>
<template>
  <div class="mobile-app">
    <header class="m-appbar">
      <RouterLink
        v-if="route.path !== '/mobile'"
        :to="back"
        class="m-icon-btn"
        :aria-label="back === '/mobile' ? '返回首页' : '返回列表'"
        ><MobileIcon name="back"
      /></RouterLink>
      <h1>{{ title }}</h1>
      <span class="m-spacer" />
      <span v-if="route.path === '/mobile'" class="m-brand">{{
        auth.displayName()
      }}</span>
    </header>
    <div v-if="mobile.error" class="m-status" role="alert">
      {{ safeMessage(new Error(mobile.error))
      }}<button @click="mobile.load">重试</button>
    </div>
    <main class="m-main" :class="{ 'm-main-map': route.path === '/mobile/warehouse' }"><RouterView /></main>
    <nav class="m-tabbar" aria-label="底部导航">
      <RouterLink
        v-for="item in nav"
        :key="item.path"
        :aria-current="activeSection === item.path ? 'page' : undefined"
        :to="item.path"
        :class="{
          active: activeSection === item.path,
        }"
        ><MobileIcon :name="item.icon" /><span>{{
          item.title
        }}</span></RouterLink
      >
    </nav>
  </div>
</template>
