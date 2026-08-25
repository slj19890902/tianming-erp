from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "admin"
    / "relocate_unmapped_finished_to_floor3.py"
)
SPEC = importlib.util.spec_from_file_location("p1_105_relocation", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _base_row(**overrides):
    row = {
        "location_id": 11,
        "location_is_active": 1,
        "placement_status": "placed",
        "warehouse_floor": 3,
        "location_area_code": "B2",
        "floor_id": 3,
        "floor_construction_status": "enabled",
        "area_id": 5,
        "formal_area_code": "B2",
        "area_construction_status": "enabled",
        "source_version": "V11",
        "policy_id": None,
        "policy_status": None,
        "policy_published_map_revision": None,
        "map_feature_id": None,
        "storage_type": "ground",
        "ground_plan_status": None,
        "ground_plan_published_map_revision": None,
        "ground_plan_area_id": None,
        "ground_slot_location_id": None,
        "geometry_location_id": 11,
    }
    row.update(overrides)
    return row


def _identities():
    return {
        "3F": {
            "revision": "revision-3f",
            "zones_by_id": {"zone-b2": "B2"},
            "zone_ids_by_area": {"B2": ("zone-b2",)},
        }
    }


def test_v11_requires_unique_current_zone_and_real_geometry() -> None:
    assert MODULE.classify_location(_base_row(), _identities()) == (
        "mapped",
        "mapped",
    )
    duplicated = _identities()
    duplicated["3F"]["zone_ids_by_area"]["B2"] = ("zone-b2", "zone-b2-copy")
    assert MODULE.classify_location(_base_row(), duplicated) == (
        "unlocated",
        "v11_area_not_unique",
    )
    assert MODULE.classify_location(
        _base_row(geometry_location_id=None), _identities()
    ) == ("unlocated", "geometry_missing")


def test_twin_ground_requires_current_policy_feature_and_ground_plan() -> None:
    row = _base_row(
        source_version="TWIN_V1",
        policy_id=7,
        policy_status="published",
        policy_published_map_revision="revision-3f",
        map_feature_id="zone-b2",
        ground_plan_status="published",
        ground_plan_published_map_revision="revision-3f",
        ground_plan_area_id=5,
        ground_slot_location_id=11,
    )
    assert MODULE.classify_location(row, _identities()) == ("mapped", "mapped")
    assert MODULE.classify_location(
        {**row, "ground_plan_published_map_revision": "old"}, _identities()
    ) == ("unlocated", "ground_layout_not_current")


def test_compatibility_key_separates_customer_code_and_specification() -> None:
    base = {
        "owner_customer_id": 1,
        "product_id": 2,
        "inventory_code_snapshot": "FG-100",
        "box_type_snapshot": "A1",
        "length_mm": 300,
        "width_mm": 200,
        "height_mm": 100,
        "material_code_snapshot": "K9",
        "flute_type_snapshot": "BC",
        "unit": "boxes",
    }
    key = MODULE.compatibility_key(base)
    assert MODULE.compatibility_key({**base, "owner_customer_id": 9}) != key
    assert MODULE.compatibility_key(
        {**base, "inventory_code_snapshot": "FG-101"}
    ) != key
    assert MODULE.compatibility_key({**base, "length_mm": 301}) != key


def test_target_gate_rejects_occupied_rack_or_non_finished_policy() -> None:
    base = {
        "warehouse_floor": 3,
        "storage_type": "ground",
        "warehouse_type": "finished",
        "occupied_pallet": 0,
        "occupied_inventory": 0,
        "occupied_ground": 0,
        "policy_id": None,
        "allowed_inventory_types_json": None,
        "policy_storage_layout": None,
    }
    assert MODULE._target_allowed(base)
    assert not MODULE._target_allowed({**base, "occupied_inventory": 1})
    assert not MODULE._target_allowed({**base, "storage_type": "rack"})
    assert not MODULE._target_allowed(
        {
            **base,
            "policy_id": 8,
            "allowed_inventory_types_json": '["semi_finished"]',
            "policy_storage_layout": "pallet_ground",
        }
    )


def test_plan_sha_ignores_generated_time_and_database_copy_path() -> None:
    base = {
        "task_id": MODULE.TASK_ID,
        "alembic_heads": ["head"],
        "runtime_map": {"sha256": "map", "revisions": {"3F": "rev"}},
        "operations": [{"client_item_id": "p1-105-001", "pallet_id": 1}],
    }
    first = {**base, "generated_at": "one", "database": {"path": "formal"}}
    second = {**base, "generated_at": "two", "database": {"path": "copy"}}
    assert MODULE._sha256_bytes(
        MODULE._canonical_json(MODULE._plan_contract(first))
    ) == MODULE._sha256_bytes(
        MODULE._canonical_json(MODULE._plan_contract(second))
    )
