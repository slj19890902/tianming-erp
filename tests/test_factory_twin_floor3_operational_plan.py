from factory_twin.scripts.finalize_floor3_operational_plan import (
    _door_clearance_points,
    _rack_specs,
    build_summary,
)


def test_confirmed_racks_include_ground_plus_two_upper_levels() -> None:
    racks = _rack_specs()
    assert len(racks) == 9
    assert all(rack["levels"] == 3 and rack["z_mm"] == 0 for rack in racks)
    assert [rack["depth_mm"] for rack in racks if rack["rack_code"].startswith("RACK-3F-F1-")] == [1100, 1100]
    d2 = {rack["rack_code"]: rack for rack in racks if "D2" in rack["rack_code"]}
    assert d2["RACK-3F-D2-SOUTH-001"]["depth_mm"] == 1500
    assert d2["RACK-3F-D2-WEST-001"]["depth_mm"] == 1200
    assert d2["RACK-3F-D2-EAST-001"]["depth_mm"] == 1200


def test_door_clearance_uses_real_opening_direction() -> None:
    structure = {"geometry": {"points": [[0, 0], [2000, 0]]}}
    assert _door_clearance_points(structure, 1500) == [[-250, 750], [2250, 750], [2250, -750], [-250, -750]]


def test_summary_counts_only_unapplied_owner_confirmed_changes() -> None:
    layout = {
        "racks": [],
        "features": [
            {"feature_code": "ZONE-3F-FIN-001", "feature_kind": "zone", "subtype": "unassigned_storage"},
            {"feature_code": "ZONE-3F-ERP-A1", "feature_kind": "zone", "subtype": "floor_marked_storage"},
            {"feature_code": "AISLE-3F-PED-001", "feature_kind": "aisle", "subtype": "pedestrian"},
        ],
    }
    assert build_summary(layout) == {
        "release_zones": 1, "finished_storage_updates": 1, "shared_aisle_updates": 1,
        "rack_creates": 9, "no_go_creates": 8,
    }
