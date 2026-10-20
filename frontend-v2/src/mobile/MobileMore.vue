<script setup lang="ts">
import { useMobileStore } from "./store";
import { useAuthStore } from "../stores/auth";
import { legacyUrl } from "./api";
import MobileIcon from "./components/MobileIcon.vue";
import { useRouter } from "vue-router";
const mobile = useMobileStore(),
  auth = useAuthStore(),
  router = useRouter();
async function logout() {
  await auth.logout();
  mobile.clear();
  await router.replace("/login");
}
</script>
<template>
  <section class="m-more">
    <RouterLink v-if="mobile.allowed('incoming')" to="/mobile/receive"
      ><MobileIcon name="receive" />收料<MobileIcon
        name="chevron" /></RouterLink
    ><a v-if="mobile.allowed('production')" :href="legacyUrl('production')"
      ><MobileIcon name="grid" />生产<MobileIcon name="chevron" /></a
    ><a v-if="mobile.allowed('pre_delivery')" :href="legacyUrl('predelivery')"
      ><MobileIcon name="truck" />预送货<MobileIcon name="chevron" /></a
    ><RouterLink v-if="mobile.allowed('warehouse')" to="/mobile/warehouse"
      ><MobileIcon name="pin" />仓库地图<MobileIcon
        name="chevron" /></RouterLink
    ><button @click="logout">退出登录</button>
  </section>
</template>
