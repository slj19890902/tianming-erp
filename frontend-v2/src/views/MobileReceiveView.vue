<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { request } from "../api/client";
import {
  mobileApi,
  backendUrl,
  type Incoming,
  type IncomingDetail,
} from "../mobile/api";
import {
  compactText,
  dimensions,
  quantity,
  safeMessage,
  shortCode,
} from "../mobile/format";
import { useMobileStore } from "../mobile/store";
import MobileIcon from "../mobile/components/MobileIcon.vue";
import MobileSheet from "../mobile/components/MobileSheet.vue";
const mobile = useMobileStore(),
  tab = ref("pending"),
  pending = ref<Incoming[]>([]),
  received = ref<Incoming[]>([]),
  loading = ref(false),
  error = ref(""),
  notice = ref("");
const expanded = ref<string | null>(null),
  details = ref<IncomingDetail | null>(null),
  detailError = ref(""),
  detailLoading = ref(false),
  busy = ref<string | null>(null),
  undo = ref<Incoming | null>(null),
  reason = ref(""),
  imageFailed = ref(false);
let listController: AbortController | null = null,
  detailController: AbortController | null = null,
  alive = true;
const items = computed(() =>
  tab.value === "pending" ? pending.value : received.value,
);
const canRevert = computed(() => mobile.shell?.user.role === "admin");
const canExecute = computed(
  () => mobile.entry("incoming")?.can_execute === true,
);
const drawing = computed(
  () =>
    details.value?.production_tasks.find((t) => t.drawing_path)?.drawing_path ||
    details.value?.incoming_item.drawing_path ||
    "",
);
const drawingUrl = computed(() =>
  drawing.value ? backendUrl(drawing.value) : "",
);
const summary = (item: Incoming) =>
  item.requisition_spec ||
  dimensions(
    item.report_length_mm ?? item.cardboard_len,
    item.report_width_mm ?? item.cardboard_width,
  );
const rowKey = (item: Incoming) => item.history_key || String(item.receipt_item_id || item.item_id);
const count = (item: Incoming) => quantity(tab.value === "received"
  ? item.received_quantity_this_time ?? item.incoming_quantity
  : item.remaining_quantity ?? item.incoming_quantity ?? item.requisition_qty);
function requestKey(action: string, item: Incoming) {
  const name = `mobile-receipt:${mobile.shell?.user.id}:${action}:${rowKey(item)}`;
  let key = sessionStorage.getItem(name);
  if (!key) { key = `${action}-${Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2, "0")).join("")}`; sessionStorage.setItem(name, key); }
  return { name, key };
}
async function load() {
  listController?.abort();
  listController = new AbortController();
  const current = listController;
  loading.value = true;
  error.value = "";
  try {
    const [p, r] = await Promise.all([
      mobileApi.pending(current.signal),
      mobileApi.received(current.signal),
    ]);
    if (!current.signal.aborted && alive) {
      pending.value = p.items;
      received.value = r.items;
    }
  } catch (e) {
    if (!current.signal.aborted && alive) error.value = safeMessage(e);
  } finally {
    if (alive && current === listController) loading.value = false;
  }
}
async function expand(item: Incoming) {
  detailController?.abort();
  details.value = null;
  detailError.value = "";
  imageFailed.value = false;
  if (expanded.value === rowKey(item)) {
    expanded.value = null;
    return;
  }
  expanded.value = rowKey(item);
  if (tab.value === "received") return;
  detailController = new AbortController();
  const current = detailController;
  detailLoading.value = true;
  try {
    const result = await mobileApi.incomingDetail(
      String(item.item_id),
      current.signal,
    );
    if (!current.signal.aborted && alive) details.value = result;
  } catch (e) {
    if (!current.signal.aborted && alive) detailError.value = safeMessage(e);
  } finally {
    if (current === detailController && alive) detailLoading.value = false;
  }
}
async function confirm(item: Incoming) {
  if (busy.value || !canExecute.value) return;
  busy.value = rowKey(item);
  error.value = "";
  notice.value = "";
  try {
    const identity = requestKey("receive", item);
    await request(`/api/incoming/receive/${encodeURIComponent(item.item_id)}`, {
      method: "PUT", body: JSON.stringify({ idempotency_key: identity.key }),
    });
    sessionStorage.removeItem(identity.name);
    if (!alive) return;
    notice.value = "已确认收料";
    expanded.value = null;
    await load();
  } catch (e) {
    if (alive) error.value = safeMessage(e);
  } finally {
    if (alive) busy.value = null;
  }
}
async function revert() {
  if (!undo.value || !reason.value.trim() || busy.value || !canRevert.value)
    return;
  const item = undo.value;
  busy.value = rowKey(item);
  error.value = "";
  try {
    const identity = requestKey("revert", item);
    const target = item.receipt_item_id ? `receipt-items/${item.receipt_item_id}/revert` : `revert/${encodeURIComponent(item.item_id)}`;
    await request(`/api/incoming/${target}`, {
      method: "PUT",
      body: JSON.stringify({ reason: reason.value.trim(), idempotency_key: identity.key }),
    });
    if (!alive) return;
    sessionStorage.removeItem(identity.name);
    undo.value = null;
    notice.value = "已撤销收料";
    await load();
  } catch (e) {
    if (alive) error.value = safeMessage(e);
  } finally {
    if (alive) busy.value = null;
  }
}
onMounted(load);
onBeforeUnmount(() => {
  alive = false;
  listController?.abort();
  detailController?.abort();
});
</script>
<template>
  <section>
    <div class="m-receive-toolbar">
      <div class="m-seg">
        <button
          :class="{ on: tab === 'pending' }"
          @click="
            tab = 'pending';
            expanded = null;
          "
        >
          待收料</button
        ><button
          :class="{ on: tab === 'received' }"
          @click="
            tab = 'received';
            expanded = null;
          "
        >
          已收料
        </button>
      </div>
      <button
        class="m-icon-btn"
        aria-label="刷新收料"
        :disabled="loading || !!busy"
        @click="load"
      >
        <MobileIcon name="refresh" />
      </button>
    </div>
    <a v-if="canExecute" :href="backendUrl('/mobile/erp.html#incoming')" class="m-text-action">部分到货 / 选择入库位置</a>
    <div v-if="notice" class="m-notice" role="status">{{ notice }}</div>
    <div v-if="error" class="m-status" role="alert">
      {{ error }}<button :disabled="!!busy" @click="load">重试</button>
    </div>
    <div v-if="loading" class="m-empty" role="status">加载中…</div>
    <div v-else-if="!items.length && !error" class="m-empty">
      {{ tab === "pending" ? "暂无待收料" : "暂无收料记录" }}
    </div>
    <article
      v-for="item in items"
      :key="rowKey(item)"
      class="m-card m-receive-card"
      :class="{ open: expanded === rowKey(item) }"
    >
      <button
        class="m-receive-head"
        :aria-expanded="expanded === rowKey(item)"
        @click="expand(item)"
      >
        <span class="m-receive-spec"
          >{{ compactText(summary(item))
          }}<span>{{ compactText(item.flute_type) }}</span></span
        ><span class="m-receive-qty"
          >数量 <b>{{ count(item) }}</b> 张<span class="m-expand"
            >{{ expanded === rowKey(item) ? "收起" : "展开"
            }}<MobileIcon name="chevron" :size="17" /></span
        ></span>
      </button>
      <section
        v-if="expanded === rowKey(item)"
        class="m-receive-details"
      >
        <div class="m-receive-meta">
          供应商
          <b>{{
            compactText(
              details?.incoming_item.snapshot_supplier_name || details?.incoming_item.supplier_name || (item.snapshot_supplier_name || item.supplier_name),
            ) || "—"
          }}</b>
          ·
          {{
            compactText(
              item.supplier_delivery_time || item.material_received_at,
            ) || "—"
          }}
        </div>
        <h2>对应产品</h2>
        <div class="m-receive-product">
          <a
            v-if="drawingUrl"
            :href="drawingUrl"
            target="_blank"
            rel="noopener"
            class="m-thumb"
            :aria-label="`查看图纸 图-${shortCode(details?.incoming_item.product_code || item.product_code)}`"
            ><img
              v-if="!imageFailed"
              :src="drawingUrl"
              alt="图纸缩略图"
              @error="imageFailed = true"
            /><MobileIcon v-else name="doc" :size="32" /><span
              >图-{{
                shortCode(
                  details?.incoming_item.product_code || item.product_code,
                )
              }}</span
            ></a
          >
          <dl>
            <div>
              <dt>短码</dt>
              <dd>
                <strong>{{
                  shortCode(
                    details?.incoming_item.product_code || item.product_code,
                  )
                }}</strong>
                ·
                {{
                  compactText(
                    details?.incoming_item.customer_name || item.customer_name,
                  )
                }}
              </dd>
            </div>
            <div>
              <dt>品名</dt>
              <dd>
                {{
                  compactText(
                    details?.incoming_item.product_name || item.product_name,
                  ) || "—"
                }}
              </dd>
            </div>
            <div>
              <dt>工艺</dt>
              <dd>
                {{
                  details?.production_tasks
                    .flatMap((t) => t.process_tags || [])
                    .map(compactText)
                    .join(" → ") ||
                  compactText(item.production_process) ||
                  "—"
                }}
              </dd>
            </div>
          </dl>
        </div>
        <div v-if="detailLoading" role="status">加载中…</div>
        <div v-if="detailError" class="m-status" role="alert">
          {{ detailError }}
        </div>
        <button
          v-if="tab === 'pending' && canExecute"
          class="m-primary m-wide"
          :disabled="!!busy"
          @click="confirm(item)"
        >
          {{ busy === rowKey(item) ? "收料中…" : "确认收料" }}</button
        ><button
          v-if="tab === 'received' && canRevert && item.can_revert_receipt !== false"
          class="m-outline m-wide"
          :disabled="!!busy"
          @click="
            undo = item;
            reason = '';
          "
        >
          撤销收料
        </button>
      </section>
    </article>
    <MobileSheet v-if="undo" title="撤销收料" @close="!busy && (undo = null)"
      ><form @submit.prevent="revert">
        <label class="m-form-field"
          >撤销原因<input
            v-model="reason"
            required
            maxlength="500"
            :disabled="!!busy" /></label
        ><button class="m-primary m-wide" :disabled="!!busy || !reason.trim()">
          {{ busy ? "处理中…" : "确认撤销" }}
        </button>
      </form></MobileSheet
    >
  </section>
</template>
