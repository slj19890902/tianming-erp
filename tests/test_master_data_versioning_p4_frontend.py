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


def test_pdf_default_price_bulk_sync_reports_actual_outcomes() -> None:
    save_imports = _method_block("async saveConfirmedImportDrafts()", "openOrderEditor(group)")

    assert "const syncSummary = { succeeded:[], cancelled:[], conflicted:[], failed:[] }" in save_imports
    assert "if (synced) syncSummary.succeeded.push" in save_imports
    assert "else syncSummary.cancelled.push" in save_imports
    assert 'detail?.code === "MASTER_VERSION_CONFLICT"' in save_imports
    assert "syncSummary.conflicted.push" in save_imports
    assert "syncSummary.failed.push" in save_imports
    assert "syncSummary.succeeded.length" in save_imports
    assert "syncSummary.cancelled.length" in save_imports
    assert "syncSummary.conflicted.length" in save_imports
    assert "syncSummary.failed.length" in save_imports
    assert "this.showToast(summary, syncSummary.conflicted.length > 0 || syncSummary.failed.length > 0)" in save_imports
    assert "this.showToast(summary" in save_imports
    assert re.search(
        r"this\.showToast\(`[^`]*0[^`]*\$\{uniqueConflicts\.length\}[^`]*`\);",
        save_imports,
    )


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
