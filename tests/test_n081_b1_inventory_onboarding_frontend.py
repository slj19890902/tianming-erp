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
        'hasPermission("warehouse.stocktake.submit")'
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


def test_inventory_onboarding_page_is_dense_and_has_no_scroll_table_dependency() -> None:
    for text in (
        "库存建账",
        "下载 CSV 模板",
        "下载 XLSX 模板",
        "上传并解析",
        "重新匹配",
        "执行 dry-run",
        "下载错误 CSV",
        "修正本行",
        "提交批次",
        "正式入账",
    ):
        assert text in WAREHOUSE
    assert "已提交批次可由有权限人员一键正式入账" in ONBOARDING_SECTION
    assert "<table" not in ONBOARDING_SECTION
    assert ".onboarding-section-panel{overflow:visible}" in WAREHOUSE
    assert ".onboarding-line-list{display:grid" in WAREHOUSE


def test_inventory_onboarding_api_contract_is_fully_wired() -> None:
    for path in (
        "/template?format=${encodeURIComponent(format)}",
        "/batches/import",
        "/batches`",
        "/batches/${encodeURIComponent(batchId)}`",
        "/batches/${encodeURIComponent(batchId)}/lines",
        "/lines/${encodeURIComponent(line.id)}",
        "/rematch",
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
    assert "原始上传值与草稿解释并列保留" in ONBOARDING_SCRIPT
    assert "line.original_values||line.original_values_json" in ONBOARDING_SCRIPT
    assert "line.error_codes||line.error_codes_json" in ONBOARDING_SCRIPT


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


def test_excluding_line_requires_a_reason_before_patch() -> None:
    assert (
        'const inventoryType=$("onboardingEditInventoryType").value||null,'
        'actionDecision=$("onboardingEditActionDecision").value,'
        'remarks=inventoryOnboardingNullable("onboardingEditRemarks");'
        'if(actionDecision==="exclude"&&!remarks)'
    ) in ONBOARDING_SCRIPT
    assert "排除本行时必须填写修正备注" in ONBOARDING_SCRIPT
    assert '$("onboardingEditRemarks").required=excluded' in ONBOARDING_SCRIPT
    assert (
        '$("onboardingEditActionDecision").onchange='
        "updateInventoryOnboardingLineEditorVisibility"
        in WAREHOUSE
    )


def test_submit_freezes_b1_before_separate_one_click_posting() -> None:
    assert "onboardingFreezeConfirmed" not in ONBOARDING_SECTION
    assert (
        "dry_run_fingerprint:batch.dry_run_fingerprint,"
        "idempotency_key:inventoryOnboardingSubmitKey(batch),confirmed:true"
    ) in ONBOARDING_SCRIPT
    assert "if(!batch.dry_run_fingerprint)" in ONBOARDING_SCRIPT
    assert "批次已提交，可执行正式入账" in ONBOARDING_SCRIPT
    assert 'batch?.status==="submitted"' in ONBOARDING_SCRIPT
    assert "/apply" not in ONBOARDING_SCRIPT
    assert 'id="postOnboardingBatch"' in ONBOARDING_SECTION
    assert "/post" in ONBOARDING_SCRIPT


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
