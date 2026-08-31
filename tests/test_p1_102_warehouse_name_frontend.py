from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TWIN_APP = (
    ROOT / "factory_twin" / "frontend" / "src" / "WarehouseTwinApp.tsx"
).read_text(encoding="utf-8")
TWIN_INVENTORY = (
    ROOT / "factory_twin" / "frontend" / "src" / "warehouseInventory.mjs"
).read_text(encoding="utf-8")
WAREHOUSE_STATIC = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")


def test_map_and_formal_area_options_use_the_employee_area_projection() -> None:
    assert "LEGACY_V11_RIGHT_AREA_CODES" in TWIN_INVENTORY
    assert "const rightLabel = `右区${code}`" in TWIN_INVENTORY
    assert "return description ? `${rightLabel}·${description}` : rightLabel" in TWIN_INVENTORY
    assert (
        "{ ...projected, name: employeeAreaName(projected, { floorCode }) }"
        in TWIN_APP
    )
    assert TWIN_APP.count(
        "employeeAreaName(area, { floorCode: area.floor_code })"
    ) >= 3
    assert (
        "<b>{employeeAreaName(selectedAreaFeature, { floorCode })}</b>" in TWIN_APP
    )


def test_bound_area_name_remains_editable_but_area_code_identity_stays_locked() -> None:
    assert (
        '<span>区域编号</span><input maxLength={30} '
        'disabled={Boolean(selectedExistingAreaId)}'
        in TWIN_APP
    )
    assert (
        '<span>区域名称</span><input maxLength={100} '
        'value={formalAreaNameDraft}'
        in TWIN_APP
    )
    assert (
        '<span>区域名称</span><input maxLength={100} '
        'disabled={Boolean(selectedExistingAreaId)}'
        not in TWIN_APP
    )


def test_legacy_warehouse_page_prefers_backend_employee_area_name() -> None:
    assert (
        'function readableArea(row){const projected=String('
        'row?.employee_area_name||"").trim();if(projected)return projected;'
        in WAREHOUSE_STATIC
    )
    assert '$("warehouseAreaName").value=readableArea(area)' in WAREHOUSE_STATIC
    assert "${readableArea(area)}" in WAREHOUSE_STATIC
