<script setup lang="ts">
import type { Product } from "../api";
import { compactText, quantity, shortCode, shortLocation } from "../format";
defineProps<{ product: Product }>();
</script>
<template>
  <RouterLink :to="`/mobile/products/${product.id}`" class="m-product-row">
    <strong class="m-code">{{ shortCode(product.product_code) }}</strong>
    <span class="m-product-mid"
      ><b>{{ compactText(product.customer_name) }}</b
      ><span>{{ compactText(product.product_name) }}</span
      ><span>{{ compactText(product.specification) }}</span></span
    >
    <span class="m-product-stock"
      ><b v-if="product.dimension_match?.score_percent != null" class="m-match">{{ product.dimension_match.score_percent }}%</b>
      <b v-else>{{ quantity(product.inventory_summary?.finished?.actual) }}</b
      ><span>{{
        shortLocation(
          product.inventory_positions?.find(
            (p) => p.inventory_type === "finished",
          )?.location_label,
        )
      }}</span></span
    >
  </RouterLink>
</template>
