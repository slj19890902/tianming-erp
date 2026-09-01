import pytest

from factory_twin.scripts.align_shared_freight_elevator import build_plan


def _feature(
    feature_id: str,
    code: str,
    points: list[list[float]],
    width_mm: float,
    *,
    status: str = "candidate",
) -> dict:
    return {
        "id": feature_id,
        "feature_code": code,
        "name": "货梯（跨楼层定位）",
        "feature_kind": "structure",
        "subtype": "freight_elevator",
        "points": points,
        "width_mm": width_mm,
        "elevation_mm": 0,
        "storage_height_mm": 3500,
        "color": "#475569",
        "status": status,
        "version": 1,
    }


def _layout(floor: str, feature: dict, extra: list[dict] | None = None) -> dict:
    return {
        "id": floor.lower(),
        "floor_code": floor,
        "features": [feature, *(extra or [])],
    }


def test_1f_uses_the_complete_authoritative_3f_lift_geometry() -> None:
    one_lift = _feature("one-lift", "LIFT-003", [[-3497, -2846], [-3497, -111]], 2000)
    three_lift = _feature("three-lift", "LIFT-002", [[-4614, -1605], [-2614, -1585]], 3000)
    plan = build_plan(_layout("1F", one_lift), _layout("3F", three_lift))

    assert plan["authority_floor"] == "3F"
    assert plan["payload"]["feature_code"] == "LIFT-002"
    assert plan["payload"]["points"] == three_lift["points"]
    assert plan["payload"]["width_mm"] == 3000
    assert plan["summary"]["center_distance_after_mm"] == 0
    assert plan["summary"]["three_floor_updates"] == 0
    assert plan["summary"]["equipment_moves"] == 0
    assert plan["summary"]["erp_writes"] == 0


def test_already_aligned_shared_lift_is_idempotent() -> None:
    one_lift = _feature("one-lift", "LIFT-002", [[-4614, -1605], [-2614, -1585]], 3000)
    three_lift = _feature("three-lift", "LIFT-002", [[-4614, -1605], [-2614, -1585]], 3000)
    plan = build_plan(_layout("1F", one_lift), _layout("3F", three_lift))

    assert plan["payload"] == {}
    assert plan["summary"]["needs_update"] is False
    assert plan["summary"]["center_distance_before_mm"] == 0


def test_4f_uses_the_same_authoritative_3f_lift_geometry() -> None:
    four_lift = _feature("four-lift", "LIFT-004", [[100, 200], [100, 2200]], 1800)
    three_lift = _feature("three-lift", "LIFT-002", [[-4614, -1605], [-2614, -1585]], 3000)

    plan = build_plan(_layout("4F", four_lift), _layout("3F", three_lift))

    assert plan["follower_floor"] == "4F"
    assert plan["authority_floor"] == "3F"
    assert plan["payload"]["feature_code"] == "LIFT-002"
    assert plan["payload"]["points"] == three_lift["points"]
    assert plan["payload"]["width_mm"] == three_lift["width_mm"]
    assert plan["summary"]["center_distance_after_mm"] == 0
    assert plan["summary"]["follower_other_feature_updates"] == 0
    assert plan["summary"]["three_floor_updates"] == 0


def test_only_1f_or_4f_can_follow_the_3f_authority() -> None:
    lift = _feature("lift", "LIFT-002", [[0, 0], [2000, 0]], 3000)

    with pytest.raises(RuntimeError, match="authority floor must be 3F"):
        build_plan(_layout("2F", lift), _layout("3F", lift))
    with pytest.raises(RuntimeError, match="authority floor must be 3F"):
        build_plan(_layout("4F", lift), _layout("1F", lift))


def test_confirmed_4f_lift_cannot_be_silently_renumbered() -> None:
    four_lift = _feature(
        "four-lift",
        "LIFT-004",
        [[0, 0], [0, 2000]],
        2000,
        status="confirmed",
    )
    three_lift = _feature("three-lift", "LIFT-002", [[10, 10], [2010, 10]], 3000)

    with pytest.raises(RuntimeError, match="confirmed 4F elevator"):
        build_plan(_layout("4F", four_lift), _layout("3F", three_lift))


def test_duplicate_target_code_on_1f_fails_closed() -> None:
    one_lift = _feature("one-lift", "LIFT-003", [[0, 0], [0, 2000]], 2000)
    three_lift = _feature("three-lift", "LIFT-002", [[10, 10], [2010, 10]], 3000)
    duplicate = {
        "id": "duplicate",
        "feature_code": "LIFT-002",
        "feature_kind": "zone",
        "subtype": "finished_storage",
    }

    with pytest.raises(RuntimeError, match="already contains another LIFT-002"):
        build_plan(_layout("1F", one_lift, [duplicate]), _layout("3F", three_lift))
