from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
LEDGER = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")
LOCATION_LABEL = (ROOT / "static" / "location-label.html").read_text(
    encoding="utf-8"
)
TWIN = (
    ROOT / "factory_twin" / "frontend" / "src" / "WarehouseTwinApp.tsx"
).read_text(encoding="utf-8")


def test_label_entry_only_lives_in_low_frequency_inventory_management() -> None:
    top_shortcuts = INDEX.split(
        '<div v-if="activePage===\'warehouse\'" class="warehouse-top-shortcuts"',
        1,
    )[1].split("</div>", 1)[0]
    assert "打印货位编号" not in top_shortcuts
    assert 'data-tab="location_labels">货架与货位标签' in LEDGER
    assert 'id="locationLabelWorkbenchSection"' in LEDGER
    assert ">打印货位编号</a>" not in TWIN


def test_label_workbench_is_map_first_and_hides_legacy_address_governance() -> None:
    assert "/api/warehouse/location-label-workbench" in LEDGER
    assert 'id="locationLabelMapPreview"' in LEDGER
    assert "当前正式实测地图" in LEDGER
    assert "在实测地图编辑" in LEDGER
    assert "统一位置地址与永久旧码" not in LEDGER
    assert "新增 / 编辑库位台账" not in LEDGER
    assert "新增 / 编辑区域与容量" not in LEDGER


def test_rack_selection_prints_every_current_cell_and_deep_links_to_map() -> None:
    assert "locationLabelSelectionUrl" in LEDGER
    assert "selectedRack.location_ids" in LEDGER
    assert "map_rack_id" in LEDGER
    assert 'query.get("rack_id")' in TWIN
    assert 'query.get("edit") === "rack"' in TWIN
    assert 'params.set("edit",rack?.map_rack_id?"rack":"area_policy")' in LEDGER
    assert 'setSelected({ kind: "rack", id: target.id })' in TWIN
    assert "每层每格" in LEDGER


def test_80x40_location_labels_use_simple_cell_codes_without_outer_border() -> None:
    assert "print_address_code" in LOCATION_LABEL
    assert "border:0" in LOCATION_LABEL
    assert ".label{width:var(--paper-width);height:var(--paper-height);background:#fff;border:.4mm" not in LOCATION_LABEL
    assert ".label{margin:0;border:.25mm" not in LOCATION_LABEL
    assert "return_to" in LOCATION_LABEL
