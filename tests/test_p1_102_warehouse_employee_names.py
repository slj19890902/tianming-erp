from __future__ import annotations

import pytest

from app.models.warehouse_inventory import (
    WarehouseArea,
    WarehouseFloor,
    WarehouseLocation,
)
from app.services.warehouse_location_address import (
    LEGACY_V11_RIGHT_AREA_CODES,
    employee_area_name,
    employee_location_name,
    location_address_payload,
    published_measured_map_readiness,
)


def _floor(floor_number: int) -> WarehouseFloor:
    return WarehouseFloor(
        id=floor_number,
        floor_code=f"{floor_number}F",
        floor_name={1: "一楼", 3: "三楼"}.get(floor_number, f"{floor_number}楼"),
        floor_number=floor_number,
        construction_status="enabled",
    )


def _area(
    floor: WarehouseFloor,
    *,
    area_code: str,
    area_name: str,
) -> WarehouseArea:
    return WarehouseArea(
        id=100 + floor.floor_number,
        floor_id=floor.id,
        area_code=area_code,
        area_name=area_name,
        construction_status="enabled",
    )


def _location(
    *,
    code: str,
    area_code: str,
    storage_type: str,
    level_no: int | None = None,
    side_code: str | None = None,
) -> WarehouseLocation:
    return WarehouseLocation(
        id=301,
        location_code=code,
        location_name=code,
        warehouse_type="finished",
        warehouse_floor=3,
        area_code=area_code,
        storage_type=storage_type,
        level_no=level_no,
        side_code=side_code,
        source_version="V11",
        is_active=True,
        placement_status="placed",
    )


def test_every_legacy_v11_right_area_uses_one_floor_scoped_default() -> None:
    floor = _floor(3)

    assert len(LEGACY_V11_RIGHT_AREA_CODES) == 22
    for code in LEGACY_V11_RIGHT_AREA_CODES:
        area = _area(floor, area_code=code, area_name=f"{code} 区")
        assert employee_area_name(area, floor_number=3) == f"右区{code}"


def test_custom_right_area_keeps_description_and_other_areas_are_not_rewritten() -> None:
    floor3 = _floor(3)
    floor1 = _floor(1)

    custom = _area(floor3, area_code="A1", area_name="三楼北侧成品区")
    left = _area(floor3, area_code="RAW-001", area_name="左区L3 原料区（上段）")
    floor1_a1 = _area(floor1, area_code="A1", area_name="A1 区")

    assert employee_area_name(custom, floor_number=3) == "右区A1·北侧成品区"
    assert employee_area_name(left, floor_number=3) == "左区L3 原料区（上段）"
    assert employee_area_name(floor1_a1, floor_number=1) == "A1 区"


def test_custom_area_name_with_floor_prefix_is_not_duplicated_in_location() -> None:
    floor = _floor(3)
    area = _area(floor, area_code="A1", area_name="三楼北侧成品区")
    location = _location(
        code="A1-L01",
        area_code="A1",
        storage_type="ground",
        side_code="L",
    )

    assert employee_location_name(location, area=area, floor=floor) == (
        "三楼 右区A1·北侧成品区·左侧第1位"
    )


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        (
            _location(
                code="A1-L01",
                area_code="A1",
                storage_type="ground",
                side_code="L",
            ),
            "三楼 右区A1·左侧第1位",
        ),
        (
            _location(
                code="A1-R02",
                area_code="A1",
                storage_type="ground",
                side_code="R",
            ),
            "三楼 右区A1·右侧第2位",
        ),
        (
            _location(
                code="F12-P01",
                area_code="F12",
                storage_type="temporary_aisle",
                side_code="P",
            ),
            "三楼 右区F12·临放第1位",
        ),
        (
            _location(
                code="F2-S1-L01",
                area_code="F2",
                storage_type="rack",
                level_no=1,
                side_code="L",
            ),
            "三楼 右区F2·1层·左侧第1格",
        ),
    ],
)
def test_v11_location_and_payload_use_the_same_employee_area_name(
    location: WarehouseLocation,
    expected: str,
) -> None:
    floor = _floor(3)
    area = _area(
        floor,
        area_code=location.area_code,
        area_name=f"{location.area_code} 区",
    )

    payload = location_address_payload(
        location,
        area=area,
        floor=floor,
        position_status="mapped",
    )

    assert employee_location_name(location, area=area, floor=floor) == expected
    assert payload["employee_location_name"] == expected
    assert payload["current_address_name"] == expected
    assert payload["area_name"] == f"右区{location.area_code}"
    assert payload["area_master_name"] == f"{location.area_code} 区"
    assert "位置名称待完善" not in expected


def _published_identity(*, feature_id: str, area_code: str) -> dict:
    return {
        "revision": "runtime-rev-1",
        "feature_ids": frozenset({feature_id}),
        "erp_area_codes": frozenset({area_code}),
        "zones_by_id": {feature_id: area_code},
        "zone_ids_by_area": {area_code: (feature_id,)},
    }


def _published_policy(*, feature_id: str, revision: str = "runtime-rev-1") -> dict:
    return {
        "status": "published",
        "published_map_revision": revision,
        "map_feature_id": feature_id,
    }


def test_v11_location_accepts_a_strict_current_published_area_policy() -> None:
    floor = _floor(3)
    area = _area(floor, area_code="D2", area_name="D2 区")
    location = _location(
        code="D2-L01",
        area_code="D2",
        storage_type="ground",
        side_code="L",
    )

    readiness = published_measured_map_readiness(
        location,
        floor=floor,
        area=area,
        policy=_published_policy(feature_id="zone-d2"),
        published_floor_identity=_published_identity(
            feature_id="zone-d2",
            area_code="D2",
        ),
        has_geometry=True,
        ground_layout=None,
    )

    assert readiness.position_status == "mapped"
    assert readiness.issue is None
    assert readiness.map_feature_id == "zone-d2"
    assert readiness.published_map_revision == "runtime-rev-1"


@pytest.mark.parametrize(
    ("policy", "identity"),
    [
        (
            {
                "status": "draft",
                "published_map_revision": None,
                "map_feature_id": "zone-d2",
            },
            _published_identity(feature_id="zone-d2", area_code="D2"),
        ),
        (
            _published_policy(feature_id="zone-d2", revision="stale-rev"),
            _published_identity(feature_id="zone-d2", area_code="D2"),
        ),
        (
            _published_policy(feature_id="zone-d2"),
            _published_identity(feature_id="zone-d2", area_code="E4"),
        ),
    ],
)
def test_v11_policy_compatibility_fails_closed_for_noncurrent_bindings(
    policy: dict,
    identity: dict,
) -> None:
    floor = _floor(3)
    area = _area(floor, area_code="D2", area_name="D2 区")
    location = _location(
        code="D2-L01",
        area_code="D2",
        storage_type="ground",
        side_code="L",
    )

    readiness = published_measured_map_readiness(
        location,
        floor=floor,
        area=area,
        policy=policy,
        published_floor_identity=identity,
        has_geometry=True,
        ground_layout=None,
    )

    assert readiness.position_status == "area_only"
    assert readiness.issue
    assert readiness.map_feature_id is None


def test_twin_ground_location_still_requires_a_current_published_ground_slot() -> None:
    floor = _floor(3)
    area = _area(floor, area_code="D2", area_name="D2 区")
    location = _location(
        code="TWIN-D2-001",
        area_code="D2",
        storage_type="ground",
        side_code="L",
    )
    location.source_version = "TWIN_V1"
    policy = _published_policy(feature_id="zone-d2")
    identity = _published_identity(feature_id="zone-d2", area_code="D2")

    blocked = published_measured_map_readiness(
        location,
        floor=floor,
        area=area,
        policy=policy,
        published_floor_identity=identity,
        has_geometry=True,
        ground_layout=None,
    )
    mapped = published_measured_map_readiness(
        location,
        floor=floor,
        area=area,
        policy=policy,
        published_floor_identity=identity,
        has_geometry=True,
        ground_layout={
            "status": "published",
            "published_map_revision": "runtime-rev-1",
            "area_id": area.id,
            "location_id": location.id,
        },
    )

    assert blocked.position_status == "area_only"
    assert "已发布排位" in str(blocked.issue)
    assert mapped.position_status == "mapped"
