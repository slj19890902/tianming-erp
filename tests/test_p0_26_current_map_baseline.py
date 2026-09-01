from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.models.warehouse_inventory import WarehouseArea, WarehouseFloor, WarehouseLocation
from app.services.warehouse_current_map_baseline import _pack_slots
from app.services.warehouse_location_address import published_measured_map_readiness
from factory_twin.scripts.migrate_floor3_current_map import (
    FORMAL_AREA_IDS,
    MEASURED_AE_ZONES,
    _canonical_floor_revision,
    migrate_document,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _feature_by_area(document: dict, area_code: str) -> dict:
    return next(
        feature
        for feature in document["floors"]["3F"]["features"]
        if str(feature.get("erp_area_code") or "").upper() == area_code
    )


def _bounds(feature: dict) -> tuple[float, float]:
    xs = [float(point[0]) for point in feature["points"]]
    ys = [float(point[1]) for point in feature["points"]]
    return max(xs) - min(xs), max(ys) - min(ys)


def test_owner_measured_map_migration_replaces_e4_without_inventing_capacity() -> None:
    source = json.loads(
        (PROJECT_ROOT / "static" / "factory_maps" / "twin_layout_v1.json").read_text(
            encoding="utf-8"
        )
    )
    migrated = migrate_document(deepcopy(source))
    floor = migrated["floors"]["3F"]

    assert floor["revision"] == _canonical_floor_revision(floor)
    assert not any(
        str(feature.get("erp_area_code") or "").upper() == "E4"
        for feature in floor["features"]
    )

    loose = _feature_by_area(migrated, "FIN-LOOSE-001")
    assert loose["storage_layout"] == "functional"
    assert loose["formal_area_id"] == 16
    assert loose["no_stacking"] is True
    assert "无栈板位" in loose["usage_notice"]

    semi = _feature_by_area(migrated, "SEMI-011")
    assert _bounds(semi) == (5300.0, 1200.0)
    assert semi["area_mm2"] == 6_360_000.0
    assert semi["allowed_inventory_types"] == ["semi_finished"]
    assert semi["formal_area_id"] == 48

    for area_code, (_x_m, width_m, height_m) in MEASURED_AE_ZONES.items():
        feature = _feature_by_area(migrated, area_code)
        assert _bounds(feature) == (width_m * 1000, height_m * 1000)
        assert feature["formal_area_id"] == FORMAL_AREA_IDS[area_code]

    for area_code in ("FG-005", "FG-006", "FG-007", "SEMI-008", "RAW-004"):
        assert _feature_by_area(migrated, area_code)["formal_area_id"] == FORMAL_AREA_IDS[
            area_code
        ]


def test_measured_ground_pack_never_exceeds_zone_and_uses_standard_pallets() -> None:
    slots = _pack_slots((20729.0, -19681.5, 3500.0, 14000.0), 33)
    assert len(slots) == 33
    assert {(slot["width"], slot["depth"]) for slot in slots} == {(1000, 1200)}
    assert max(slot["x"] + slot["width"] for slot in slots) <= 24229.0
    assert max(slot["y"] + slot["depth"] for slot in slots) <= -5681.5


def test_measured_ground_pack_percentages_round_trip_without_vertical_mirroring() -> None:
    min_x, min_y, width, height = (16679.0, -5042.0, 2550.0, 15000.0)
    slots = _pack_slots((min_x, min_y, width, height), 28)
    for slot in slots:
        restored_x = min_x + slot["left_pct"] / 100 * width
        restored_y = min_y + height - (
            slot["top_pct"] + slot["height_pct"]
        ) / 100 * height
        assert restored_x == pytest.approx(slot["x"], abs=0.02)
        assert restored_y == pytest.approx(slot["y"], abs=0.02)


def test_current_map_functional_location_is_mapped_without_becoming_pallet_slot() -> None:
    floor = WarehouseFloor(
        id=1,
        floor_code="3F",
        floor_name="三楼",
        floor_number=3,
        construction_status="enabled",
    )
    area = WarehouseArea(
        id=16,
        floor_id=1,
        area_code="FIN-LOOSE-001",
        area_name="送货剩余零散库存暂存区（无栈板）",
        construction_status="enabled",
    )
    location = WarehouseLocation(
        id=269,
        location_code="FIN-LOOSE-001",
        location_name="送货剩余零散库存暂存（无栈板）",
        warehouse_type="finished",
        warehouse_floor=3,
        area_code="FIN-LOOSE-001",
        storage_type="temporary_aisle",
        source_version="CURRENT_MAP",
        address_kind="functional",
        is_active=True,
        placement_status="placed",
    )
    readiness = published_measured_map_readiness(
        location,
        floor=floor,
        area=area,
        policy={
            "status": "published",
            "storage_layout": "functional",
            "published_map_revision": "e5f192ba605185db",
            "map_feature_id": "c399826c-0049-41bc-a687-04993152608f",
        },
        published_floor_identity={
            "revision": "e5f192ba605185db",
            "zones_by_id": {
                "c399826c-0049-41bc-a687-04993152608f": "FIN-LOOSE-001"
            },
        },
        has_geometry=True,
        ground_layout=None,
    )
    assert readiness.position_status == "mapped"
    assert readiness.issue is None
