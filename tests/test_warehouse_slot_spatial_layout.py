from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.api import warehouse as warehouse_api
from app.api.auth import router as auth_router
from app.api.deps import get_db
from app.api.warehouse import router as warehouse_router
from app.core.database import create_sqlite_engine
from app.core.security import hash_password
from app.models import Base
from app.models.audit import OperationLog
from app.models.user import User
from app.models.warehouse_inventory import (
    Floor3LocationLayout,
    InventoryLot,
    InventoryPallet,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseLocation,
)
from app.services.warehouse_area_activation import (
    WarehouseAreaActivationError,
    adjust_area_location_count,
)
from app.services.floor3_locations import (
    adjust_area_location_count as adjust_v11_area_location_count,
)
from app.services.location_candidates import claim_active_placed_location
from app.services.warehouse_floor1_candidate_planner import (
    Floor1CandidatePlanningError,
    confirmed_capacity_slots_for_zone,
    validate_capacity_layout_slots_for_zone,
)


def _layout(*, width: float = 10_000, height: float = 10_000) -> dict:
    return {
        "floor_code": "1F",
        "revision": "slot-layout-test",
        "bounds_mm": {
            "min_x": 0,
            "min_y": 0,
            "max_x": width,
            "max_y": height,
        },
        "structures": [
            {
                "id": "COLUMN-CENTER",
                "kind": "column",
                "geometry": {
                    "type": "circle",
                    "x_mm": width / 2,
                    "y_mm": height / 2,
                    "radius_mm": 800,
                },
            }
        ],
        "placements": [],
        "racks": [],
        "features": [
            {
                "id": "ZONE-TEST",
                "feature_code": "ZONE-1F-TEST",
                "feature_kind": "zone",
                "points": [[0, 0], [width, 0], [width, height], [0, height]],
            }
        ],
    }


def _centers(slots: list[dict]) -> list[tuple[float, float]]:
    return [
        (
            float(slot["x_mm"]) + float(slot["width_mm"]) / 2,
            float(slot["y_mm"]) + float(slot["depth_mm"]) / 2,
        )
        for slot in slots
    ]


@pytest.fixture()
def spatial_contract_app(tmp_path, monkeypatch):
    engine = create_sqlite_engine(tmp_path / "slot-spatial-contract.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    measured_layout = _layout()
    measured_layout["floor_code"] = "3F"
    measured_layout["features"].append(
        {
            **measured_layout["features"][0],
            "id": "ZONE-ZERO",
            "feature_code": "ZONE-1F-ZERO",
        }
    )
    monkeypatch.setattr(
        warehouse_api,
        "load_warehouse_twin_floor",
        lambda _floor_code: measured_layout,
    )

    with factory() as db:
        admin = User(
            username="slot-contract-admin",
            password_hash=hash_password("123456"),
            role="admin",
            real_name="Slot contract admin",
            is_active=True,
            must_change_password=False,
            customer_access_mode="all",
            ui_mode="standard",
        )
        workshop = User(
            username="slot-contract-workshop",
            password_hash=hash_password("123456"),
            role="workshop",
            real_name="Slot contract workshop user",
            is_active=True,
            must_change_password=False,
            customer_access_mode="all",
            ui_mode="standard",
        )
        floor = WarehouseFloor(
            floor_code="3F",
            floor_name="Third floor",
            floor_number=3,
            construction_status="enabled",
            planning_reference_pallet_capacity=0,
        )
        db.add_all([admin, workshop, floor])
        db.flush()

        zero_area = WarehouseArea(
            floor_id=floor.id,
            area_code="ZERO",
            area_name="Zero location area",
            planned_location_count=0,
            planned_pallet_capacity=0,
            construction_status="enabled",
            capacity_review_status="pending",
            capacity_eligible=False,
        )
        published_area = WarehouseArea(
            floor_id=floor.id,
            area_code="PUB",
            area_name="Published location area",
            planned_location_count=2,
            planned_pallet_capacity=2,
            construction_status="enabled",
            capacity_review_status="pending",
            capacity_eligible=False,
        )
        db.add_all([zero_area, published_area])
        db.flush()
        for area, feature_id in (
            (zero_area, "ZONE-ZERO"),
            (published_area, "ZONE-TEST"),
        ):
            db.add(
                WarehouseAreaStoragePolicy(
                    area_id=area.id,
                    map_feature_id=feature_id,
                    allowed_inventory_types_json='["finished"]',
                    storage_layout="pallet_ground",
                    status="published",
                    published_map_revision="slot-layout-test",
                    version=1,
                    updated_by=admin.id,
                )
            )

        location_specs = (
            ("PUB-L001", True, 10, 10, 1),
            ("PUB-L002", True, 70, 70, 2),
            ("PUB-COLUMN", False, 45, 45, 3),
            ("PUB-OVERLAP", False, 12, 12, 4),
        )
        location_ids: dict[str, int] = {}
        for code, is_active, left, top, sort_order in location_specs:
            location = WarehouseLocation(
                location_code=code,
                location_name=code,
                warehouse_type="finished",
                is_active=is_active,
                warehouse_floor=3,
                area_code="PUB",
                storage_type="ground",
                sort_order=sort_order,
                is_temporary=False,
                source_version="TWIN_V1",
                placement_status="placed",
            )
            location.floor3_layout = Floor3LocationLayout(
                left_pct=Decimal(left),
                top_pct=Decimal(top),
                width_pct=Decimal(10),
                height_pct=Decimal(10),
                z_index=0,
                version=1,
                source_type="seeded",
                layout_kind="physical_pallet",
                created_by=admin.id,
                updated_by=admin.id,
            )
            db.add(location)
            db.flush()
            location_ids[code] = location.id
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(warehouse_router, prefix="/api/warehouse")

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, factory, location_ids
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _contract_login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "123456"},
    )
    assert response.status_code == 200, response.text


def test_mapped_empty_destination_requires_the_selected_layout_version(
    spatial_contract_app,
) -> None:
    _app, factory, location_ids = spatial_contract_app
    location_id = location_ids["PUB-L001"]
    with factory() as db:
        assert claim_active_placed_location(db, location_id) is False
        db.rollback()
    with factory() as db:
        assert (
            claim_active_placed_location(
                db,
                location_id,
                expected_layout_version=2,
            )
            is False
        )
        db.rollback()
    with factory() as db:
        assert (
            claim_active_placed_location(
                db,
                location_id,
                expected_layout_version=1,
            )
            is True
        )
        db.rollback()


def _area_contract_state(factory, area_code: str) -> tuple:
    with factory() as db:
        area = db.scalar(
            select(WarehouseArea).where(WarehouseArea.area_code == area_code)
        )
        assert area is not None
        policy = db.scalar(
            select(WarehouseAreaStoragePolicy).where(
                WarehouseAreaStoragePolicy.area_id == area.id
            )
        )
        assert policy is not None
        rows = list(
            db.scalars(
                select(WarehouseLocation)
                .where(WarehouseLocation.area_code == area_code)
                .order_by(WarehouseLocation.id)
            ).all()
        )
        layouts = {
            row.location_code: (
                row.is_active,
                row.placement_status,
                row.floor3_layout.left_pct if row.floor3_layout else None,
                row.floor3_layout.top_pct if row.floor3_layout else None,
                row.floor3_layout.width_pct if row.floor3_layout else None,
                row.floor3_layout.height_pct if row.floor3_layout else None,
                row.floor3_layout.version if row.floor3_layout else None,
                row.floor3_layout.source_type if row.floor3_layout else None,
            )
            for row in rows
        }
        audit_rows = list(
            db.execute(
                select(OperationLog.id, OperationLog.details)
                .where(OperationLog.entity_type == "floor3_location_layout")
                .order_by(OperationLog.id)
            ).all()
        )
        return (
            area.planned_location_count,
            area.construction_status,
            policy.version,
            policy.published_map_revision,
            layouts,
            audit_rows,
        )


def test_zero_location_count_rejects_replayed_empty_snapshot_atomically(
    spatial_contract_app,
) -> None:
    app, factory, _location_ids = spatial_contract_app
    payload = {
        "target_count": 1,
        "confirmed": True,
        "expected_map_revision": "slot-layout-test",
        "expected_policy_version": 1,
        "expected_layout_versions": {},
    }

    with TestClient(app) as client:
        _contract_login(client, "slot-contract-admin")
        initial = _area_contract_state(factory, "ZERO")
        assert initial[0] == 0
        assert initial[2] == 1
        assert initial[4] == {}
        assert initial[5] == []

        first = client.post(
            "/api/warehouse/spatial-layout/floors/3F/areas/ZERO/location-count",
            json=payload,
        )
        assert first.status_code == 200, first.text
        assert first.json()["active_count"] == 1
        assert first.json()["policy_version"] == 2
        committed = _area_contract_state(factory, "ZERO")
        assert committed[0] == 1
        assert committed[2] == 2
        assert len(committed[4]) == 1
        assert committed[5]

        replay = client.post(
            "/api/warehouse/spatial-layout/floors/3F/areas/ZERO/location-count",
            json=payload,
        )
        assert replay.status_code == 409, replay.text
        assert _area_contract_state(factory, "ZERO") == committed


@pytest.mark.parametrize(
    ("expected_map_revision", "expected_policy_version"),
    (("stale-map-revision", 1), ("slot-layout-test", 2)),
    ids=("stale-map", "stale-policy"),
)
def test_location_count_rejects_stale_map_or_policy_without_side_effects(
    spatial_contract_app,
    expected_map_revision: str,
    expected_policy_version: int,
) -> None:
    app, factory, _location_ids = spatial_contract_app
    before = _area_contract_state(factory, "ZERO")

    with TestClient(app) as client:
        _contract_login(client, "slot-contract-admin")
        response = client.post(
            "/api/warehouse/spatial-layout/floors/3F/areas/ZERO/location-count",
            json={
                "target_count": 1,
                "confirmed": True,
                "expected_map_revision": expected_map_revision,
                "expected_policy_version": expected_policy_version,
                "expected_layout_versions": {},
            },
        )

    assert response.status_code == 409, response.text
    assert _area_contract_state(factory, "ZERO") == before


def test_manual_batch_conflict_rolls_back_every_slot_policy_and_audit(
    spatial_contract_app,
) -> None:
    app, factory, location_ids = spatial_contract_app
    before = _area_contract_state(factory, "PUB")

    with TestClient(app) as client:
        _contract_login(client, "slot-contract-admin")
        response = client.patch(
            "/api/warehouse/spatial-layout/floors/3F/areas/PUB",
            json={
                "expected_map_revision": "slot-layout-test",
                "expected_policy_version": 1,
                "slots": [
                    {
                        "location_id": location_ids["PUB-L001"],
                        "expected_version": 1,
                        "left_pct": 25,
                        "top_pct": 10,
                        "width_pct": 10,
                        "height_pct": 10,
                        "z_index": 0,
                    },
                    {
                        "location_id": location_ids["PUB-L002"],
                        "expected_version": 99,
                        "left_pct": 70,
                        "top_pct": 70,
                        "width_pct": 10,
                        "height_pct": 10,
                        "z_index": 0,
                    },
                ],
            },
        )

    assert response.status_code == 409, response.text
    assert _area_contract_state(factory, "PUB") == before


def test_location_count_and_manual_layout_are_admin_only(
    spatial_contract_app,
) -> None:
    app, factory, location_ids = spatial_contract_app
    zero_before = _area_contract_state(factory, "ZERO")
    published_before = _area_contract_state(factory, "PUB")

    with TestClient(app) as client:
        _contract_login(client, "slot-contract-workshop")
        count_response = client.post(
            "/api/warehouse/spatial-layout/floors/3F/areas/ZERO/location-count",
            json={
                "target_count": 1,
                "confirmed": True,
                "expected_map_revision": "slot-layout-test",
                "expected_policy_version": 1,
                "expected_layout_versions": {},
            },
        )
        manual_response = client.patch(
            "/api/warehouse/spatial-layout/floors/3F/areas/PUB",
            json={
                "expected_map_revision": "slot-layout-test",
                "expected_policy_version": 1,
                "slots": [
                    {
                        "location_id": location_ids["PUB-L001"],
                        "expected_version": 1,
                        "left_pct": 25,
                        "top_pct": 10,
                        "width_pct": 10,
                        "height_pct": 10,
                        "z_index": 0,
                    }
                ],
            },
        )

    assert count_response.status_code == 403, count_response.text
    assert manual_response.status_code == 403, manual_response.text
    assert _area_contract_state(factory, "ZERO") == zero_before
    assert _area_contract_state(factory, "PUB") == published_before


@pytest.mark.parametrize("location_code", ("PUB-COLUMN", "PUB-OVERLAP"))
def test_published_area_rejects_enabling_spatially_invalid_location_atomically(
    spatial_contract_app,
    location_code: str,
) -> None:
    app, factory, location_ids = spatial_contract_app
    before = _area_contract_state(factory, "PUB")

    with TestClient(app) as client:
        _contract_login(client, "slot-contract-admin")
        response = client.post(
            f"/api/warehouse/spatial-layout/locations/{location_ids[location_code]}/enable",
            json={
                "expected_version": 1,
                "expected_map_revision": "slot-layout-test",
                "expected_policy_version": 1,
            },
        )

    assert response.status_code == 409, response.text
    assert _area_contract_state(factory, "PUB") == before


def test_zero_location_snapshot_is_an_explicit_empty_cas_map() -> None:
    payload = warehouse_api.Floor3AreaLocationCountPayload(
        target_count=3,
        confirmed=True,
        expected_layout_versions={},
    )

    assert payload.expected_layout_versions == {}


def test_one_step_operation_key_reserves_child_suffix_space() -> None:
    common = {
        "expected_revision": "draft-revision",
        "expected_published_revision": "published-revision",
        "expected_version": 1,
        "primary_inventory_type": "finished",
        "storage_layout": "pallet_ground",
        "max_pallet_capacity": 1,
        "erp_area_code": "TEST-001",
        "confirmed": True,
    }
    accepted = warehouse_api.TwinZoneConfirmAreaPayload(
        **common,
        operation_key="k" * 112,
    )
    assert len(f"{accepted.operation_key}-publish") == 120
    with pytest.raises(ValidationError):
        warehouse_api.TwinZoneConfirmAreaPayload(
            **common,
            operation_key="k" * 113,
        )


def test_v11_count_can_remove_seeded_location_with_zero_balance_history(
    tmp_path,
) -> None:
    engine = create_sqlite_engine(tmp_path / "v11-zero-balance-count.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        operator = User(
            username="v11-zero-balance-admin",
            password_hash=hash_password("123456"),
            role="admin",
            real_name="V11 zero balance admin",
            is_active=True,
            must_change_password=False,
            customer_access_mode="all",
            ui_mode="standard",
        )
        db.add(operator)
        db.flush()
        location = WarehouseLocation(
            location_code="A1-ZERO-BALANCE",
            location_name="A1 zero balance",
            warehouse_type="finished",
            is_active=True,
            warehouse_floor=3,
            area_code="A1",
            storage_type="ground",
            sort_order=1,
            is_temporary=False,
            source_version="V11",
            placement_status="placed",
        )
        location.floor3_layout = Floor3LocationLayout(
            left_pct=Decimal("10"),
            top_pct=Decimal("10"),
            width_pct=Decimal("12"),
            height_pct=Decimal("10"),
            version=1,
            source_type="seeded",
            layout_kind="physical_pallet",
            created_by=operator.id,
            updated_by=operator.id,
        )
        db.add(location)
        db.flush()
        db.add(
            InventoryLot(
                lot_number="V11-ZERO-HISTORY",
                inventory_type="finished",
                warehouse_location_id=location.id,
                quantity_available=0,
                quantity_reserved=0,
                quantity_consumed=5,
                quantity_damaged=0,
                quantity_scrapped=0,
                unit="boxes",
                status="active",
                source_type="manual",
                stock_date=date(2026, 8, 14),
                stock_date_accuracy="exact",
                last_movement_at=datetime(2026, 8, 14, 12, 0),
                version=1,
                created_by=operator.id,
            )
        )
        db.commit()
        location_id = location.id
        operator_id = operator.id

    with factory() as db:
        result = adjust_v11_area_location_count(
            db,
            area_code="A1",
            target_count=0,
            operator_id=operator_id,
        )
        db.commit()
        assert result.active_count == 0
        assert [row.id for row in result.disabled] == [location_id]
        assert db.get(WarehouseLocation, location_id).is_active is False
    engine.dispose()


def test_standard_slots_are_stable_evenly_spread_and_avoid_columns() -> None:
    layout = _layout()
    first = confirmed_capacity_slots_for_zone(
        layout, feature_id="ZONE-TEST", target_count=20
    )
    second = confirmed_capacity_slots_for_zone(
        layout, feature_id="ZONE-TEST", target_count=20
    )

    assert first == second
    centers = _centers(first)
    assert max(x for x, _y in centers) - min(x for x, _y in centers) >= 8_000
    assert max(y for _x, y in centers) - min(y for _x, y in centers) >= 8_000
    validate_capacity_layout_slots_for_zone(
        layout, feature_id="ZONE-TEST", slots=first
    )


def test_percent_round_trip_does_not_reject_its_own_edge_aligned_slots() -> None:
    layout = _layout(width=2_400, height=3_000)
    layout["structures"] = []
    slots = confirmed_capacity_slots_for_zone(
        layout, feature_id="ZONE-TEST", target_count=2
    )

    assert len(slots) == 2
    validate_capacity_layout_slots_for_zone(
        layout, feature_id="ZONE-TEST", slots=slots
    )


def test_percent_round_trip_tolerance_scales_for_a_thirty_metre_zone() -> None:
    layout = _layout(width=1_200, height=29_999.76)
    layout["structures"] = []
    slots = confirmed_capacity_slots_for_zone(
        layout, feature_id="ZONE-TEST", target_count=29
    )

    assert len(slots) == 29
    validate_capacity_layout_slots_for_zone(
        layout, feature_id="ZONE-TEST", slots=slots
    )


def test_logical_and_unknown_anchors_use_the_same_400mm_collision_footprint() -> None:
    layout = _layout()
    layout["structures"] = [
        {
            "id": "COLUMN-NEAR-ANCHOR",
            "kind": "column",
            "geometry": {
                "type": "polyline",
                "points": [
                    [5_250, 4_900],
                    [5_450, 4_900],
                    [5_450, 5_100],
                    [5_250, 5_100],
                ],
            },
        }
    ]
    base = {
        "location_id": 1,
        "left_pct": 47,
        "top_pct": 47,
        "width_pct": 6,
        "height_pct": 6,
    }

    validate_capacity_layout_slots_for_zone(
        layout,
        feature_id="ZONE-TEST",
        slots=[{**base, "layout_kind": "logical_anchor"}],
    )
    validate_capacity_layout_slots_for_zone(
        layout,
        feature_id="ZONE-TEST",
        slots=[{**base, "layout_kind": "unknown"}],
    )
    with pytest.raises(Floor1CandidatePlanningError, match="柱子"):
        validate_capacity_layout_slots_for_zone(
            layout,
            feature_id="ZONE-TEST",
            slots=[{**base, "layout_kind": "physical_pallet"}],
        )


@pytest.mark.parametrize("target_count", [91, 500])
def test_logical_capacity_anchors_use_the_whole_area_and_avoid_columns(
    target_count: int,
) -> None:
    layout = _layout()
    slots = confirmed_capacity_slots_for_zone(
        layout, feature_id="ZONE-TEST", target_count=target_count
    )

    assert len(slots) == target_count
    assert all(slot.get("capacity_confirmed") is True for slot in slots)
    centers = _centers(slots)
    assert max(x for x, _y in centers) - min(x for x, _y in centers) >= 9_000
    assert max(y for _x, y in centers) - min(y for _x, y in centers) >= 9_000
    validate_capacity_layout_slots_for_zone(
        layout, feature_id="ZONE-TEST", slots=slots
    )


def test_reserved_fixed_slot_is_not_moved_or_overlapped() -> None:
    layout = _layout()
    fixed = {
        "location_id": 1,
        "left_pct": 0,
        "top_pct": 0,
        "width_pct": 12,
        "height_pct": 10,
        "z_index": 0,
        "expected_version": 2,
    }
    generated = confirmed_capacity_slots_for_zone(
        layout,
        feature_id="ZONE-TEST",
        target_count=12,
        reserved_slots=[fixed],
    )

    assert len(generated) == 12
    validate_capacity_layout_slots_for_zone(
        layout,
        feature_id="ZONE-TEST",
        slots=[fixed, *generated],
    )


def test_concave_zone_never_accepts_a_rectangle_across_the_notch() -> None:
    layout = _layout()
    layout["structures"] = []
    layout["features"][0]["points"] = [
        [0, 0],
        [10_000, 0],
        [10_000, 10_000],
        [7_000, 10_000],
        [7_000, 3_000],
        [3_000, 3_000],
        [3_000, 10_000],
        [0, 10_000],
    ]
    generated = confirmed_capacity_slots_for_zone(
        layout, feature_id="ZONE-TEST", target_count=30
    )
    validate_capacity_layout_slots_for_zone(
        layout, feature_id="ZONE-TEST", slots=generated
    )

    bridge_across_notch = {
        "location_id": 999,
        "left_pct": 25,
        "top_pct": 60,
        "width_pct": 50,
        "height_pct": 20,
    }
    with pytest.raises(Floor1CandidatePlanningError, match="区域边界"):
        validate_capacity_layout_slots_for_zone(
            layout,
            feature_id="ZONE-TEST",
            slots=[bridge_across_notch],
        )


@pytest.mark.parametrize(
    "slots, expected_message",
    [
        (
            [
                {
                    "location_id": 1,
                    "left_pct": 95,
                    "top_pct": 20,
                    "width_pct": 10,
                    "height_pct": 10,
                    "layout_kind": "physical_pallet",
                }
            ],
            "区域边界",
        ),
        (
            [
                {
                    "location_id": 1,
                    "left_pct": 45,
                    "top_pct": 45,
                    "width_pct": 10,
                    "height_pct": 10,
                    "layout_kind": "physical_pallet",
                }
            ],
            "柱子",
        ),
        (
            [
                {
                    "location_id": 1,
                    "left_pct": 10,
                    "top_pct": 10,
                    "width_pct": 10,
                    "height_pct": 10,
                    "layout_kind": "physical_pallet",
                },
                {
                    "location_id": 2,
                    "left_pct": 15,
                    "top_pct": 15,
                    "width_pct": 10,
                    "height_pct": 10,
                    "layout_kind": "physical_pallet",
                },
            ],
            "重叠",
        ),
    ],
)
def test_server_side_validation_rejects_invalid_manual_layouts(
    slots: list[dict], expected_message: str
) -> None:
    with pytest.raises(Floor1CandidatePlanningError, match=expected_message):
        validate_capacity_layout_slots_for_zone(
            _layout(), feature_id="ZONE-TEST", slots=slots
        )


def test_reflow_preserves_occupied_and_manual_fixed_locations(
    tmp_path, monkeypatch
) -> None:
    engine = create_sqlite_engine(tmp_path / "slot-reflow.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    layout = _layout()
    layout["floor_code"] = "3F"
    monkeypatch.setattr(warehouse_api, "load_warehouse_twin_floor", lambda _code: layout)
    try:
        with factory() as db:
            admin = User(
                username="slot-layout-admin",
                password_hash="test-only",
                role="admin",
                real_name="货位布局测试管理员",
                is_active=True,
                must_change_password=False,
                customer_access_mode="all",
                ui_mode="standard",
            )
            floor = WarehouseFloor(
                floor_code="3F",
                floor_name="三楼",
                floor_number=3,
                construction_status="enabled",
                planning_reference_pallet_capacity=0,
            )
            db.add_all([admin, floor])
            db.flush()
            area = WarehouseArea(
                floor_id=floor.id,
                area_code="TEST",
                area_name="测试栈板区",
                planned_location_count=4,
                planned_pallet_capacity=4,
                construction_status="enabled",
                capacity_review_status="pending",
                capacity_eligible=False,
            )
            db.add(area)
            db.flush()
            db.add(
                WarehouseAreaStoragePolicy(
                    area_id=area.id,
                    map_feature_id="ZONE-TEST",
                    allowed_inventory_types_json='["finished"]',
                    storage_layout="pallet_ground",
                    status="published",
                    published_map_revision="slot-layout-test",
                    version=1,
                    updated_by=admin.id,
                )
            )
            geometries = [
                (0, 0, "manual", 2),
                (0, 90, "seeded", 1),
                (0, 20, "seeded", 1),
                (0, 40, "seeded", 1),
            ]
            locations: list[WarehouseLocation] = []
            for index, (left, top, source_type, version) in enumerate(
                geometries, start=1
            ):
                location = WarehouseLocation(
                    location_code=f"3F-TEST-L{index:03d}",
                    location_name=f"测试栈板区 {index:03d} 号位",
                    warehouse_type="finished",
                    is_active=True,
                    warehouse_floor=3,
                    area_code="TEST",
                    storage_type="ground",
                    sort_order=index,
                    is_temporary=False,
                    source_version="TWIN_V1",
                    placement_status="placed",
                )
                location.floor3_layout = Floor3LocationLayout(
                    left_pct=Decimal(left),
                    top_pct=Decimal(top),
                    width_pct=Decimal(12),
                    height_pct=Decimal(10),
                    z_index=0,
                    version=version,
                    source_type=source_type,
                    created_by=admin.id,
                    updated_by=admin.id,
                )
                db.add(location)
                locations.append(location)
            db.flush()
            occupied = InventoryPallet(
                pallet_code="PALLET-LOCKED",
                location_id=locations[1].id,
                status="active",
                is_current=True,
                version=1,
                created_by=admin.id,
                updated_by=admin.id,
            )
            db.add(occupied)
            db.commit()

            fixed_before = (
                locations[0].floor3_layout.left_pct,
                locations[0].floor3_layout.top_pct,
                locations[0].floor3_layout.version,
            )
            occupied_before = (
                locations[1].floor3_layout.left_pct,
                locations[1].floor3_layout.top_pct,
                locations[1].floor3_layout.version,
                occupied.location_id,
                occupied.version,
            )
            result = warehouse_api._reflow_area_locations(
                db,
                floor_code="3F",
                area_code="TEST",
                source_version="TWIN_V1",
                operator_id=admin.id,
            )

            assert result["fixed_count"] == 2
            assert result["occupied_count"] == 1
            assert len(result["changes"]) == 2
            assert fixed_before == (
                locations[0].floor3_layout.left_pct,
                locations[0].floor3_layout.top_pct,
                locations[0].floor3_layout.version,
            )
            assert occupied_before == (
                locations[1].floor3_layout.left_pct,
                locations[1].floor3_layout.top_pct,
                locations[1].floor3_layout.version,
                occupied.location_id,
                occupied.version,
            )
            assert all(
                location.floor3_layout.source_type == "seeded"
                for location in locations[2:]
            )
            assert all(
                location.floor3_layout.layout_kind == "physical_pallet"
                for location in locations[2:]
            )
            assert db.scalar(select(func.count(InventoryPallet.id))) == 1

            replay = warehouse_api._reflow_area_locations(
                db,
                floor_code="3F",
                area_code="TEST",
                source_version="TWIN_V1",
                operator_id=admin.id,
            )
            assert replay["changes"] == []

            with pytest.raises(WarehouseAreaActivationError):
                adjust_area_location_count(
                    db,
                    floor_code="3F",
                    area_code="TEST",
                    target_count=1,
                    operator_id=admin.id,
                )
            assert len(
                list(
                    db.scalars(
                        select(WarehouseLocation).where(
                            WarehouseLocation.is_active.is_(True)
                        )
                    )
                )
            ) == 4
            assert occupied.location_id == locations[1].id
    finally:
        engine.dispose()


def test_one_step_confirmation_reuses_v11_without_creating_twin_duplicates(
    tmp_path, monkeypatch
) -> None:
    engine = create_sqlite_engine(tmp_path / "slot-v11-reuse.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    layout = _layout()
    layout["floor_code"] = "3F"
    monkeypatch.setattr(warehouse_api, "load_warehouse_twin_floor", lambda _code: layout)
    try:
        with factory() as db:
            admin = User(
                username="slot-v11-admin",
                password_hash="test-only",
                role="admin",
                real_name="旧版货位复用测试管理员",
                is_active=True,
                must_change_password=False,
                customer_access_mode="all",
                ui_mode="standard",
            )
            floor = WarehouseFloor(
                floor_code="3F",
                floor_name="三楼",
                floor_number=3,
                construction_status="enabled",
                planning_reference_pallet_capacity=0,
            )
            db.add_all([admin, floor])
            db.flush()
            area = WarehouseArea(
                floor_id=floor.id,
                area_code="E4",
                area_name="旧版三楼栈板区",
                planned_location_count=2,
                planned_pallet_capacity=2,
                construction_status="enabled",
                capacity_review_status="pending",
                capacity_eligible=False,
            )
            db.add(area)
            db.flush()
            policy = WarehouseAreaStoragePolicy(
                area_id=area.id,
                map_feature_id="ZONE-TEST",
                allowed_inventory_types_json='["finished"]',
                storage_layout="pallet_ground",
                status="published",
                published_map_revision="slot-layout-test",
                version=1,
                updated_by=admin.id,
            )
            db.add(policy)
            locations: list[WarehouseLocation] = []
            for index, (left, top) in enumerate(((0, 0), (0, 90)), start=1):
                location = WarehouseLocation(
                    location_code=f"E4-L{index:03d}",
                    location_name=f"旧版 E4 {index:03d} 号位",
                    warehouse_type="finished",
                    is_active=True,
                    warehouse_floor=3,
                    area_code="E4",
                    storage_type="ground",
                    sort_order=index,
                    is_temporary=False,
                    source_version="V11",
                    placement_status="placed",
                )
                location.floor3_layout = Floor3LocationLayout(
                    left_pct=Decimal(left),
                    top_pct=Decimal(top),
                    width_pct=Decimal(12),
                    height_pct=Decimal(10),
                    z_index=0,
                    version=1,
                    source_type="manual",
                    created_by=admin.id,
                    updated_by=admin.id,
                )
                db.add(location)
                locations.append(location)
            db.flush()
            occupied = InventoryPallet(
                pallet_code="PALLET-V11-LOCKED",
                location_id=locations[1].id,
                status="active",
                is_current=True,
                version=1,
                created_by=admin.id,
                updated_by=admin.id,
            )
            db.add(occupied)
            db.commit()

            occupied_before = (
                locations[1].floor3_layout.left_pct,
                locations[1].floor3_layout.top_pct,
                locations[1].floor3_layout.version,
                occupied.location_id,
            )
            created, active_count, metadata = (
                warehouse_api._ensure_one_step_pallet_locations(
                    db,
                    floor_layout=layout,
                    feature_id="ZONE-TEST",
                    area=area,
                    inventory_type="finished",
                    storage_layout="pallet_ground",
                    target_count=3,
                    operator_id=admin.id,
                )
            )

            assert active_count == 3
            assert len(created) == 1
            assert metadata["source_version"] == "V11"
            assert metadata["reflow"]["occupied_count"] == 1
            assert occupied_before == (
                locations[1].floor3_layout.left_pct,
                locations[1].floor3_layout.top_pct,
                locations[1].floor3_layout.version,
                occupied.location_id,
            )
            sources = set(
                db.scalars(
                    select(WarehouseLocation.source_version).where(
                        WarehouseLocation.area_code == "E4"
                    )
                ).all()
            )
            assert sources == {"V11"}
    finally:
        engine.dispose()
