from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.user import User
from app.models.warehouse_inventory import (
    Floor3LocationLayout,
    InventoryLot,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseLocation,
)
from app.services.location_candidates import list_operational_locations
from app.services.warehouse_area_activation import (
    WarehouseAreaActivationError,
    adjust_area_location_count,
    publish_floor_area_policies,
    update_area_location_layout,
)
from app.services.warehouse_twin_layout_editor import (
    _floor_revision,
    update_warehouse_twin_zone_policy,
)


ROOT = Path(__file__).resolve().parents[1]


def _factory(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "warehouse-area-activation.sqlite3")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _seed_area(
    db,
    *,
    allowed: list[str],
    area_code: str = "FIN-001",
    floor_code: str = "F1",
    floor_number: int = 1,
):
    if db.get(User, 1) is None:
        db.add(
            User(
                username="area-admin",
                password_hash="test-only",
                role="admin",
                real_name="区域测试管理员",
                is_active=True,
                must_change_password=False,
                customer_access_mode="all",
                ui_mode="standard",
            )
        )
        db.flush()
    floor = WarehouseFloor(
        floor_code=floor_code,
        floor_name=f"{floor_number} 楼",
        floor_number=floor_number,
        construction_status="enabled",
        planning_reference_pallet_capacity=0,
    )
    db.add(floor)
    db.flush()
    area = WarehouseArea(
        floor_id=floor.id,
        area_code=area_code,
        area_name="一楼正式区域",
        planned_location_count=0,
        planned_pallet_capacity=0,
        construction_status="layout_building",
        capacity_review_status="pending",
        capacity_eligible=False,
    )
    db.add(area)
    db.flush()
    policy = WarehouseAreaStoragePolicy(
        area_id=area.id,
        map_feature_id=f"zone-{area_code.lower()}",
        allowed_inventory_types_json=json.dumps(allowed),
        storage_layout="pallet_ground",
        status="draft",
        version=1,
        updated_by=1,
    )
    db.add(policy)
    db.flush()
    return floor, area, policy


def _add_placed_location(
    db,
    *,
    floor_number: int,
    area_code: str,
    serial: int,
    source_version: str,
) -> WarehouseLocation:
    row = WarehouseLocation(
        location_code=f"{area_code}-L{serial:03d}",
        location_name=f"{area_code} {serial:03d} 号位",
        warehouse_type="finished",
        is_active=True,
        warehouse_floor=floor_number,
        area_code=area_code,
        storage_type="ground",
        sort_order=serial,
        is_temporary=False,
        source_version=source_version,
        placement_status="placed",
    )
    row.floor3_layout = Floor3LocationLayout(
        left_pct=serial * 10,
        top_pct=10,
        width_pct=5,
        height_pct=5,
        z_index=0,
        version=1,
        source_type="manual",
        created_by=1,
        updated_by=1,
    )
    db.add(row)
    db.flush()
    return row


def test_one_floor_area_stays_fail_closed_until_layout_is_published(tmp_path: Path) -> None:
    engine, factory = _factory(tmp_path)
    try:
        with factory() as db:
            _floor, area, policy = _seed_area(
                db, allowed=["finished", "semi_finished"]
            )
            result = adjust_area_location_count(
                db,
                floor_code="1F",
                area_code=area.area_code,
                target_count=2,
                operator_id=1,
            )
            assert [row.location_code for row in result.created] == [
                "F1-FIN-001-L001",
                "F1-FIN-001-L002",
            ]
            assert {row.warehouse_type for row in result.created} == {"shared"}
            assert {row.placement_status for row in result.created} == {"unplaced"}
            assert list_operational_locations(db) == []

            slots = [
                {
                    "location_id": row.id,
                    "expected_version": row.floor3_layout.version,
                    "left_pct": row.floor3_layout.left_pct,
                    "top_pct": row.floor3_layout.top_pct,
                    "width_pct": row.floor3_layout.width_pct,
                    "height_pct": row.floor3_layout.height_pct,
                    "z_index": row.floor3_layout.z_index,
                }
                for row in result.created
            ]
            update_area_location_layout(
                db,
                floor_code="1F",
                area_code=area.area_code,
                slots=slots,
                operator_id=1,
            )
            assert area.construction_status == "layout_complete"
            assert list_operational_locations(db) == []

            published = publish_floor_area_policies(
                db,
                floor_code="1F",
                published_revision="revision-1",
                operator_id=1,
                published_features=[
                    {
                        "id": policy.map_feature_id,
                        "feature_kind": "zone",
                        "erp_area_code": area.area_code,
                        "allowed_inventory_types": ["finished", "semi_finished"],
                        "storage_layout": "pallet_ground",
                    }
                ],
            )
            assert published == [policy]
            assert area.construction_status == "enabled"
            assert policy.status == "published"
            candidates = list_operational_locations(
                db, warehouse_types={"finished", "semi_finished", "shared"}
            )
            assert [row.location.location_code for row in candidates] == [
                "F1-FIN-001-L001",
                "F1-FIN-001-L002",
            ]
    finally:
        engine.dispose()


def test_asset_or_raw_area_does_not_fake_inventory_locations(tmp_path: Path) -> None:
    engine, factory = _factory(tmp_path)
    try:
        with factory() as db:
            _seed_area(db, allowed=["raw_material", "mold"], area_code="RAW-001")
            with pytest.raises(
                WarehouseAreaActivationError,
                match="使用各自台账",
            ):
                adjust_area_location_count(
                    db,
                    floor_code="1F",
                    area_code="RAW-001",
                    target_count=1,
                    operator_id=1,
                )
    finally:
        engine.dispose()


def test_empty_floor_three_area_can_switch_to_semi_finished_without_new_ids(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    try:
        with factory() as db:
            floor, area, policy = _seed_area(
                db,
                allowed=["semi_finished"],
                area_code="F3",
                floor_code="3F",
                floor_number=3,
            )
            rows = [
                _add_placed_location(
                    db,
                    floor_number=3,
                    area_code=area.area_code,
                    serial=serial,
                    source_version="V11",
                )
                for serial in (1, 2)
            ]
            original_ids = [row.id for row in rows]
            area.planned_location_count = 2
            area.construction_status = "layout_complete"

            published = publish_floor_area_policies(
                db,
                floor_code=floor.floor_code,
                published_revision="revision-3f-semi",
                operator_id=1,
                published_features=[
                    {
                        "id": policy.map_feature_id,
                        "feature_kind": "zone",
                        "erp_area_code": area.area_code,
                        "allowed_inventory_types": ["semi_finished"],
                        "storage_layout": "pallet_ground",
                    }
                ],
            )

            assert published == [policy]
            assert [row.id for row in rows] == original_ids
            assert {row.warehouse_type for row in rows} == {"semi_finished"}
            assert {row.is_active for row in rows} == {True}
            candidates = list_operational_locations(
                db, warehouse_types={"semi_finished"}
            )
            assert [row.location.id for row in candidates] == original_ids
    finally:
        engine.dispose()


def test_floor_three_usage_change_is_blocked_when_inventory_is_incompatible(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    try:
        with factory() as db:
            floor, area, policy = _seed_area(
                db,
                allowed=["semi_finished"],
                area_code="F3",
                floor_code="3F",
                floor_number=3,
            )
            row = _add_placed_location(
                db,
                floor_number=3,
                area_code=area.area_code,
                serial=1,
                source_version="V11",
            )
            area.planned_location_count = 1
            db.add(
                InventoryLot(
                    lot_number="LOT-F3-FINISHED-1",
                    inventory_type="finished",
                    warehouse_location_id=row.id,
                    quantity_available=10,
                    quantity_reserved=0,
                    quantity_consumed=0,
                    quantity_damaged=0,
                    quantity_scrapped=0,
                    unit="boxes",
                    status="active",
                    source_type="manual",
                    stock_date=date(2026, 8, 10),
                    stock_date_accuracy="exact",
                    last_movement_at=datetime(2026, 8, 10, 8, 0),
                    version=1,
                )
            )
            db.flush()

            with pytest.raises(
                WarehouseAreaActivationError,
                match="与新用途不一致",
            ):
                publish_floor_area_policies(
                    db,
                    floor_code=floor.floor_code,
                    published_revision="revision-3f-semi",
                    operator_id=1,
                    published_features=[
                        {
                            "id": policy.map_feature_id,
                            "feature_kind": "zone",
                            "erp_area_code": area.area_code,
                            "allowed_inventory_types": ["semi_finished"],
                            "storage_layout": "pallet_ground",
                        }
                    ],
                )
            assert row.warehouse_type == "finished"
            assert policy.status == "draft"
    finally:
        engine.dispose()


def test_empty_floor_three_asset_area_disables_inventory_slots_but_keeps_ids(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    try:
        with factory() as db:
            floor, area, policy = _seed_area(
                db,
                allowed=["raw_material"],
                area_code="F3",
                floor_code="3F",
                floor_number=3,
            )
            row = _add_placed_location(
                db,
                floor_number=3,
                area_code=area.area_code,
                serial=1,
                source_version="V11",
            )
            original_id = row.id
            area.planned_location_count = 1

            published = publish_floor_area_policies(
                db,
                floor_code=floor.floor_code,
                published_revision="revision-3f-raw",
                operator_id=1,
                published_features=[
                    {
                        "id": policy.map_feature_id,
                        "feature_kind": "zone",
                        "erp_area_code": area.area_code,
                        "allowed_inventory_types": ["raw_material"],
                        "storage_layout": "pallet_ground",
                    }
                ],
            )

            assert published == [policy]
            assert row.id == original_id
            assert row.is_active is False
            assert area.planned_location_count == 0
            assert list_operational_locations(db) == []
    finally:
        engine.dispose()


def test_live_loose_inventory_marks_location_occupied_without_pallet(tmp_path: Path) -> None:
    engine, factory = _factory(tmp_path)
    try:
        with factory() as db:
            _floor, area, policy = _seed_area(db, allowed=["finished"])
            result = adjust_area_location_count(
                db,
                floor_code="1F",
                area_code=area.area_code,
                target_count=1,
                operator_id=1,
            )
            row = result.created[0]
            update_area_location_layout(
                db,
                floor_code="1F",
                area_code=area.area_code,
                slots=[
                    {
                        "location_id": row.id,
                        "expected_version": row.floor3_layout.version,
                        "left_pct": row.floor3_layout.left_pct,
                        "top_pct": row.floor3_layout.top_pct,
                        "width_pct": row.floor3_layout.width_pct,
                        "height_pct": row.floor3_layout.height_pct,
                        "z_index": row.floor3_layout.z_index,
                    }
                ],
                operator_id=1,
            )
            publish_floor_area_policies(
                db,
                floor_code="1F",
                published_revision="revision-1",
                operator_id=1,
                published_features=[
                    {
                        "id": policy.map_feature_id,
                        "feature_kind": "zone",
                        "erp_area_code": area.area_code,
                        "allowed_inventory_types": ["finished"],
                        "storage_layout": "pallet_ground",
                    }
                ],
            )
            db.add(
                InventoryLot(
                    lot_number="LOT-OCCUPIED-1",
                    inventory_type="finished",
                    warehouse_location_id=row.id,
                    quantity_available=10,
                    quantity_reserved=0,
                    quantity_consumed=0,
                    quantity_damaged=0,
                    quantity_scrapped=0,
                    unit="boxes",
                    status="active",
                    source_type="manual",
                    stock_date=date(2026, 8, 10),
                    stock_date_accuracy="exact",
                    last_movement_at=datetime(2026, 8, 10, 8, 0),
                    version=1,
                )
            )
            db.flush()
            all_rows = list_operational_locations(db, warehouse_types={"finished"})
            assert len(all_rows) == 1 and all_rows[0].occupied is True
            assert list_operational_locations(
                db, warehouse_types={"finished"}, empty_only=True
            ) == []
    finally:
        engine.dispose()


def test_zone_policy_can_bind_a_formal_area_code(tmp_path: Path) -> None:
    floor = {
        "layout_id": "layout-1f",
        "floor_code": "1F",
        "features": [
            {
                "id": "zone-raw-001",
                "feature_code": "ZONE-1F-RAW-001",
                "name": "一楼原料区",
                "feature_kind": "zone",
                "subtype": "raw_material",
                "points": [[0, 0], [100, 0], [100, 100], [0, 100]],
                "version": 1,
            }
        ],
        "racks": [],
        "pallets": [],
        "erp_area_codes": [],
    }
    floor["revision"] = _floor_revision(floor)
    path = tmp_path / "layout.json"
    path.write_text(
        json.dumps({"schema_version": 1, "floors": {"1F": floor}}, ensure_ascii=False),
        encoding="utf-8",
    )
    result = update_warehouse_twin_zone_policy(
        "1F",
        "zone-raw-001",
        expected_revision=floor["revision"],
        expected_version=1,
        operation_key="zone-bind-0001",
        allowed_inventory_types=["raw_material"],
        storage_layout="pallet_ground",
        erp_area_code="raw-001",
        path=path,
    )
    assert result.value["erp_area_code"] == "RAW-001"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["floors"]["1F"]["erp_area_codes"] == ["RAW-001"]


def test_inventory_ledger_link_escapes_the_embedded_map_frame() -> None:
    source = (
        ROOT / "factory_twin" / "frontend" / "src" / "WarehouseTwinApp.tsx"
    ).read_text(encoding="utf-8")

    assert 'href="/warehouse-ledger.html" target="_top"' in source
