from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = ROOT / "static" / "warehouse.html"
INDEX = ROOT / "static" / "index.html"
FINISHED_LABEL = ROOT / "static" / "finished-goods-label.html"
INCOMING_MOBILE = ROOT / "static" / "incoming.html"
MOBILE_ERP = ROOT / "static" / "mobile_erp.html"
MOBILE_PICK = ROOT / "static" / "mobile_delivery_pick.html"
MOBILE_STOCKTAKE = ROOT / "static" / "mobile_stocktake.html"
MOBILE_TIANHUA_PICK = ROOT / "static" / "mobile_tianhua_pick.html"
MOBILE_MOLD = ROOT / "static" / "mobile_mold_live.html"
DELIVERY_PICK_PRINT = ROOT / "static" / "delivery-pick-print.html"
LOCATION_LABEL = ROOT / "static" / "location-label.html"
REQUISITION_PRINT = ROOT / "static" / "requisition-production-print.html"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _inline_javascript(path: Path) -> str:
    blocks = []
    for attributes, body in re.findall(
        r"<script([^>]*)>(.*?)</script>", _source(path), flags=re.IGNORECASE | re.DOTALL
    ):
        if re.search(r"\bsrc\s*=", attributes, flags=re.IGNORECASE):
            continue
        blocks.append(body)
    return "\n".join(blocks)


def test_admin_address_editor_uses_preview_then_single_explicit_confirm() -> None:
    source = _source(WAREHOUSE)
    assert 'id="locationAddressManager"' in source
    assert "/api/warehouse/location-addresses/preview" in source
    assert "/api/warehouse/location-addresses/confirm" in source
    assert "preview_fingerprint:current.preview.preview_fingerprint" in source
    assert "idempotency_key:current.attemptKey" in source
    assert "确认保存并保留旧码" in source
    assert "保存只改当前地址并保留旧码，不移动货物、不改数量、不生成库存流水" in source
    confirm_body = source.split("async function confirmLocationAddressChange()", 1)[1].split(
        "\n", 1
    )[0]
    assert "window.confirm" not in confirm_body
    assert "confirm(" not in confirm_body


def test_search_map_labels_and_cross_module_views_prefer_current_chinese_address() -> None:
    warehouse = _source(WAREHOUSE)
    index = _source(INDEX)
    label = _source(FINISHED_LABEL)
    assert "function readableLocation(row)" in warehouse
    assert "function readableAssetLocation(row)" in warehouse
    assert "employee_location_name||row?.current_address_name||row?.location_name" in warehouse
    assert "位置待人工核对" not in warehouse
    assert "位置待确认" in warehouse
    assert '${h(readableAssetLocation(row))}' in warehouse
    assert '${h(readableLocation(row.location))}' in warehouse
    assert "result.location_match" in warehouse
    assert "旧码 ${h(result.location_match.matched_alias)} 已对应当前地址" in warehouse
    assert "function openTwinLocationMatch()" in warehouse
    assert "location.employee_location_name || location.current_address_name || location.location_name" in index
    assert "event.employee_location_name || event.current_address_name || event.location_name" in index
    assert "{{ inventoryLocation(location) }}｜{{ location.is_temporary ? '临时' : '固定' }}" in index
    assert "{{ row.current_warehouse_location_name || '位置名称待完善' }}" in index
    assert "row.current_inventory_status==='located'" in index
    assert "<th>当前库位</th>" in index
    assert "完工时：{{ row.completion_warehouse_location_name" not in index
    assert "{{ lot.location_name || \"位置名称待完善\" }}" in index
    assert "{{ lot.pallet_code || \"-\" }}" not in index
    assert "const code=readableLocation(location)" in warehouse
    assert "x.from_location_name||\"-\"" in warehouse
    assert "x.to_location_name||\"-\"" in warehouse
    assert "location.employee_location_name || location.current_address_name || location.location_name" in label
    assert "location?.area_name" in index
    assert "row?.completion_actionable === false" in index
    assert "row.completion_block_message" in index
    assert "available_material_input_quantity ?? 0" in index
    location_markup = label.split('<section class="location">', 1)[1].split("</section>", 1)[0]
    assert "location.location_code" not in location_markup


def test_employee_mobile_print_and_location_label_views_hide_internal_codes() -> None:
    incoming = _source(INCOMING_MOBILE)
    mobile = _source(MOBILE_ERP)
    pick = _source(MOBILE_PICK)
    stocktake = _source(MOBILE_STOCKTAKE)
    tianhua = _source(MOBILE_TIANHUA_PICK)
    mold = _source(MOBILE_MOLD)
    pick_print = _source(DELIVERY_PICK_PRINT)
    location_label = _source(LOCATION_LABEL)
    requisition_print = _source(REQUISITION_PRINT)

    assert "row.employee_location_name || row.current_address_name || row.location_name" in incoming
    assert 'function readableLocation(row)' in mobile
    location_card = mobile.split("function locationCard(position)", 1)[1].split(
        "function stockCard", 1
    )[0]
    assert "position.location_code" not in location_card
    move_body = mobile.split("async function confirmWarehouseMapMove()", 1)[1].split(
        "async function confirmWarehousePhysicalReturn", 1
    )[0]
    assert "window.confirm" not in move_body
    assert "group.label||group.employee_location_name||group.current_address_name||group.location_name" in pick
    assert "group.pallet_code" not in pick
    assert "function locationName(location)" in stocktake
    assert "<strong>${h(locationName(location))}</strong>" in stocktake
    assert "source.employee_location_name || source.current_address_name || source.location_name" in tianhua
    assert 'row.employee_location_name||row.current_address_name||row.location_name' in mold
    assert "row.pallet" not in pick_print
    assert "line.location_code ||" not in pick_print
    label_html = location_label.split("function labelHtml(row)", 1)[1].split(
        "function renderRows", 1
    )[0]
    assert "row.location_code" not in label_html
    assert "row.display_path||row.employee_location_name||row.current_address_name" in label_html
    assert "card.pallet_code" not in requisition_print


@pytest.mark.parametrize(
    ("path", "filename"),
    [
        (WAREHOUSE, "p186-warehouse.js"),
        (INDEX, "p186-index.js"),
        (FINISHED_LABEL, "p186-finished-label.js"),
        (INCOMING_MOBILE, "p186-incoming-mobile.js"),
        (MOBILE_ERP, "p186-mobile-erp.js"),
        (MOBILE_PICK, "p186-mobile-pick.js"),
        (MOBILE_STOCKTAKE, "p186-mobile-stocktake.js"),
        (MOBILE_TIANHUA_PICK, "p186-mobile-tianhua.js"),
        (MOBILE_MOLD, "p186-mobile-mold.js"),
        (DELIVERY_PICK_PRINT, "p186-pick-print.js"),
        (LOCATION_LABEL, "p186-location-label.js"),
        (REQUISITION_PRINT, "p186-requisition-print.js"),
    ],
)
def test_changed_inline_javascript_is_syntax_valid(
    path: Path,
    filename: str,
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    script = tmp_path / filename
    script.write_text(_inline_javascript(path), encoding="utf-8")
    subprocess.run([node, "--check", str(script)], check=True, capture_output=True, text=True)
