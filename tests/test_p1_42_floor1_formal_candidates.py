from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.user import User
from app.models.warehouse_inventory import (
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseLocation,
    InventoryLot,
    InventoryPallet,
)
from app.services.location_candidates import list_operational_locations
from app.services.warehouse_floor1_candidate_planner import (
    Floor1CandidatePlanningError,
    build_floor1_formal_candidate_plan,
    confirm_floor1_formal_candidate_plan,
    inspect_floor1_formal_candidate_state,
)
from app.services.warehouse_area_activation import publish_floor_area_policies
from app.services.warehouse_twin_layout import load_warehouse_twin_floor


ROOT = Path(__file__).resolve().parents[1]


def _factory(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "floor1-formal-candidates.sqlite3")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _seed_floor_and_admin(db) -> None:
    db.add(
        User(
            username="floor1-candidate-admin",
            password_hash="test-only",
            role="admin",
            real_name="一楼候选测试管理员",
            is_active=True,
            must_change_password=False,
            customer_access_mode="all",
            ui_mode="standard",
        )
    )
    db.add(
        WarehouseFloor(
            floor_code="1F",
            floor_name="一楼",
            floor_number=1,
            construction_status="enabled",
            planning_reference_pallet_capacity=34,
        )
    )
    db.flush()


def test_floor1_candidate_plan_uses_confirmed_map_geometry() -> None:
    plan = build_floor1_formal_candidate_plan(load_warehouse_twin_floor("1F"))

    assert plan["candidate_count"] == 19
    assert plan["excluded_out_of_bounds_count"] == 3
    assert {
        row["feature_code"] for row in plan["excluded_out_of_bounds"]
    } == {
        "ZONE-1F-OUT-E-001",
        "ZONE-1F-OUT-E-002",
        "ZONE-1F-OUT-S-001",
    }
    assert plan["obstacle_count"] > 0
    assert plan["formal_location_count"] == 45
    assert plan["long_term_pallet_capacity"] == 70
    by_code = {row["area_code"]: row for row in plan["candidates"]}
    assert by_code["FIN-001"]["formal_location_count"] == 10
    assert by_code["FIN-003"]["formal_location_count"] == 15
    assert by_code["SEMI-001"]["formal_location_count"] == 9
    assert by_code["RAW-004"]["measured_pallet_slots"] == 0
    assert by_code["RAW-004"]["long_term_capacity_eligible"] is False
    assert "OUT-E-001" not in by_code
    assert "OUT-E-002" not in by_code
    assert "OUT-S-001" not in by_code
    assert by_code["MOLD-002"]["formal_location_count"] == 0
    assert all(
        0 <= slot["left_pct"] < 100
        and 0 <= slot["top_pct"] < 100
        and slot["left_pct"] + slot["width_pct"] <= 100.0001
        and slot["top_pct"] + slot["height_pct"] <= 100.0001
        for row in plan["candidates"]
        for slot in row["slots"]
    )
    fin_slot = by_code["FIN-001"]["slots"][0]
    assert fin_slot["left_pct"] == 0
    assert fin_slot["top_pct"] == pytest.approx(82.6087)
    assert fin_slot["width_pct"] == pytest.approx(48.9796)
    assert fin_slot["height_pct"] == pytest.approx(17.3913)


def test_floor1_candidate_plan_excludes_confirmed_physical_obstacles() -> None:
    layout = {
        "floor_code": "1F",
        "revision": "obstacle-test-revision",
        "bounds_mm": {"min_x": 0, "min_y": 0, "max_x": 2400, "max_y": 1000},
        "placements": [
            {
                "id": "machine-1",
                "x_mm": 600,
                "y_mm": 500,
                "width_mm": 1200,
                "depth_mm": 1000,
                "rotation_deg": 0,
                "is_confirmed": True,
            }
        ],
        "racks": [],
        "features": [
            {
                "id": "zone-fin-test",
                "feature_code": "ZONE-1F-FIN-TEST",
                "feature_kind": "zone",
                "name": "障碍物排除测试区",
                "subtype": "finished_wait_delivery",
                "status": "confirmed",
                "storage_mode": "floor",
                "points": [[0, 0], [2400, 0], [2400, 1000], [0, 1000]],
            }
        ],
    }

    plan = build_floor1_formal_candidate_plan(layout)
    candidate = plan["candidates"][0]
    assert candidate["measured_pallet_slots"] == 1
    assert candidate["formal_location_count"] == 1
    assert candidate["slots"][0]["x_mm"] == 1200


def test_floor1_candidate_confirmation_is_atomic_and_idempotent(tmp_path: Path) -> None:
    engine, factory = _factory(tmp_path)
    layout = load_warehouse_twin_floor("1F")
    plan = build_floor1_formal_candidate_plan(layout)
    try:
        with factory() as db:
            _seed_floor_and_admin(db)
            formal_state = inspect_floor1_formal_candidate_state(db, plan=plan)
            result = confirm_floor1_formal_candidate_plan(
                db,
                floor_layout=layout,
                expected_revision=plan["map_revision"],
                expected_fingerprint=plan["plan_fingerprint"],
                expected_formal_state_fingerprint=formal_state["fingerprint"],
                operator_id=1,
                reviewer_name="一楼候选测试管理员",
            )
            assert result.applied is True
            assert len(result.areas) == 19
            assert len(result.locations) == 45
            db.commit()

        with factory() as db:
            assert db.scalar(select(func.count(WarehouseArea.id))) == 19
            assert db.scalar(select(func.count(WarehouseAreaStoragePolicy.id))) == 19
            assert db.scalar(select(func.count(WarehouseLocation.id))) == 45
            assert len(list_operational_locations(db)) == 45
            raw = db.scalar(
                select(WarehouseArea).where(WarehouseArea.area_code == "RAW-003")
            )
            assert raw is not None
            assert raw.confirmed_pallet_capacity == 14
            assert raw.planned_location_count == 0
            assert raw.storage_policy is not None
            assert raw.storage_policy.status == "published"
            assert db.scalar(
                select(func.count(WarehouseLocation.id)).where(
                    WarehouseLocation.area_code == "RAW-003"
                )
            ) == 0

            replay = confirm_floor1_formal_candidate_plan(
                db,
                floor_layout=layout,
                expected_revision=plan["map_revision"],
                expected_fingerprint=plan["plan_fingerprint"],
                expected_formal_state_fingerprint=formal_state["fingerprint"],
                operator_id=1,
                reviewer_name="一楼候选测试管理员",
            )
            assert replay.applied is False
            assert len(replay.areas) == 19
            assert len(replay.locations) == 0

            republished = publish_floor_area_policies(
                db,
                floor_code="1F",
                published_revision="next-map-revision",
                operator_id=1,
                published_features=list(layout["features"]),
            )
            assert len(republished) == 19
            assert all(
                policy.published_map_revision == "next-map-revision"
                for policy in republished
            )
    finally:
        engine.dispose()


def test_floor1_candidate_confirmation_refuses_partial_existing_binding(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    layout = load_warehouse_twin_floor("1F")
    plan = build_floor1_formal_candidate_plan(layout)
    try:
        with factory() as db:
            _seed_floor_and_admin(db)
            floor = db.scalar(select(WarehouseFloor).where(WarehouseFloor.floor_number == 1))
            assert floor is not None
            db.add(
                WarehouseArea(
                    floor_id=floor.id,
                    area_code="FIN-001",
                    area_name="人工既有区域",
                    planned_location_count=0,
                    planned_pallet_capacity=0,
                    construction_status="ledger_building",
                    capacity_review_status="pending",
                    capacity_eligible=False,
                )
            )
            db.flush()
            with pytest.raises(Floor1CandidatePlanningError, match="不能静默覆盖"):
                formal_state = inspect_floor1_formal_candidate_state(db, plan=plan)
                confirm_floor1_formal_candidate_plan(
                    db,
                    floor_layout=layout,
                    expected_revision=plan["map_revision"],
                    expected_fingerprint=plan["plan_fingerprint"],
                    expected_formal_state_fingerprint=formal_state["fingerprint"],
                    operator_id=1,
                    reviewer_name="一楼候选测试管理员",
                )
            assert db.scalar(select(func.count(WarehouseArea.id))) == 1
            assert db.scalar(select(func.count(WarehouseLocation.id))) == 0
    finally:
        engine.dispose()


def test_floor1_confirmation_archives_only_empty_unmapped_legacy_area(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    layout = load_warehouse_twin_floor("1F")
    plan = build_floor1_formal_candidate_plan(layout)
    try:
        with factory() as db:
            _seed_floor_and_admin(db)
            floor = db.scalar(select(WarehouseFloor).where(WarehouseFloor.floor_number == 1))
            assert floor is not None
            legacy = WarehouseArea(
                floor_id=floor.id,
                area_code="DISPATCH",
                area_name="旧待发区域",
                planned_location_count=1,
                planned_pallet_capacity=1,
                construction_status="enabled",
                capacity_review_status="pending",
                capacity_eligible=False,
            )
            db.add(legacy)
            db.flush()
            old_location = WarehouseLocation(
                location_code="1F-DISPATCH-L001",
                location_name="旧待发区 001 号位",
                warehouse_type="finished",
                is_active=True,
                warehouse_floor=1,
                area_code="DISPATCH",
                storage_type="ground",
                sort_order=1,
                source_version="TWIN_V1",
                placement_status="placed",
            )
            db.add(old_location)
            db.flush()
            formal_state = inspect_floor1_formal_candidate_state(db, plan=plan)
            assert formal_state["archivable_legacy_area_count"] == 1
            assert formal_state["blocking_conflicts"] == []

            result = confirm_floor1_formal_candidate_plan(
                db,
                floor_layout=layout,
                expected_revision=plan["map_revision"],
                expected_fingerprint=plan["plan_fingerprint"],
                expected_formal_state_fingerprint=formal_state["fingerprint"],
                operator_id=1,
                reviewer_name="一楼候选测试管理员",
            )
            assert [area.area_code for area in result.archived_legacy_areas] == [
                "DISPATCH"
            ]
            assert old_location.is_active is False
            assert legacy.construction_status == "ledger_building"
            assert legacy.capacity_review_status == "excluded"
            assert legacy.planned_location_count == 0
            assert len(result.locations) == 45
    finally:
        engine.dispose()


def test_floor1_candidate_inventory_blocker_exposes_exact_move_action(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    plan = build_floor1_formal_candidate_plan(load_warehouse_twin_floor("1F"))
    try:
        with factory() as db:
            _seed_floor_and_admin(db)
            floor = db.scalar(select(WarehouseFloor).where(WarehouseFloor.floor_number == 1))
            assert floor is not None
            area = WarehouseArea(
                floor_id=floor.id,
                area_code="DISPATCH",
                area_name="待送区",
                planned_location_count=1,
                planned_pallet_capacity=1,
                construction_status="enabled",
                capacity_review_status="pending",
                capacity_eligible=False,
            )
            db.add(area)
            db.flush()
            location = WarehouseLocation(
                location_code="F1-DISPATCH-01",
                location_name="一楼厂外待送区",
                warehouse_type="finished",
                is_active=True,
                warehouse_floor=1,
                area_code="DISPATCH",
                storage_type="temporary_aisle",
                sort_order=1,
                source_version="P1-25C",
                placement_status="placed",
            )
            db.add(location)
            db.flush()
            db.add(
                InventoryLot(
                    lot_number="FG-DISPATCH-BLOCKER",
                    inventory_type="finished",
                    warehouse_location_id=location.id,
                    quantity_available=10,
                    quantity_reserved=0,
                    quantity_consumed=0,
                    quantity_damaged=0,
                    quantity_scrapped=0,
                    unit="boxes",
                    status="active",
                    source_type="manual",
                    stock_date=date(2026, 8, 13),
                    stock_date_accuracy="exact",
                    last_movement_at=datetime(2026, 8, 13, 10, 0),
                    version=1,
                )
            )
            db.add(
                InventoryPallet(
                    pallet_code="PLT-DISPATCH-BLOCKER",
                    location_id=location.id,
                    location_occupancy_key="DISPATCH-BLOCKER",
                    status="active",
                    is_current=True,
                    needs_relocation=True,
                    version=1,
                )
            )
            db.flush()

            formal_state = inspect_floor1_formal_candidate_state(db, plan=plan)
            assert formal_state["blocking_conflicts"] == [
                "DISPATCH 历史区域仍有库存或实体栈板，不能自动归档"
            ]
            assert formal_state["blocking_items"] == [
                {
                    "code": "legacy_area_inventory",
                    "message": "DISPATCH 历史区域仍有库存或实体栈板，不能自动归档",
                    "action_kind": "open_inventory_move",
                    "action_label": "去移动库存和栈板",
                    "area_id": area.id,
                    "area_code": "DISPATCH",
                    "map_feature_id": None,
                    "location_id": location.id,
                    "live_lot_count": 1,
                    "current_pallet_count": 1,
                }
            ]
    finally:
        engine.dispose()
