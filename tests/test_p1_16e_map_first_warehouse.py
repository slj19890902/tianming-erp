from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")


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


def test_row_ledgers_are_admin_advanced_entries_not_daily_tabs() -> None:
    assert 'class="btn admin-only inventory-ledger-tab" data-tab="finished">高级成品台账' in WAREHOUSE
    assert 'class="btn admin-only inventory-ledger-tab" data-tab="semi_finished">高级半成品台账' in WAREHOUSE
    assert 'onclick="switchTab(\'finished\')">高级库存台账' in WAREHOUSE
    assert 'onclick="switchLocationView(\'ledger\')">库位结构维护' in WAREHOUSE
    assert 'state.user.role!=="admin"||state.readOnly' in WAREHOUSE
    assert 'document.querySelectorAll(".admin-only")' in WAREHOUSE


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
