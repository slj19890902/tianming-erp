from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")
ONBOARDING_SECTION = WAREHOUSE.split(
    '<section id="inventoryOnboardingSection"', 1
)[1].split("</section>", 1)[0]
ONBOARDING_SCRIPT = WAREHOUSE.split(
    'const INVENTORY_ONBOARDING_API="/api/warehouse/inventory-onboarding";', 1
)[1].split("function stocktakePick(", 1)[0]
AUTO_CHECK_FUNCTION = ONBOARDING_SCRIPT.split(
    "async function runInventoryOnboardingAutoCheck(",
    1,
)[1].split("async function rematchInventoryOnboardingBatch()", 1)[0]
CONFIRM_FUNCTION = ONBOARDING_SCRIPT.split(
    "async function confirmInventoryOnboardingBatch(){",
    1,
)[1].split("function stocktakePick(", 1)[0]


def test_inventory_onboarding_entry_is_strictly_stocktake_view_gated() -> None:
    assert (
        'id="inventoryOnboardingTab" '
        'class="btn inventory-onboarding-access hidden"'
    ) in WAREHOUSE
    assert (
        'const canViewInventoryOnboarding=()=>'
        'hasPermission("warehouse.stocktake.view")'
    ) in WAREHOUSE
    assert (
        'const canEditInventoryOnboarding=()=>'
        '!state.readOnly&&hasPermission("warehouse.stocktake.submit")'
    ) in WAREHOUSE
    assert 'class="onboarding-upload-box onboarding-submit-only"' in WAREHOUSE
    assert "revealInventoryOnboardingTab();" in WAREHOUSE
    assert (
        'if(tab==="inventory_onboarding"&&!canViewInventoryOnboarding())'
    ) in WAREHOUSE
    assert (
        'else if(tab==="inventory_onboarding")'
        "loadInventoryOnboardingBatches()"
    ) in WAREHOUSE


def test_inventory_onboarding_page_is_first_count_autopilot_without_technical_steps() -> None:
    for text in (
        "首次盘点入库",
        "仅在首次盘点或发现账外库存时使用",
        "日常订单、生产、移动和送货会自动更新库存",
        "导出当前成品库存盘点表",
        "不知道就写“？”",
        "位置可空",
        "上传并解析",
        "重新检查",
        "下载问题",
        "确认盘点入库",
        "技术记录",
        "正常行无需操作",
    ):
        assert text in WAREHOUSE
    for removed_control in (
        'id="refreshOnboardingBatches"',
        'id="downloadOnboardingCsv"',
        'id="dryRunOnboardingBatch"',
        'id="submitOnboardingBatch"',
        'id="postOnboardingBatch"',
        "执行 dry-run",
        "提交批次",
        "小批正式入账",
    ):
        assert removed_control not in ONBOARDING_SECTION
    assert "<table" not in ONBOARDING_SECTION
    assert ".onboarding-section-panel{overflow:visible}" in WAREHOUSE
    assert ".onboarding-line-list{display:grid" in WAREHOUSE
    assert (
        'return match==="blocked"||match==="pending"||errors.length>0'
        in WAREHOUSE
    )


def test_inventory_onboarding_api_contract_is_fully_wired() -> None:
    for path in (
        "/field-sheet",
        "/batches/import",
        "/batches`",
        "/batches/${encodeURIComponent(batchId)}`",
        "/batches/${encodeURIComponent(batchId)}/lines",
        "/lines/${encodeURIComponent(line.id)}",
        "/dry-run",
        "/errors.csv",
        "/submit",
        "/post",
    ):
        assert path in ONBOARDING_SCRIPT
    assert 'const formData=new FormData();formData.append("file",file)' in ONBOARDING_SCRIPT
    assert 'method:"PATCH"' in ONBOARDING_SCRIPT
    assert "expected_version:Number(line.version)" in ONBOARDING_SCRIPT
    assert "batch_expected_version:Number(batch.version)" in ONBOARDING_SCRIPT
    assert "expected_version:Number(batch.version)" in ONBOARDING_SCRIPT


def test_line_editor_exposes_only_draft_correction_fields() -> None:
    for field_id in (
        "onboardingEditInventoryType",
        "onboardingEditOwnershipType",
        "onboardingEditStocktakeDate",
        "onboardingEditStocktakerName",
        "onboardingEditCustomerCode",
        "onboardingEditCustomerName",
        "onboardingEditInventoryCode",
        "onboardingEditProductName",
        "onboardingEditQuantity",
        "onboardingEditUnit",
        "onboardingEditLocationCode",
        "onboardingEditPalletCode",
        "onboardingEditStockDate",
        "onboardingEditStockDateAccuracy",
        "onboardingEditMaterialCode",
        "onboardingEditSupplierName",
        "onboardingEditLayerCount",
        "onboardingEditFluteType",
        "onboardingEditBoardLength",
        "onboardingEditBoardWidth",
        "onboardingEditSheetType",
        "onboardingEditComponentType",
        "onboardingEditPiecesPerBox",
        "onboardingEditStockYield",
        "onboardingEditCreaseType",
        "onboardingEditCreaseLeft",
        "onboardingEditCreaseMiddle",
        "onboardingEditCreaseRight",
        "onboardingEditCuttingNote",
        "onboardingEditActionDecision",
        "onboardingEditRemarks",
    ):
        assert f'id="{field_id}"' in ONBOARDING_SECTION
    for value in ("pending", "create_new", "exclude"):
        assert f'value="{value}"' in ONBOARDING_SECTION
    for service_routed_value in (
        "route_n035",
        "route_semi_adjust",
        "route_snapshot_conversion",
    ):
        assert f'value="{service_routed_value}"' not in ONBOARDING_SECTION
        assert service_routed_value in ONBOARDING_SCRIPT
    assert "line.original_values||line.original_values_json" in ONBOARDING_SCRIPT
    assert "line.error_codes||line.error_codes_json" in ONBOARDING_SCRIPT
    assert "<summary>技术记录</summary>" in ONBOARDING_SCRIPT
    assert "if(!needsAttention)return" in ONBOARDING_SCRIPT
    assert "第 ${h(line.source_row_number" in ONBOARDING_SCRIPT


def test_line_editor_shows_semi_finished_fields_only_for_semi_finished() -> None:
    assert (
        'id="onboardingSemiFields" class="onboarding-semi-fields hidden"'
        in ONBOARDING_SECTION
    )
    assert ".onboarding-semi-fields{display:contents}" in WAREHOUSE
    assert (
        'const semi=$("onboardingEditInventoryType").value==="semi_finished"'
        in ONBOARDING_SCRIPT
    )
    assert (
        '$("onboardingSemiFields").classList.toggle("hidden",!semi)'
        in ONBOARDING_SCRIPT
    )
    assert (
        '$("onboardingEditInventoryType").onchange='
        "updateInventoryOnboardingLineEditorVisibility"
        in WAREHOUSE
    )
    assert 'if(inventoryType==="semi_finished")' in ONBOARDING_SCRIPT
    assert ".onboarding-line-editor{border:2px solid #93c5fd;" in WAREHOUSE
    assert "background:#eff6ff;overflow:visible}" in WAREHOUSE
    assert (
        ".onboarding-editor-grid{display:grid;"
        "grid-template-columns:repeat(4,minmax(0,1fr));"
        "gap:8px;overflow:visible}"
    ) in WAREHOUSE


def test_line_editor_payload_matches_all_supported_correction_fields() -> None:
    for payload_field in (
        "stocktake_date:",
        "stocktaker_name:",
        "customer_code:",
        "material_code:",
        "supplier_name:",
        "layer_count:",
        "flute_type:",
        "board_length_mm:",
        "board_width_mm:",
        "pieces_per_box:",
        "stock_yield_per_sheet:",
        "crease_type:",
        "crease_left_mm:",
        "crease_middle_mm:",
        "crease_right_mm:",
        "cutting_note:",
        "semiPayload.sheet_type=",
        "semiPayload.component_type=",
    ):
        assert payload_field in ONBOARDING_SCRIPT
    assert 'line.stocktake_date||""' in ONBOARDING_SCRIPT
    assert 'line.stocktaker_name||""' in ONBOARDING_SCRIPT
    assert 'line,["customer_code","customer_code_snapshot"]' in ONBOARDING_SCRIPT
    assert "line.board_length_mm??" in ONBOARDING_SCRIPT
    assert "line.stock_yield_per_sheet??" in ONBOARDING_SCRIPT
    assert "line.crease_middle_mm??" in ONBOARDING_SCRIPT


def test_excluding_line_keeps_optional_note_and_needs_no_reason() -> None:
    assert 'if(actionDecision==="exclude"&&!remarks)' not in ONBOARDING_SCRIPT
    assert "不计入本次盘点时必须填写原因" not in ONBOARDING_SCRIPT
    assert '$("onboardingEditRemarks").required=false' in ONBOARDING_SCRIPT
    assert '$("onboardingEditRemarks").placeholder="可选备注"' in ONBOARDING_SCRIPT
    assert (
        '$("onboardingEditActionDecision").onchange='
        "updateInventoryOnboardingLineEditorVisibility"
        in WAREHOUSE
    )


def test_upload_and_line_save_automatically_run_one_combined_backend_check() -> None:
    assert (
        'await runInventoryOnboardingAutoCheck('
        '"盘点表已上传并自动检查；正常行无需操作")'
    ) in ONBOARDING_SCRIPT
    assert (
        'await runInventoryOnboardingAutoCheck('
        '"已保存并自动重新检查；只需继续处理标红问题")'
    ) in ONBOARDING_SCRIPT
    assert "/dry-run" in AUTO_CHECK_FUNCTION
    assert "/rematch" not in AUTO_CHECK_FUNCTION
    assert AUTO_CHECK_FUNCTION.count('method:"POST"') == 1
    assert "expected_version:Number(batch.version)" in AUTO_CHECK_FUNCTION


def test_one_confirm_button_submits_then_posts_without_extra_input() -> None:
    assert "onboardingFreezeConfirmed" not in ONBOARDING_SECTION
    assert ONBOARDING_SECTION.count('id="confirmOnboardingBatch"') == 1
    assert 'id="submitOnboardingBatch"' not in ONBOARDING_SECTION
    assert 'id="postOnboardingBatch"' not in ONBOARDING_SECTION
    assert (
        "dry_run_fingerprint:batch.dry_run_fingerprint,"
        "idempotency_key:inventoryOnboardingSubmitKey(batch),confirmed:true"
    ) in CONFIRM_FUNCTION
    assert "if(!frozen&&!batch.dry_run_fingerprint)" in CONFIRM_FUNCTION
    assert "inventoryOnboardingIssueCount(batch)>0" in CONFIRM_FUNCTION
    assert "/apply" not in CONFIRM_FUNCTION
    assert CONFIRM_FUNCTION.index("/submit") < CONFIRM_FUNCTION.index("/post")
    assert 'const posted=await api(' in CONFIRM_FUNCTION
    assert '{method:"POST"}' in CONFIRM_FUNCTION
    for forbidden in ("confirm(", "prompt(", "confirmation_phrase", "reason:"):
        assert forbidden not in CONFIRM_FUNCTION


def test_warehouse_inline_javascript_remains_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    scripts = re.findall(
        r"<script(?:\s[^>]*)?>(.*?)</script>",
        WAREHOUSE,
        flags=re.DOTALL | re.IGNORECASE,
    )
    inline = "\n".join(script for script in scripts if script.strip())
    output = tmp_path / "warehouse-inline.js"
    output.write_text(inline, encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
