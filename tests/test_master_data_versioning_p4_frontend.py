from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_block(start: str, end: str) -> str:
    begin = INDEX.index(start)
    return INDEX[begin : INDEX.index(end, begin)]


def test_existing_master_save_builds_readable_diff_and_versioned_payload() -> None:
    save = _method_block("async saveModal()", "async dispatchDelivery(row)")

    assert "masterLocalChanges(masterEntity)" in save
    assert "openMasterChangeConfirmation(masterEntity,changes)" in save
    assert "masterForm?.id && !masterOptions" in save
    assert 'payload.expected_version = expectedVersion' in INDEX
    assert 'payload.change_reason = options.change_reason' in INDEX
    assert 'payload.confirmation_token = options.confirmation_token' in INDEX
    for entity in ("customer", "product", "material"):
        assert f'attachMasterUpdateMetadata("{entity}",payload,masterOptions)' in save


def test_confirmation_required_keeps_shared_modal_reason_and_requires_ack() -> None:
    handler = _method_block("handleMaster409(error, entity", "closeMasterChangeConfirm()")

    assert 'detail.code === "MASTER_CHANGE_CONFIRMATION_REQUIRED"' in handler
    assert "current.reason" in handler or "...current" in handler
    assert "confirmation_token" in handler
    assert "changed_fields" in handler
    assert "warnings" in handler
    assert "requireAcknowledgement:true" in handler
    assert "confirm(" not in handler
    assert "确认主数据变更" in INDEX
    assert "我已核对异常修改" in INDEX
    assert 'v-model.trim="masterChangeConfirm.reason"' in INDEX


def test_version_conflict_preserves_editor_and_has_safe_resolution_actions() -> None:
    handler = _method_block("handleMaster409(error, entity", "closeMasterChangeConfirm()")

    assert 'detail.code === "MASTER_VERSION_CONFLICT"' in handler
    assert "this.masterVersionConflict = conflict" in handler
    assert "他人已修改，打开版本" in INDEX
    assert "加载服务器最新版" in INDEX
    assert "查看版本历史" in INDEX
    assert ">强制覆盖<" not in INDEX
    assert "/versions/${encodeURIComponent(conflict.currentVersion)}" in INDEX


def test_versioned_mutations_abort_when_confirmation_response_has_no_token() -> None:
    send_mutation = _method_block(
        "async sendVersionedMasterMutation({method,url,row,body={},reasonLabel,defaultReason=\"\"})",
        "async syncProductFieldsVersioned(",
    )
    sync_product = _method_block(
        "async syncProductFieldsVersioned(",
        "async deleteCustomer(row)",
    )

    for block in (send_mutation, sync_product):
        assert "confirmationToken = detail.confirmation_token || null" in block
        assert "if (!confirmationToken) throw new Error(" in block
        assert "continue;" in block


def test_version_conflict_errors_include_expected_and_current_versions() -> None:
    send_mutation = _method_block(
        "async sendVersionedMasterMutation({method,url,row,body={},reasonLabel,defaultReason=\"\"})",
        "async syncProductFieldsVersioned(",
    )
    sync_product = _method_block(
        "async syncProductFieldsVersioned(",
        "async deleteCustomer(row)",
    )

    assert 'detail?.code === "MASTER_VERSION_CONFLICT"' in send_mutation
    assert "detail.expected_version ?? expectedVersion" in send_mutation
    assert "detail.current_version ??" in send_mutation
    assert "throw new Error(`" in send_mutation

    assert 'detail?.code === "MASTER_VERSION_CONFLICT"' in sync_product
    assert "detail.expected_version ?? requestedVersion" in sync_product
    assert "detail.current_version ??" in sync_product
    assert "throw new Error(`" in sync_product


def test_pdf_order_save_does_not_start_post_save_default_price_sync() -> None:
    save_imports = _method_block("async saveConfirmedImportDrafts()", "openOrderEditor(group)")

    assert "this.refreshPdfPriceConflict(item)" in save_imports
    assert "priceConflictItems" not in save_imports
    assert "uniqueConflicts" not in save_imports
    assert "PDF 默认价批量同步" not in save_imports
    assert "syncProductFieldsVersioned" not in save_imports


def test_new_order_save_does_not_offer_post_save_common_box_overwrite() -> None:
    save = _method_block("async saveModal()", "async dispatchDelivery(row)")

    assert "async openOrder() {" in INDEX
    assert "if (!this.customerOptions.length) await this.loadCustomerOptions();" in INDEX
    assert "offerSyncCommonBox" not in INDEX
    assert "await axios.post(\"/api/orders\", orderPayload)" in save
    assert "await Promise.all([this.loadOrders(), this.loadKpi()])" in save
    assert "是否同步更新到常用箱" not in INDEX


def test_pdf_explicit_common_box_edit_short_circuits_equal_values() -> None:
    edit = _method_block("async saveImportItemEdit(", "toggleAllDelivery(checked)")

    assert '@click="startEditImportItem(draft,item)">修改本行</button>' in INDEX
    assert "productSyncFieldChanges(prod, fields)" in edit
    assert "if (Object.keys(changed).length)" in edit
    assert '"PDF 草稿显式覆盖常用箱"' in edit
    assert "prod.version" in edit
    assert "false," in edit
    assert "确认保存并覆盖常用箱" not in edit
    assert "内容与常用箱一致，无需更新主数据" in edit


def test_p1_12_price_recheck_and_explicit_sync_execute_without_extra_prompt() -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the P1-12 frontend behavior test"

    harness = r"""
const fs = require("fs");
const vm = require("vm");
const html = fs.readFileSync(process.argv[2], "utf8");
const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)]
  .map(match => match[1])
  .filter(source => source.trim());
if (scripts.length !== 1) throw new Error(`Expected one inline script, found ${scripts.length}`);

const sandbox = {
  axios: {
    defaults: {},
    interceptors: { response: { use() {} } },
  },
  Vue: {
    createApp(definition) {
      sandbox.definition = definition;
      return { component() { return this; }, mount() { return this; } };
    },
  },
  localStorage: { getItem() { return ""; }, setItem() {}, removeItem() {} },
  window: {},
  console,
  URLSearchParams,
  setTimeout,
  clearTimeout,
};
vm.createContext(sandbox);
vm.runInContext(scripts[0], sandbox);
const methods = sandbox.definition.methods;
const assert = (condition, message) => {
  if (!condition) throw new Error(message);
};

const priceItem = {
  unit_price: "1.00",
  product_default_price: "1",
  matched_product_id: 7,
  price_conflict: { stale: true },
};
methods.refreshPdfPriceConflict.call(methods, priceItem);
assert(priceItem.price_conflict === null, "1 and 1.00 were treated as different");
priceItem.unit_price = "1.50";
methods.refreshPdfPriceConflict.call(methods, priceItem);
assert(priceItem.price_conflict?.pdf_price === "1.50", "Real price difference was not shown");
priceItem.unit_price = "1.0000";
methods.refreshPdfPriceConflict.call(methods, priceItem);
assert(priceItem.price_conflict === null, "Restoring the original price kept a stale conflict");
priceItem.unit_price = 0;
priceItem.product_default_price = "0.0000";
methods.refreshPdfPriceConflict.call(methods, priceItem);
assert(priceItem.price_conflict === null, "Zero price was treated as missing or different");

const product = {
  id: 7,
  version: 3,
  product_name: "P1-12常用箱",
  material_id: 4,
  layer_count: 3,
  flute_type: "B",
  sale_unit_price: "1",
  production_process: null,
  report_length_mm: null,
  report_width_mm: null,
  crease_type: null,
  crease_left_mm: null,
  crease_middle_mm: null,
  crease_right_mm: null,
};
sandbox.axios.get = async () => ({ data: product });
const syncCalls = [];
const toasts = [];
const context = {
  ...methods,
  loading: false,
  showToast(message, error = false) { toasts.push({ message, error }); },
  isImportDraftLocked() { return false; },
  invalidateImportDraftConfirmation(draft) { draft.confirmed = false; },
  async syncProductFieldsVersioned(...args) {
    syncCalls.push(args);
    return { data: { updated: Object.keys(args[1]) } };
  },
};
const makeEditItem = unitPrice => ({
  matched_product_id: 7,
  is_new_product: false,
  product_name: product.product_name,
  specification: "300×200×100",
  unit_price: unitPrice,
  production_notes: "",
  matched_material_id: 4,
  layer_count: 3,
  flute_type: "B",
  product_default_price: "1",
  _editing: true,
  _backup: {},
  _edit: {
    product_name: `  ${product.product_name}  `,
    specification: "300×200×100",
    unit_price: unitPrice,
    production_notes: "",
    matched_material_id: 4,
    layer_count: 3,
    flute_type: "b",
    report_length_mm: null,
    report_width_mm: null,
    crease_type: null,
    crease_left_mm: null,
    crease_middle_mm: null,
    crease_right_mm: null,
  },
});

(async () => {
  const equalItem = makeEditItem("1.00");
  await methods.saveImportItemEdit.call(context, { confirmed: true }, equalItem, 0);
  assert(syncCalls.length === 0, "Equal values triggered a common-box write");
  assert(equalItem._editing === false, "Equal edit did not close cleanly");

  const changedItem = makeEditItem("1.50");
  await methods.saveImportItemEdit.call(context, { confirmed: true }, changedItem, 0);
  assert(syncCalls.length === 1, "Explicit real change was not synced exactly once");
  assert(syncCalls[0][1].sale_unit_price === "1.50", "Changed price was lost");
  assert(syncCalls[0][3] === 3, "Expected product version was not frozen");
  assert(syncCalls[0][4] === false, "Explicit button still requested a generic reason prompt");
  assert(changedItem.price_conflict === null, "Successful explicit price sync kept a stale conflict");

  let promptCount = 0;
  let posted = null;
  sandbox.window.prompt = () => {
    promptCount += 1;
    throw new Error("Generic reason prompt must not open");
  };
  sandbox.axios.get = async () => ({ data: product });
  sandbox.axios.post = async (_url, payload) => {
    posted = payload;
    return { data: { updated: ["sale_unit_price"], version: 4 } };
  };
  await methods.syncProductFieldsVersioned.call(
    methods,
    7,
    { sale_unit_price: "1.50" },
    "PDF 草稿显式覆盖常用箱",
    3,
    false,
  );
  assert(promptCount === 0, "Explicit sync opened an extra reason prompt");
  assert(posted.change_reason === "PDF 草稿显式覆盖常用箱", "Structured reason was not saved");
  assert(posted.expected_version === 3, "Expected version changed during explicit sync");

  process.stdout.write(JSON.stringify({
    syncCalls: syncCalls.length,
    promptCount,
    lastToast: toasts.at(-1)?.message || "",
  }));
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
"""

    result = subprocess.run(
        [node, "-", str(ROOT / "static" / "index.html")],
        input=harness,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert '"syncCalls":1' in result.stdout
    assert '"promptCount":0' in result.stdout


def test_three_master_lists_have_history_and_errors_are_not_empty_states() -> None:
    for entity in ("customer", "product", "material"):
        assert f"openMasterVersionHistory('{entity}',row)" in INDEX

    assert "/api/master-data/${encodeURIComponent(entity)}/${encodeURIComponent(entityId)}" in INDEX
    assert "/versions`" in INDEX
    assert "版本历史加载失败" in INDEX
    assert "v-if=\"masterVersionHistory.error\"" in INDEX
    assert "v-else-if=\"masterVersionHistory.loading\"" in INDEX
    assert "v-else-if=\"!masterVersionHistory.items.length\"" in INDEX
    assert "Number(b.version || 0) - Number(a.version || 0)" in INDEX
    for label in ("操作人：", "动作：", "原因："):
        assert label in INDEX


def test_restore_preview_and_restore_are_admin_only_and_token_bound() -> None:
    assert 'v-if="canAdmin && Number(item.version) < Number(masterVersionHistory.currentVersion)"' in INDEX
    assert "/restore-preview`" in INDEX
    assert "/restore`" in INDEX
    assert "{expected_version:expectedVersion}" in INDEX
    assert "reason:String(state.reason || \"\").trim()" in INDEX
    assert "confirmation_token:state.confirmationToken" in INDEX
    assert "恢复反向差异" in INDEX
    assert "旧版本已恢复，并已生成新的主数据版本" in INDEX


def test_customer_and_product_readonly_views_disable_real_controls() -> None:
    assert '<fieldset class="form-grid" :class="\'readonly-fieldset\'" :disabled="!!customerForm.id && !canEditCustomers">' in INDEX
    assert '<fieldset class="product-edit-grid" :class="\'readonly-fieldset\'" :disabled="!!productForm.id && !canEditProducts">' in INDEX
    assert "客户资料控件已禁用" in INDEX
    assert "常用箱资料控件已禁用" in INDEX


def test_price_adjust_preview_is_invalidated_and_apply_reuses_preview_contract() -> None:
    assert "修改原因 *" in INDEX
    assert 'v-model.trim="priceAdjustForm.change_reason"' in INDEX
    assert "priceAdjustPreviewKey !== this.priceAdjustFormKey()" in INDEX
    assert "invalidatePriceAdjustPreview()" in INDEX
    assert ':disabled="!priceAdjustPreviewValid || priceAdjustLoading"' in INDEX
    assert "body.expected_versions = expectedVersions" in INDEX
    assert "body.preview_token = previewToken" in INDEX
    assert "body.confirmation_token = previewToken" in INDEX
    assert "body.confirmation_tokens = confirmationTokens" in INDEX


def test_system_batch_apply_requires_preview_tokens_and_human_confirmation() -> None:
    helper = _method_block(
        "async previewAndApplySystemBatch(",
        "async applyHighConfidence()",
    )

    assert "axios.get(previewUrl)" in helper
    assert "preview?.preview_token" in helper
    assert "preview?.confirmation_tokens" in helper
    assert "对象/变更摘要" in helper
    assert "confirm(message)" in helper
    assert "preview_token: previewToken" in helper
    assert "confirmation_tokens: confirmationTokens" in helper
    assert "SYSTEM_BATCH_PREVIEW_STALE" in INDEX
    assert "批量预览已过期或对象/版本已变化，请重新预览" in INDEX

    operations = {
        "applyHighConfidence()": (
            "/api/system/material-mapping/preview-high-confidence",
            "/api/system/material-mapping/apply-high-confidence",
        ),
        "applyCustomerCodes()": (
            "/api/system/material-mapping/preview-customer-codes",
            "/api/system/material-mapping/apply-customer-codes",
        ),
        "applyFluteMapping()": (
            "/api/system/flute-mapping/preview",
            "/api/system/flute-mapping/apply",
        ),
        "fixFluteConsistency()": (
            "/api/system/flute-mapping/preview-consistency",
            "/api/system/flute-mapping/fix-consistency",
        ),
    }
    for method, (preview_url, apply_url) in operations.items():
        block = _method_block(f"async {method}", "},\n          ")
        assert "previewAndApplySystemBatch" in block
        assert preview_url in block
        assert apply_url in block


def test_inline_javascript_is_syntactically_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the frontend contract test"
    scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "index-p4-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr


def test_material_deactivation_keeps_the_versioned_mutation_contract() -> None:
    deactivate = _method_block("async deleteMaterial(row)", "openOrder()")

    assert 'method:"delete"' in deactivate
    assert 'url:`/api/master/materials/${row.id}`' in deactivate
    assert "sendVersionedMasterMutation" in deactivate
    assert 'reasonLabel:`停用材质“${row.code}”`' in deactivate
    assert 'defaultReason:"停用材质"' in deactivate
    assert "await this.loadMaterials()" in deactivate
