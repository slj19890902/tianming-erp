from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE_HTML = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")


def test_measured_twin_floor_is_the_operational_default() -> None:
    assert 'data-location-view="floor3" type="button">数字孪生库位图' in WAREHOUSE_HTML
    assert 'locationView:"floor3"' in WAREHOUSE_HTML
    assert 'await switchLocationView("floor3");return' in WAREHOUSE_HTML
    assert 'aria-label="数字孪生库位管理">数字孪生库位' in WAREHOUSE_HTML
    assert 'id="twinDashboardSection"' in WAREHOUSE_HTML
    assert "数字孪生智慧仓储综合看板" in WAREHOUSE_HTML
    assert "ERP业务数字孪生，非传感器实时定位" in WAREHOUSE_HTML
    assert "async function loadTwinDashboard" in WAREHOUSE_HTML
    assert "/api/warehouse/twin-dashboard/overview?days=${state.twinDashboard.days}" in WAREHOUSE_HTML


def test_floor_drilldown_and_safe_return_preserve_existing_map_contract() -> None:
    assert "async function openTwinFloor(floorCode,locationId=null)" in WAREHOUSE_HTML
    assert "await switchLocationView(\"floor3\")" in WAREHOUSE_HTML
    assert "await focusTwinOperationalLocation(Number(locationId))" in WAREHOUSE_HTML
    assert "仓库总览 ‹" in WAREHOUSE_HTML
    assert 'data-location-view="floor3" type="button">数字孪生库位图' in WAREHOUSE_HTML
    assert "浏览拖动不会移动货物" in WAREHOUSE_HTML


def test_location_map_uses_measured_twin_geometry_and_only_maps_legacy_area_codes() -> None:
    assert 'id="twinOperationalSvg"' in WAREHOUSE_HTML
    assert "/api/warehouse/twin-layout/floors/${normalized}" in WAREHOUSE_HTML
    assert "function twinOperationalFeatureSvg" in WAREHOUSE_HTML
    assert "item.erp_area_code" in WAREHOUSE_HTML
    assert "旧库位只按区域编码关联到这里，不搬入旧方块坐标" in WAREHOUSE_HTML
    assert "async function loadTwinOperationalLocations" in WAREHOUSE_HTML
    assert "formatDateTime(data.generated_at)" in WAREHOUSE_HTML
    assert "fmtTime(data.generated_at)" not in WAREHOUSE_HTML


def test_inventory_code_search_is_server_side_debounced_and_race_safe() -> None:
    assert "/api/warehouse/twin-dashboard/search?inventory_code=${encodeURIComponent(keyword)}" in WAREHOUSE_HTML
    assert "state.twinDashboard.searchTimer=setTimeout(searchTwinInventory,350)" in WAREHOUSE_HTML
    assert "requestId!==state.twinDashboard.searchRequestId" in WAREHOUSE_HTML
    assert '$("twinInventoryCode").value.trim()!==keyword' in WAREHOUSE_HTML
    assert "openTwinSearchResult" in WAREHOUSE_HTML
    assert "尚无已发布坐标，仅显示文字位置" in WAREHOUSE_HTML


def test_dashboard_read_paths_do_not_add_inventory_mutations() -> None:
    dashboard_script = WAREHOUSE_HTML.split("function twinUnitLabel", 1)[1].split(
        "async function switchLocationView", 1
    )[0]
    for method in ('method:"POST"', 'method:"PUT"', 'method:"PATCH"', 'method:"DELETE"'):
        assert method not in dashboard_script
    assert "P1-27现场安全容量尚未确认" in WAREHOUSE_HTML
    assert "不计算伪精确满载率" in WAREHOUSE_HTML


def test_dashboard_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    script = WAREHOUSE_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    target = tmp_path / "warehouse-inline.js"
    target.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
