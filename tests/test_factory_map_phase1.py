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
        "addb330ed2887751f0486b6a3ed00fda6f2c6f67de3becfb9f01a31467a76963"
    )
    assert payload["source"]["file_name"] == "TM_FACTORY_1F_ERP_V4_CONFIRMED_OUTDOOR.dxf"
    assert payload["source"]["standard_load_compatible"] is False
    assert len(payload["primitives"]) == 77
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


def test_factory_map_service_and_api_are_read_only_and_floor_scoped() -> None:
    payload = load_factory_map("1f")
    assert payload["floor_code"] == "1F"

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
