from copy import deepcopy

from factory_twin.scripts.refine_floor3_zone_blocks import (
    AISLE_TARGETS,
    LEFT_DELETE_CODES,
    LEFT_ZONE_TARGETS,
    build_summary,
    right_zone_target,
)


def _feature(code: str, *, name: str = "旧名称", points=None, **extra):
    return {
        "feature_code": code,
        "name": name,
        "feature_kind": "zone",
        "subtype": "finished_storage",
        "points": points or [[0, 0], [100, 0], [100, 100], [0, 100]],
        **extra,
    }


def test_left_blocks_absorb_redundant_fragments_without_crossing_named_aisles() -> None:
    assert LEFT_DELETE_CODES == {
        "ZONE-3F-SEMI-004", "ZONE-3F-SEMI-005",
        "ZONE-3F-SEMI-007", "ZONE-3F-SEMI-009",
    }
    west_mid = LEFT_ZONE_TARGETS["ZONE-3F-SEMI-006"]["points"]
    east_mid = LEFT_ZONE_TARGETS["ZONE-3F-SEMI-010"]["points"]
    assert max(point[0] for point in west_mid) < -12325
    assert min(point[0] for point in east_mid) > -12325
    assert min(point[0] for point in east_mid) > -12325
    assert max(point[0] for point in east_mid) < -5000


def test_right_zones_align_to_the_1500_mm_horizontal_divider() -> None:
    upper = _feature("ZONE-3F-ERP-A1", points=[[30000, -3300], [33000, -3300], [33000, 4000], [30000, 4000]])
    lower = _feature("ZONE-3F-ERP-A2", points=[[30000, -20000], [33000, -20000], [33000, -5700], [30000, -5700]])
    upper_target = right_zone_target(upper)
    lower_target = right_zone_target(lower)
    assert min(point[1] for point in upper_target["points"]) == -4010
    assert max(point[1] for point in lower_target["points"]) == -5510
    assert -4010 - (-5510) == 1500
    assert min(point[0] for point in upper_target["points"]) == 30000
    assert max(point[0] for point in lower_target["points"]) == 33000


def test_right_divider_is_continuous_and_uses_the_owner_confirmed_width() -> None:
    divider = AISLE_TARGETS["AISLE-3F-PED-022"]
    assert divider["width_mm"] == 1500
    assert divider["points"] == [[9500, -4760], [34000, -4760]]
    assert divider["no_stacking"] is True
    assert "主分割通道" in divider["name"]


def test_summary_becomes_zero_after_targets_are_present() -> None:
    features = []
    for code, changes in LEFT_ZONE_TARGETS.items():
        features.append(_feature(code, **deepcopy(changes)))
    for code, changes in AISLE_TARGETS.items():
        features.append(_feature(code, feature_kind="aisle", **deepcopy(changes)))
    originals = (
        _feature("ZONE-3F-ERP-A1", points=[[0, -3300], [100, -3300], [100, 100], [0, 100]]),
        _feature("ZONE-3F-ERP-A2", points=[[0, -20000], [100, -20000], [100, -5700], [0, -5700]]),
    )
    for original in originals:
        target = right_zone_target(original)
        features.append({**original, **target})
    assert build_summary({"features": features}) == {
        "left_fragment_deletes": 0,
        "left_block_updates": 0,
        "right_zone_updates": 0,
        "aisle_updates": 0,
    }
