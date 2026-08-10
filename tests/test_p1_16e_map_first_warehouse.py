from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")
WAREHOUSE_TWIN = (
    ROOT / "factory_twin" / "frontend" / "src" / "WarehouseTwinApp.tsx"
).read_text(encoding="utf-8")


def test_warehouse_daily_navigation_defaults_to_measured_twin_floor() -> None:
    tabs = WAREHOUSE[
        WAREHOUSE.index('<div class="tabs">') : WAREHOUSE.index(
            '<div id="pageError"'
        )
    ]
    assert tabs.index('data-tab="locations"') < tabs.index(
        'data-tab="insights"'
    )
    assert '<button class="btn active" data-tab="locations" aria-label="数字孪生库位管理">数字孪生库位</button>' in tabs
    assert 'tab:"locations"' in WAREHOUSE
    assert 'if(!requestedTab&&!locationId&&!lotId&&!keyword){await switchTab("locations");return}' in WAREHOUSE
    assert 'if(requestedTab==="locations"&&!locationId){' in WAREHOUSE
    assert 'await switchTab("locations");' in WAREHOUSE
    assert 'locationView:"floor3"' in WAREHOUSE
    assert 'requestedLocationView==="floor3"' in WAREHOUSE


def test_row_ledgers_are_visible_read_entries_while_mutations_stay_admin_only() -> None:
    assert 'class="btn inventory-ledger-tab" data-tab="finished">高级成品台账' in WAREHOUSE
    assert 'class="btn inventory-ledger-tab" data-tab="semi_finished">高级半成品台账' in WAREHOUSE
    assert 'class="btn" data-location-view="ledger" type="button">全部库位台账' in WAREHOUSE
    assert 'class="btn" type="button" onclick="switchTab(\'finished\')">高级库存台账' in WAREHOUSE
    assert 'class="btn" type="button" onclick="switchLocationView(\'ledger\')">库位结构维护' in WAREHOUSE
    assert '<form id="moldForm" class="panel admin-only">' in WAREHOUSE
    assert '<form id="printingPlateForm" class="panel admin-only">' in WAREHOUSE
    assert 'class="actions admin-only"' in WAREHOUSE
    assert 'state.user.role!=="admin"||state.readOnly' in WAREHOUSE
    assert 'document.querySelectorAll(".admin-only")' in WAREHOUSE


def test_mold_plate_and_stocktake_entries_keep_permission_boundaries() -> None:
    assert '<button class="btn" data-tab="molds">模具位置</button>' in WAREHOUSE
    assert '<button class="btn" data-tab="printing_plates">挂板位置</button>' in WAREHOUSE
    assert 'id="stocktakeReviewTab" class="btn stocktake-review-access hidden"' in WAREHOUSE
    assert 'id="inventoryOnboardingTab" class="btn inventory-onboarding-access hidden"' in WAREHOUSE
    assert 'function revealStocktakeReviewTab(){if(canViewStocktakes())' in WAREHOUSE
    assert 'function revealInventoryOnboardingTab(){if(canViewInventoryOnboarding())' in WAREHOUSE
    assert 'document.querySelectorAll(".onboarding-post-only")' in WAREHOUSE
    assert 'body.floor3-mode header,body.floor3-mode #locationViewTabs{display:none}' in WAREHOUSE
    assert 'body.floor3-mode header,body.floor3-mode .wrap>.tabs' not in WAREHOUSE


def test_map_first_change_does_not_remove_authoritative_ledgers_or_actions() -> None:
    for marker in (
        'id="inventorySection"',
        'id="lotTable"',
        'id="movementSection"',
        'id="locationSection"',
        'id="floor3LocationSection"',
        'async function switchLocationView(view)',
        'async function switchTab(tab)',
        '/api/warehouse/lots',
        '/api/warehouse/floor3/locations',
    ):
        assert marker in WAREHOUSE
    assert "inventory_lots" not in WAREHOUSE.lower()


def test_selected_map_location_can_move_whole_pallet_without_drag_mode() -> None:
    opener = WAREHOUSE.split("async function openFloor3Location(locationId){", 1)[1].split(
        "function floor3FormatItem", 1
    )[0]
    detail = WAREHOUSE.split("function renderFloor3Detail(){", 1)[1].split(
        "function closeFloor3Detail", 1
    )[0]
    mover = WAREHOUSE.split("async function moveFloor3Pallet(palletId){", 1)[1].split(
        "async function clearFloor3Pallet", 1
    )[0]
    assert "floor3CanMoveSelectedPallet" in opener
    assert "移动货物（整栈板）" in detail
    assert "确认移动整栈板" in detail
    assert "移动备注" not in detail
    assert "state.floor3.moveMode" not in mover
    assert "floor3CanMoveSelectedPallet" in mover
    assert '"地图详情移动整栈板"' in mover
    assert "/api/warehouse/pallets/${palletId}/move" in WAREHOUSE


def test_measured_twin_location_detail_exposes_the_same_controlled_move() -> None:
    detail = WAREHOUSE.split("function twinOperationalGoodsHtml(row){", 1)[1].split(
        "async function loadTwinOperationalLayout", 1
    )[0]
    mover = WAREHOUSE.split(
        "async function moveTwinOperationalPallet(palletId){", 1
    )[1].split("async function clearFloor3Pallet", 1)[0]
    selector = WAREHOUSE.split(
        "async function selectTwinOperationalLocation(locationId){", 1
    )[1].split("async function focusTwinOperationalLocation", 1)[0]
    assert "移动货物（整栈板）" in detail
    assert "确认移动整栈板" in detail
    assert "floor3QuantityHtml(item)" in detail
    assert "loadFloor3MoveLocations()" in selector
    assert "state.floor3.moveMode" not in mover
    assert '"数字孪生地图移动整栈板"' in mover
    assert "selectTwinOperationalArea(floor3AreaCode(targetRow))" in mover
    assert "await selectTwinOperationalLocation(Number(locationId))" in WAREHOUSE


def test_p1_16e2_merge_all_is_a_single_confirm_without_quantity_or_reason() -> None:
    merger = WAREHOUSE.split(
        "async function mergeFloor3PalletAll(palletId,selectId,twinMode){", 1
    )[1].split("async function clearFloor3Pallet", 1)[0]
    panel = WAREHOUSE.split("function floor3MergePanel(pallet,selectId,twinMode){", 1)[
        1
    ].split("function floor3ExpectedVersion", 1)[0]
    assert "/api/warehouse/pallets/${source.id}/merge-all" in merger
    assert merger.count("confirm(") == 1
    assert "target_pallet_id:target.id" in merger
    assert "expected_target_version:targetVersion" in merger
    assert "confirmed:true" in merger
    assert "idempotency_key:idempotencyKey" in merger
    assert "<input" not in panel.lower()
    assert "reason" not in panel.lower()
    assert "合并全部剩余货物" in panel
    assert "同客户、同库存类型" in panel


def test_p1_16e2_only_lists_compatible_occupied_target_pallets() -> None:
    signature = WAREHOUSE.split("function floor3PalletMergeSignature(pallet){", 1)[
        1
    ].split("function floor3MergeTypeLabel", 1)[0]
    loader = WAREHOUSE.split("async function loadFloor3MergeLocations(pallet){", 1)[
        1
    ].split("function renderFloor3Locations", 1)[0]
    assert "customers.length===1" in signature
    assert "types.length===1" in signature
    assert "signature?.customerId===source.customerId" in signature
    assert "signature?.inventoryType===source.inventoryType" in signature
    assert 'occupancy:"occupied"' in loader
    assert "customer_id:signature.customerId" in loader


def test_p1_16e2_merge_action_is_available_in_both_map_details() -> None:
    standard = WAREHOUSE.split("function renderFloor3Detail(){", 1)[1].split(
        "function closeFloor3Detail", 1
    )[0]
    twin = WAREHOUSE.split("function renderTwinOperationalDetail(){", 1)[1].split(
        "async function loadTwinOperationalLayout", 1
    )[0]
    assert 'floor3MergePanel(pallet,"floor3PalletMergeTarget",false)' in standard
    assert 'floor3MergePanel(pallet,"twinPalletMergeTarget",true)' in twin
    assert "mergePanel" in standard
    assert "mergePanel" in twin


def test_p1_16e2_current_twin_entry_exposes_single_confirm_merge() -> None:
    merger = WAREHOUSE_TWIN.split(
        "const confirmPalletMergeAll = async () => {", 1
    )[1].split("const correctSelectedInventoryLot", 1)[0]
    form = WAREHOUSE_TWIN.split(
        '<div className="twin-pallet-merge-form">', 1
    )[1].split("</div>}", 1)[0]
    assert "/api/warehouse/pallets/${selectedLocation.pallet.pallet_id}/merge-all" in merger
    assert merger.count("window.confirm(") == 1
    assert "target_pallet_id: target.pallet.pallet_id" in merger
    assert "expected_target_version: target.pallet.version" in merger
    assert "confirmed: true" in merger
    assert "idempotency_key: mergeIdempotencyKey" in merger
    assert "数量" not in form.replace("不拆数量", "")
    assert "原因" not in form
    assert "合并全部剩余货物" in form


def test_p1_16e2_current_twin_only_lists_compatible_target_pallets() -> None:
    assert "const selectedMergeSignature = palletMergeSignature(selectedLocation);" in WAREHOUSE_TWIN
    assert 'item.occupancy_status !== "occupied"' in WAREHOUSE_TWIN
    assert 'item.storage_type === "rack"' in WAREHOUSE_TWIN
    assert "signature?.customerId === selectedMergeSignature.customerId" in WAREHOUSE_TWIN
    assert "signature.inventoryType === selectedMergeSignature.inventoryType" in WAREHOUSE_TWIN
    assert "同客户、同库存类型" in WAREHOUSE_TWIN


def test_map_lot_card_shows_authoritative_quantity_breakdown() -> None:
    block = WAREHOUSE.split("function floor3QuantityHtml(item){", 1)[1].split(
        "function floor3CurrentPallet", 1
    )[0]
    assert "item?.official_inventory" in block
    assert "总数" in block
    assert "可用" in block
    assert "订单已占" in block


def test_drag_mode_stays_separate_from_direct_detail_action() -> None:
    assert "function floor3CanMoveSelectedPallet(row)" in WAREHOUSE
    assert "function floor3CanMovePallet(row){return Boolean(state.floor3.moveMode&&floor3CanMoveSelectedPallet(row))}" in WAREHOUSE
    assert 'state.floor3.moveMode=!state.floor3.moveMode' in WAREHOUSE


def test_warehouse_inline_script_remains_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for frontend syntax validation"
    scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>", WAREHOUSE, re.DOTALL
        )
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "p1-16e-map-first-warehouse.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
