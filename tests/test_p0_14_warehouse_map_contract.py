from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (
    ROOT / "factory_twin" / "frontend" / "src" / "WarehouseTwinApp.tsx"
).read_text(encoding="utf-8")
CANVAS_SOURCE = (
    ROOT / "factory_twin" / "frontend" / "src" / "EditorCanvas.tsx"
).read_text(encoding="utf-8")
INVENTORY_SOURCE = (
    ROOT / "factory_twin" / "frontend" / "src" / "warehouseInventory.mjs"
).read_text(encoding="utf-8")
SCENE_SOURCE = (
    ROOT / "factory_twin" / "frontend" / "src" / "industrialScene.ts"
).read_text(encoding="utf-8")
TYPE_SOURCE = (
    ROOT / "factory_twin" / "frontend" / "src" / "types.ts"
).read_text(encoding="utf-8")
BUILT_ROOT = ROOT / "static" / "factory-twin-assets"


def _between(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    end_index = source.index(end, start_index + len(start))
    return source[start_index:end_index]


def _function(source: str, signature: str) -> str:
    start_index = source.index(signature)
    brace_index = source.index("{", start_index + len(signature))
    depth = 0
    for index in range(brace_index, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start_index : index + 1]
    raise AssertionError(f"unterminated function: {signature}")


def test_employee_location_text_never_falls_back_to_internal_code() -> None:
    assert "export function employeeLocationName" in INVENTORY_SOURCE
    helper = _function(INVENTORY_SOURCE, "export function employeeLocationName")
    assert "location_name" in helper
    assert "location_code" not in helper
    assert "位置名称待完善" in helper

    # These were confirmed employee-facing leaks on the formal baseline.
    assert "当前位置 · {selectedLocation.location_code}" not in APP_SOURCE
    assert "{item.location_name} · {item.location_code}" not in APP_SOURCE
    assert '内部码 {item.location_code || "未编"}' not in APP_SOURCE
    assert (
        '<dd>{selectedLocation.area_code || "未分区"} · '
        "{selectedLocation.location_code}</dd>"
    ) not in APP_SOURCE
    assert "`${floorCode} · ${areaCode} 区" not in APP_SOURCE

    # Planning still keeps the stable internal identity; hiding it from employees
    # must not delete the administrator's location-maintenance capability.
    assert "formalAreaCodeDraft" in APP_SOURCE
    assert "location.location_code" in APP_SOURCE


def test_logical_anchor_has_an_explicit_non_pallet_visual_contract() -> None:
    assert 'visual_kind?: "location_anchor" | "physical_pallet"' in TYPE_SOURCE
    mapped_locations = _between(
        INVENTORY_SOURCE,
        "export function buildMappedLocationPallets",
        "function palletBounds",
    )
    assert 'visual_kind: isLogicalAnchor ? "location_anchor" : "physical_pallet"' in mapped_locations
    assert "readableLocationName = employeeLocationName(location)" in mapped_locations
    assert "逻辑位置标记" in mapped_locations
    assert "非实尺度栈板占地" not in mapped_locations

    # The operational renderer branches on the explicit visual kind.  A logical
    # anchor may have a marker footprint for picking, but never enters the real
    # pallet instancing path or uses the employee label's internal code.
    assert "if (pallet.is_logical_anchor)" in CANVAS_SOURCE
    assert "warehouseTheme && !pallet.is_logical_anchor" in CANVAS_SOURCE
    assert "showInternalCodes" in CANVAS_SOURCE
    assert 'pallet.name || "位置名称待完善"' in CANVAS_SOURCE
    assert "CylinderGeometry" in SCENE_SOURCE


def test_normal_move_submit_is_one_request_and_cancel_is_zero_request() -> None:
    clear_move = _between(
        APP_SOURCE,
        "  const clearMoveDrafts = () => {",
        "  const toggleMergeSource =",
    )
    assert "setMoveDrafts([])" in clear_move
    assert "mutateJson(" not in clear_move
    assert "window.confirm" not in clear_move

    submit_move = _between(
        APP_SOURCE,
        "  const confirmMoveDrafts = async () => {",
        "  const beginSelectedAreaLocationPointEdit =",
    )
    assert "window.confirm" not in submit_move
    assert submit_move.count("await mutateJson(") == 1
    assert '"/api/warehouse/twin-operations/move-batches"' in submit_move
    assert "buildMoveBatchPayload(moveBatchIdempotencyKey, moveDrafts)" in submit_move
    assert "页面草稿与本次幂等键已保留" in submit_move

    assert "confirmMapPalletMove" not in APP_SOURCE
    assert "confirmDispatchStagingTransfer" not in APP_SOURCE
    assert APP_SOURCE.count("onClick={confirmMoveDrafts}") == 1


def test_fin_segments_share_one_dispatch_staging_identity() -> None:
    dispatch_builder = _between(
        INVENTORY_SOURCE,
        "export function buildMeasuredDispatchPallets",
        "export function buildMappedLocationPallets",
    )
    assert 'dispatchLocation?.location_code !== "F1-DISPATCH-01"' in dispatch_builder
    assert "operational_group_id" in dispatch_builder
    assert 'zone_code: "一楼成品合并暂存区"' in dispatch_builder

    # The three measured outlines are one visual staging group.  The frontend may
    # spread pallet markers over those outlines, but it must not synthesize three
    # inventory locations or use their FIN codes as employee-facing identities.
    assert "dispatch-location:" in dispatch_builder
    assert "location_code: feature" not in dispatch_builder
    assert "location_id: feature" not in dispatch_builder


def test_built_asset_references_are_complete_without_orphan_hashes() -> None:
    html_files = sorted(BUILT_ROOT.glob("*.html"))
    assert {path.name for path in html_files} == {"index.html", "warehouse-twin.html"}

    referenced_assets: set[str] = set()
    for html_file in html_files:
        html = html_file.read_text(encoding="utf-8")
        referenced_assets.update(
            match.removeprefix("/factory-twin-assets/assets/")
            for match in re.findall(
                r'["\'](/factory-twin-assets/assets/[^"\']+)["\']',
                html,
            )
        )

    actual_assets = {
        path.name for path in (BUILT_ROOT / "assets").iterdir() if path.is_file()
    }
    assert referenced_assets == actual_assets
    assert all((BUILT_ROOT / "assets" / name).stat().st_size > 0 for name in actual_assets)
