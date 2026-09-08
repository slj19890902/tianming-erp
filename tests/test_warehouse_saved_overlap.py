from copy import deepcopy
from types import SimpleNamespace as NS

from app.services.warehouse_ground_map_application import _previously_verified, location_signature


def test_overlap_exception_requires_exact_previously_applied_location_snapshot():
    layout = NS(version=2, left_pct=10, top_pct=10, width_pct=40, height_pct=50, layout_kind="physical_pallet")
    location = NS(id=12, is_active=True, warehouse_floor=3, area_code="B1", address_area_id=5,
                  source_version="CURRENT_MAP", address_kind="ground_slot", ground_row_no=1,
                  slot_no=1, placement_status="placed", storage_type="ground", floor3_layout=layout)
    plan = NS(id=4, version=1, area_id=5)
    feature = {"id": "south-b1", "points": [[0, 0], [3000, 0], [3000, 4000], [0, 4000]]}
    previous = {"revision": "applied-map", "features": [feature]}
    receipt = {"plan_id": 4, "plan_version": 1, "area_id": 5, "map_feature_id": "south-b1",
               "map_revision": "applied-map", "locations": {"12": location_signature(location, layout)}}
    slots = [NS(location_id=12, location=location)]
    assert _previously_verified(plan, feature, previous, receipt, slots)
    for key in ["map_revision", "plan_id", "plan_version", "area_id", "map_feature_id"]:
        assert not _previously_verified(plan, feature, previous, {**receipt, key: "stale"}, slots)
    assert not _previously_verified(plan, feature, previous, {}, slots)
    changed = deepcopy(feature)
    changed["points"][0][0] = 1
    assert not _previously_verified(plan, changed, previous, receipt, slots)
    layout.version += 1
    assert not _previously_verified(plan, feature, previous, receipt, slots)
    layout.version -= 1
    layout.left_pct += 1
    assert not _previously_verified(plan, feature, previous, receipt, slots)
    layout.left_pct -= 1
    removed = NS(location_id=99, location=NS(is_active=False))
    plan.slots = [*slots, removed]
    prior_with_removed = {**receipt, "locations": {**receipt["locations"], "99": "old-signature"}}
    assert not _previously_verified(plan, feature, previous, prior_with_removed, slots)
    assert _previously_verified(plan, feature, previous, prior_with_removed, slots, {99})
    removed.location.is_active = True
    assert not _previously_verified(plan, feature, previous, prior_with_removed, slots, {99})
