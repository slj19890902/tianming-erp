from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.warehouse_twin_layout_editor import (
    WarehouseTwinLayoutEditConflictError,
    _floor_revision,
    create_warehouse_twin_rack,
    delete_warehouse_twin_rack,
    update_warehouse_twin_rack,
    update_warehouse_twin_zone_policy,
)
from factory_twin.scripts.export_erp_twin_floor_maps import preserve_operator_layout_edits


ROOT = Path(__file__).resolve().parents[1]


def _asset(path: Path) -> Path:
    floor = {
        "layout_id": "layout-3f",
        "floor_code": "3F",
        "features": [
            {
                "id": "zone-f1",
                "feature_code": "ZONE-3F-ERP-F1",
                "name": "F1",
                "feature_kind": "zone",
                "subtype": "rack_storage",
                "points": [[0, 0], [10000, 0], [10000, 10000], [0, 10000]],
                "version": 1,
                "erp_area_code": "F1",
            }
        ],
        "racks": [],
        "pallets": [],
    }
    floor["revision"] = _floor_revision(floor)
    path.write_text(
        json.dumps({"schema_version": 1, "generated_at": "old", "floors": {"3F": floor}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _rack_values(**changes) -> dict:
    values = {
        "name": "F1测试货架",
        "x_mm": 5000,
        "y_mm": 5000,
        "width_mm": 2800,
        "depth_mm": 1100,
        "height_mm": 2200,
        "levels": 3,
        "level_heights_mm": [700, 1450],
        "cargo_rows": 4,
        "bays": 1,
        "access_side": "south",
        "min_aisle_width_mm": 1500,
        "rotation_deg": 0,
        "color": "#38bdf8",
    }
    values.update(changes)
    return values


def test_rack_crud_is_versioned_idempotent_and_deletion_is_recoverable(tmp_path: Path) -> None:
    path = _asset(tmp_path / "layout.json")
    revision = json.loads(path.read_text(encoding="utf-8"))["floors"]["3F"]["revision"]
    created = create_warehouse_twin_rack(
        "3f",
        expected_revision=revision,
        operation_key="create-rack-0001",
        area_feature_id="zone-f1",
        values=_rack_values(),
        path=path,
    )
    assert created.applied is True
    assert created.value["area_code"] == "F1"
    assert created.value["rack_code"] == "RACK-3F-F1-EDIT-001"

    retried = create_warehouse_twin_rack(
        "3F",
        expected_revision=revision,
        operation_key="create-rack-0001",
        area_feature_id="zone-f1",
        values=_rack_values(),
        path=path,
    )
    assert retried.applied is False
    assert retried.value["id"] == created.value["id"]

    updated = update_warehouse_twin_rack(
        "3F",
        created.value["id"],
        expected_revision=created.floor_revision,
        expected_version=1,
        operation_key="update-rack-0001",
        values=_rack_values(name="F1现场货架", width_mm=5600, rotation_deg=90),
        path=path,
    )
    assert updated.value["width_mm"] == 5600
    assert updated.value["rotation_deg"] == 90
    assert updated.value["version"] == 2

    with pytest.raises(WarehouseTwinLayoutEditConflictError):
        update_warehouse_twin_rack(
            "3F",
            created.value["id"],
            expected_revision=updated.floor_revision,
            expected_version=1,
            operation_key="update-rack-stale",
            values=_rack_values(),
            path=path,
        )

    deleted = delete_warehouse_twin_rack(
        "3F",
        created.value["id"],
        expected_revision=updated.floor_revision,
        expected_version=2,
        operation_key="delete-rack-0001",
        path=path,
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    floor = document["floors"]["3F"]
    assert deleted.value["inventory_changed"] is False
    assert floor["racks"] == []
    assert floor["retired_racks"][0]["id"] == created.value["id"]
    assert len(floor["layout_edit_receipts"]) == 3


def test_zone_policy_keeps_business_usage_separate_from_storage_layout(tmp_path: Path) -> None:
    path = _asset(tmp_path / "layout.json")
    revision = json.loads(path.read_text(encoding="utf-8"))["floors"]["3F"]["revision"]
    result = update_warehouse_twin_zone_policy(
        "3F",
        "zone-f1",
        expected_revision=revision,
        expected_version=1,
        operation_key="zone-policy-0001",
        allowed_inventory_types=["finished", "semi_finished", "raw_material"],
        storage_layout="mixed",
        path=path,
    )
    assert result.value["allowed_inventory_types"] == ["finished", "semi_finished", "raw_material"]
    assert result.value["storage_layout"] == "mixed"
    assert result.value["subtype"] == "rack_storage"


def test_refresh_export_preserves_operator_racks_and_zone_policy(tmp_path: Path) -> None:
    existing = tmp_path / "twin.json"
    existing.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "floors": {
                    "3F": {
                        "layout_id": "same",
                        "layout_edited_at": "2026-08-06T00:00:00+00:00",
                        "racks": [{"id": "operator-rack", "rack_code": "RACK-3F-F1-EDIT-001"}],
                        "retired_racks": [{"id": "old-rack"}],
                        "layout_edit_receipts": [{"operation_key": "saved"}],
                        "features": [{"id": "zone", "allowed_inventory_types": ["finished"], "storage_layout": "mixed"}],
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    refreshed = {
        "schema_version": 1,
        "floors": {
            "3F": {
                "layout_id": "same",
                "racks": [{"id": "database-rack"}],
                "features": [{"id": "zone", "name": "CAD刷新后的区域"}],
                "revision": "before",
            }
        },
    }
    result = preserve_operator_layout_edits(refreshed, existing)
    floor = result["floors"]["3F"]
    assert floor["racks"] == [{"id": "operator-rack", "rack_code": "RACK-3F-F1-EDIT-001"}]
    assert floor["features"][0]["name"] == "CAD刷新后的区域"
    assert floor["features"][0]["storage_layout"] == "mixed"
    assert floor["features"][0]["allowed_inventory_types"] == ["finished"]


def test_checked_in_f1_uses_two_combined_independently_editable_racks() -> None:
    payload = json.loads((ROOT / "static" / "factory_maps" / "twin_layout_v1.json").read_text(encoding="utf-8"))
    three = payload["floors"]["3F"]
    racks = [item for item in three["racks"] if item["rack_code"].startswith("RACK-3F-F1-")]
    assert len(racks) == 2
    assert {item["width_mm"] for item in racks} == {5600.0}
    assert {item["depth_mm"] for item in racks} == {1100.0}
    assert {item["rotation_deg"] for item in racks} == {90}
    assert all(item["area_code"] == "F1" for item in racks)
