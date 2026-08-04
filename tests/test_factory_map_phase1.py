from __future__ import annotations

import json
from pathlib import Path

import ezdxf
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.warehouse import can_read, router as warehouse_router
from app.services.factory_maps import load_factory_map
from scripts.import_factory_dxf import parse_factory_dxf


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAP_PATH = PROJECT_ROOT / "static" / "factory_maps" / "factory_1f_v4.json"
EXPECTED_MACHINE_ASSETS = {
    "machine-printer-new-lineart-25d.webp",
    "machine-printer-old-lineart-25d.webp",
    "machine-slitter-lineart-25d.webp",
    "machine-gluer-semi-lineart-25d.webp",
    "machine-stitcher-semi-lineart-25d.webp",
    "machine-stitcher-arm-lineart-25d.webp",
    "machine-diecutter-lineart-25d.webp",
    "machine-packer-lineart-25d.webp",
    "machine-slotter-lineart-25d.webp",
}


def test_importer_converts_metric_dxf_to_mm_and_groups_zone(tmp_path: Path) -> None:
    source = tmp_path / "factory.dxf"
    document = ezdxf.new("R2007")
    document.header["$INSUNITS"] = 6
    for layer in ("00_BASE_墙体", "01_COLUMN_柱子", "05_ZONE_区域"):
        document.layers.add(layer)
    modelspace = document.modelspace()
    modelspace.add_line(
        (0, 0), (4, 0), dxfattribs={"layer": "00_BASE_墙体"}
    )
    modelspace.add_lwpolyline(
        [(0, 0), (1, 0), (1, 1), (0, 1)],
        close=True,
        dxfattribs={"layer": "01_COLUMN_柱子"},
    )
    modelspace.add_lwpolyline(
        [(1, 1), (3, 1), (3, 2), (1, 2)],
        close=True,
        dxfattribs={"layer": "05_ZONE_区域"},
    )
    modelspace.add_text("1F-A-001", dxfattribs={"height": 0.2}).set_placement(
        (1.5, 1.5)
    )
    document.saveas(source)

    payload = parse_factory_dxf(source)

    assert payload["source"]["input_units"] == "m"
    assert payload["source"]["output_units"] == "mm"
    assert payload["source"]["standard_load_compatible"] is True
    assert payload["bounds_mm"]["max_x"] == 4000
    zone = next(item for item in payload["primitives"] if item["kind"] == "zone")
    assert zone["zone_code"] == "ZONE-1F-A"
    assert zone["source_label"] == "1F-A-001"


def test_confirmed_v4_asset_preserves_real_dxf_evidence() -> None:
    payload = json.loads(MAP_PATH.read_text(encoding="utf-8"))

    assert payload["floor_code"] == "1F"
    assert payload["map_version"] == "V4"
    assert payload["source"]["sha256"] == (
        "226e97de6a9ce51cc8d41486d08d385853623f1f73e726ca903b6a4a80e39836"
    )
    assert payload["source"]["file_name"] == "#TM_FACTORY_1F_ERP_V4_CONFIRMED_OUTDOOR.dxf"
    assert payload["source"]["standard_load_compatible"] is False
    assert len(payload["primitives"]) == 82
    assert sum(item["kind"] == "zone" for item in payload["primitives"]) == 17
    assert sum(
        item["kind"] == "zone" and "zone_code" not in item
        for item in payload["primitives"]
    ) == 1
    assert sum(
        item.get("zone_code") == "ZONE-1F-P" for item in payload["primitives"]
    ) == 3
    assert sum(
        item.get("zone_code") == "ZONE-1F-D" for item in payload["primitives"]
    ) == 2
    assert sum(
        item.get("zone_code") == "ZONE-1F-OUT-E" for item in payload["primitives"]
    ) == 2
    assert sum(
        item.get("zone_code") == "ZONE-1F-OUT-S" for item in payload["primitives"]
    ) == 1
    assert {item["zone_code"] for item in payload["business_zones"]} == {
        "ZONE-1F-A",
        "ZONE-1F-B",
        "ZONE-1F-C",
        "ZONE-1F-D",
        "ZONE-1F-E",
        "ZONE-1F-OUT-E",
        "ZONE-1F-OUT-S",
        "ZONE-1F-P",
    }
    outdoor_zones = [
        item for item in payload["business_zones"] if item["zone_code"].startswith("ZONE-1F-OUT-")
    ]
    assert {item["short_code"] for item in outdoor_zones} == {"外东", "外南"}
    assert all(item["boundary_status"] == "drawn" for item in outdoor_zones)
    assert all(item["temporary_only"] is True for item in outdoor_zones)
    assert all(item["weather_exposed"] is True for item in outdoor_zones)
    assert all(
        set(item["allow_stock_types"])
        == {"INBOUND_WAIT_RECEIPT", "FINISHED_WAIT_LOADING"}
        for item in outdoor_zones
    )
    assert any("EQ-1F-SLITTER-01" in warning for warning in payload["warnings"])


def test_confirmed_v4_asset_has_employee_friendly_floor_plan_presentation() -> None:
    payload = json.loads(MAP_PATH.read_text(encoding="utf-8"))

    assert payload["presentation"] == {
        "mode": "employee_floor_plan",
        "language": "zh-CN",
        "wall_snap_ratio": 0.12,
        "door_symbol": "plan_swing",
        "window_symbol": "double_line",
    }

    labels = {item["text"]: item.get("display_text") for item in payload["labels"]}
    assert labels["EQ-1F-PRINT-NEW"] == "新水性印刷机"
    assert labels["粘箱机"] == "半自动粘箱机"
    assert labels["E-1F-PINT-OLD"] == "旧印刷机"
    assert labels["PATH-1F-001"] == "货运通道（禁堆）"
    assert labels["1F-A-001"] == "入口缓冲区①"
    assert labels["1F-OUT-E-001"] == "东侧临时装卸区①"
    assert labels["1F-OUT-S-001"] == "南侧临时装卸区"

    mold_racks = {
        item["display_name"]: item
        for item in payload["primitives"]
        if item.get("staff_category") == "mold_rack"
    }
    assert mold_racks["小模切机上方模具架"]["rack_levels"] == 2
    assert mold_racks["中模切机上方模具架"]["rack_levels"] == 1
    assert mold_racks["中模切机上方模具架"]["planned_rack_levels"] == 2
    assert mold_racks["中模切机左侧模具架"]["rack_levels"] == 3
    assert mold_racks["小模切机上方模具架"]["display_bays"] == 2
    assert mold_racks["中模切机上方模具架"]["display_bays"] == 4
    assert mold_racks["中模切机左侧模具架"]["display_bays"] == 3
    assert all(item["bay_status"] == "visual_estimate" for item in mold_racks.values())

    overlays = payload["staff_overlays"]
    assert {item["id"] for item in overlays if item["kind"] == "mold_storage"} == {
        "MOLD-OVERSIZE-WEST",
        "MOLD-OVERSIZE-SOUTH",
    }
    assert all(
        "超大模具" in item["display_name"]
        for item in overlays
        if item["kind"] == "mold_storage"
    )

    machines = [item for item in payload["primitives"] if item["kind"] == "machine"]
    assert len(machines) == 11
    assert all(item["interactive"] is False for item in machines)
    assert all(item["storage_rule"] == "NO_PALLET_ON_MACHINE" for item in machines)
    assert all(item["machine_asset"].endswith("-25d.webp") for item in machines)

    primitive_by_id = {item["id"]: item for item in payload["primitives"]}
    assert primitive_by_id["B9A"]["kind"] == "freight_elevator"
    assert primitive_by_id["B9A"]["operational"] is False
    assert primitive_by_id["B9B"]["kind"] == "freight_elevator"
    assert primitive_by_id["B9B"]["operational"] is True
    assert primitive_by_id["B98"]["kind"] == "rack"
    assert primitive_by_id["B98"]["rack_levels"] == 1
    assert primitive_by_id["B97"]["display_name"] == "开槽老虎机"
    assert primitive_by_id["B97"]["machine_asset_opacity"] < 0.5
    assert primitive_by_id["B99"]["kind"] == "pallet_spot"
    assert primitive_by_id["B99"]["capacity_pallets"] == 2
    assert primitive_by_id["B99"]["temporary_only"] is True
    assert "presentation_hidden" not in primitive_by_id["B5F"]

    asset_dir = MAP_PATH.parent / "assets"
    assert EXPECTED_MACHINE_ASSETS <= {path.name for path in asset_dir.iterdir()}


def test_factory_map_service_and_api_are_read_only_and_floor_scoped() -> None:
    payload = load_factory_map("1f")
    assert payload["floor_code"] == "1F"
    assert set(payload["machine_assets"]) == EXPECTED_MACHINE_ASSETS
    assert all(
        value.startswith("data:image/webp;base64,")
        for value in payload["machine_assets"].values()
    )

    application = FastAPI()
    application.include_router(warehouse_router, prefix="/api/warehouse")
    application.dependency_overrides[can_read] = lambda: object()
    client = TestClient(application)

    response = client.get("/api/warehouse/factory-maps/floors/1f")
    assert response.status_code == 200
    assert response.json()["map_version"] == "V4"
    assert response.json()["source"]["output_units"] == "mm"
    missing = client.get("/api/warehouse/factory-maps/floors/3f")
    assert missing.status_code == 404
