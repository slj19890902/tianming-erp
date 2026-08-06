from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from app.services.warehouse_twin_layout import (
    WarehouseTwinLayoutNotFoundError,
    load_warehouse_twin_floor,
)
from factory_twin.scripts.export_erp_twin_floor_maps import build_export


ROOT = Path(__file__).resolve().parents[1]


def test_readonly_twin_floor_asset_requires_supported_floor_and_keeps_inventory_boundary(
    tmp_path: Path,
) -> None:
    asset = tmp_path / "twin.json"
    asset.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": "2026-08-06T00:00:00+00:00",
                "floors": {
                    "3F": {
                        "floor_code": "3F",
                        "name": "三楼实测仓库布局",
                        "features": [],
                        "structures": [],
                        "racks": [],
                        "placements": [],
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    floor = load_warehouse_twin_floor("3f", path=asset)
    assert floor["floor_code"] == "3F"
    assert "正式库存数量仍以 ERP 库存账为准" in floor["projection_notice"]
    with pytest.raises(WarehouseTwinLayoutNotFoundError):
        load_warehouse_twin_floor("2F", path=asset)


def test_export_marks_only_erp_zones_as_location_area_mappings_and_keeps_pallets_isolated() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE twin_layouts (
            id TEXT PRIMARY KEY, name TEXT, floor_code TEXT, source_name TEXT,
            source_units TEXT, bounds_json TEXT, structures_json TEXT, updated_at TEXT
        );
        CREATE TABLE twin_layout_features (
            id TEXT PRIMARY KEY, layout_id TEXT, feature_code TEXT, name TEXT,
            feature_kind TEXT, subtype TEXT, points_json TEXT, width_mm REAL,
            storage_mode TEXT, elevation_mm REAL, storage_height_mm REAL,
            color TEXT, status TEXT
        );
        CREATE TABLE twin_equipment_placements (
            id TEXT, layout_id TEXT, name TEXT, x_mm REAL, y_mm REAL,
            width_mm REAL, depth_mm REAL, height_mm REAL, rotation_deg INTEGER,
            is_confirmed INTEGER, is_locked INTEGER
        );
        CREATE TABLE twin_rack_placements (
            id TEXT, layout_id TEXT, rack_code TEXT, name TEXT, x_mm REAL, y_mm REAL,
            width_mm REAL, depth_mm REAL, height_mm REAL, rotation_deg INTEGER,
            status TEXT, is_locked INTEGER
        );
        """
    )
    for floor in ("1F", "3F"):
        connection.execute(
            "INSERT INTO twin_layouts VALUES (?,?,?,?,?,?,?,?)",
            (
                floor,
                f"{floor}布局",
                floor,
                f"{floor}.dxf",
                "mm",
                json.dumps({"min_x": 0, "min_y": 0, "max_x": 10000, "max_y": 10000}),
                json.dumps([{"kind": "wall", "geometry": {"points": [[0, 0], [10000, 0]]}}]),
                "2026-08-06",
            ),
        )
    connection.execute(
        "INSERT INTO twin_layout_features VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "erp-zone",
            "3F",
            "ZONE-3F-ERP-A1",
            "A1成品区",
            "zone",
            "finished_storage",
            json.dumps([[0, 0], [1000, 0], [1000, 1000]]),
            None,
            "floor",
            0,
            1800,
            "#3b82f6",
            "candidate",
        ),
    )
    connection.execute(
        "INSERT INTO twin_layout_features VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "measured-e4",
            "3F",
            "ZONE-3F-FG-002",
            "送剩零头区",
            "zone",
            "delivery_surplus",
            json.dumps([[4000, 0], [5000, 0], [5000, 1000]]),
            None,
            "floor",
            0,
            1800,
            "#f59e0b",
            "candidate",
        ),
    )
    connection.execute(
        "INSERT INTO twin_layout_features VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "actual-zone",
            "3F",
            "ZONE-3F-FG-001",
            "现场零头区",
            "zone",
            "delivery_surplus",
            json.dumps([[2000, 0], [3000, 0], [3000, 1000]]),
            None,
            "floor",
            0,
            1800,
            "#f59e0b",
            "candidate",
        ),
    )
    payload = build_export(connection)
    floor = payload["floors"]["3F"]
    by_code = {item["feature_code"]: item for item in floor["features"]}
    assert by_code["ZONE-3F-ERP-A1"]["erp_area_code"] == "A1"
    assert by_code["ZONE-3F-FG-002"]["erp_area_code"] is None
    assert by_code["ZONE-3F-FG-001"]["erp_area_code"] == "E4"
    assert floor["erp_area_codes"] == ["A1", "E4"]
    assert floor["pallets_inventory_linked"] is False
    assert floor["pallets"] == []


def test_checked_in_twin_asset_contains_locked_shared_lift_projected_columns_and_all_erp_areas() -> None:
    payload = json.loads(
        (ROOT / "static" / "factory_maps" / "twin_layout_v1.json").read_text(encoding="utf-8")
    )
    one = payload["floors"]["1F"]
    three = payload["floors"]["3F"]
    one_lift = next(item for item in one["features"] if item["feature_code"] == "LIFT-002")
    three_lift = next(item for item in three["features"] if item["feature_code"] == "LIFT-002")
    assert one_lift["is_locked"] is True
    assert three_lift["is_locked"] is True
    assert one_lift["points"] == three_lift["points"]
    assert one_lift["width_mm"] == three_lift["width_mm"] == 3000
    assert sum(1 for item in one["features"] if item["subtype"] == "custom_column") == 45
    assert sum(1 for item in one["features"] if item["subtype"] == "custom_wall") >= 22
    assert set(three["erp_area_codes"]) == {
        "A1", "A2", "AB1", "AB2", "B1", "B2", "C1", "C2", "CD1", "D1", "D2",
        "DE1", "E1", "E2", "E3", "E4", "F1", "F2", "F3", "F4", "F12", "F34",
    }
    assert one["pallets_inventory_linked"] is False
    assert three["pallets_inventory_linked"] is False
    assert len(one["pallets"]) == 7
