from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.api import warehouse as warehouse_api
from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.customer_finished_storage_preference import (
    CustomerFinishedStoragePreference,
)
from app.models.inventory_onboarding import InventoryOnboardingLine
from app.models.user import User
from app.models.warehouse_inventory import (
    InventoryLot,
    InventoryPallet,
    OrderedFinishedReceiptReturn,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseGroundLayoutPlan,
    WarehouseGroundLayoutPlanRetirement,
    WarehouseGroundLayoutSlot,
    WarehouseLocation,
    WarehouseLocationAddressMutation,
    WarehouseLocationAlias,
)
from app.services import warehouse_area_activation as activation
from app.services import inventory_onboarding as onboarding
from app.services import warehouse_location_address as address
from app.services import warehouse_twin_layout_editor as editor
from app.services.warehouse_floor1_candidate_planner import overlay_formal_area_bindings
from app.services.warehouse_twin_layout_editor import (
    WarehouseTwinLayoutEditConflictError,
    _floor_revision,
)


ROOT = Path(__file__).resolve().parents[1]
TWIN_SOURCE = (
    ROOT / "factory_twin" / "frontend" / "src" / "WarehouseTwinApp.tsx"
).read_text(encoding="utf-8")
CANVAS_SOURCE = (
    ROOT / "factory_twin" / "frontend" / "src" / "EditorCanvas.tsx"
).read_text(encoding="utf-8")


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "DELETE",
            "path": "/api/warehouse/twin-layout/floors/3F/features/zone-f1",
            "headers": [],
            "client": ("testclient", 50000),
        }
    )


def _layout(path: Path, *, with_rack: bool = False) -> Path:
    floor = {
        "layout_id": "layout-3f-p1-139",
        "floor_code": "3F",
        "bounds_mm": {"min_x": 0, "min_y": 0, "max_x": 10_000, "max_y": 10_000},
        "features": [
            {
                "id": "zone-f1",
                "feature_code": "ZONE-3F-ERP-F1",
                "name": "三楼待归档区",
                "feature_kind": "zone",
                "subtype": "rack_storage",
                "points": [[0, 0], [10_000, 0], [10_000, 10_000], [0, 10_000]],
                "area_mm2": 100_000_000,
                "version": 1,
                "is_locked": True,
                "erp_area_code": "F1",
            }
        ],
        "racks": (
            [
                {
                    "id": "rack-f1-01",
                    "rack_code": "R01",
                    "name": "空货架",
                    "area_feature_id": "zone-f1",
                    "x_mm": 1_000,
                    "y_mm": 1_000,
                    "width_mm": 2_000,
                    "depth_mm": 800,
                    "height_mm": 3_000,
                    "levels": 2,
                    "version": 1,
                }
            ]
            if with_rack
            else []
        ),
        "pallets": [],
    }
    floor["revision"] = _floor_revision(floor)
    path.write_text(
        json.dumps(
            {"schema_version": 1, "generated_at": "old", "floors": {"3F": floor}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _isolate_layout(tmp_path: Path, monkeypatch, *, with_rack: bool = False):
    baseline = _layout(tmp_path / "baseline.json", with_rack=with_rack)
    published = tmp_path / "runtime" / "published.json"
    draft = tmp_path / "runtime" / "draft.json"
    monkeypatch.setattr(editor, "TWIN_LAYOUT_BASELINE_PATH", baseline)
    monkeypatch.setattr(editor, "TWIN_LAYOUT_PATH", published)
    monkeypatch.setattr(editor, "TWIN_LAYOUT_DRAFT_PATH", draft)
    monkeypatch.setattr(editor, "TWIN_LAYOUT_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(
        warehouse_api,
        "list_production_projection_mappings",
        lambda *_args, **_kwargs: [],
    )
    return baseline, published, draft


def _append_layout_zone(path: Path, *, feature_id: str, area_code: str) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    floor = payload["floors"]["3F"]
    floor["features"].append(
        {
            "id": feature_id,
            "feature_code": f"ZONE-3F-ERP-{area_code}",
            "name": f"三楼{area_code}区",
            "feature_kind": "zone",
            "subtype": "rack_storage",
            "points": [[20_000, 0], [30_000, 0], [30_000, 10_000], [20_000, 10_000]],
            "area_mm2": 100_000_000,
            "version": 1,
            "is_locked": True,
            "erp_area_code": area_code,
        }
    )
    floor["bounds_mm"]["max_x"] = 30_000
    floor["revision"] = _floor_revision(floor)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return floor["revision"]


def _database(tmp_path: Path, *, revision: str):
    engine = create_sqlite_engine(tmp_path / "p1-139.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="p1-139-admin",
            password_hash="test-only",
            role="admin",
            real_name="P1-139 管理员",
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
            area_code="F1",
            area_name="三楼待归档区",
            planned_location_count=1,
            planned_pallet_capacity=1,
            construction_status="enabled",
            capacity_review_status="pending",
            capacity_eligible=False,
        )
        db.add(area)
        db.flush()
        policy = WarehouseAreaStoragePolicy(
            area_id=area.id,
            map_feature_id="zone-f1",
            allowed_inventory_types_json=json.dumps(["finished"]),
            storage_layout="pallet_ground",
            status="published",
            published_map_revision=revision,
            version=1,
            updated_by=admin.id,
        )
        location = WarehouseLocation(
            location_code="F1-G01",
            location_name="三楼待归档区第1位",
            warehouse_type="finished",
            is_active=True,
            warehouse_floor=3,
            area_code="F1",
            storage_type="ground",
            address_kind="ground_slot",
            address_area_id=area.id,
            ground_row_no=1,
            slot_no=1,
            source_version=activation.AREA_LOCATION_SOURCE_VERSION,
            placement_status="placed",
        )
        db.add_all([policy, location])
        db.commit()
    return engine, factory


def _archive(
    db,
    admin: User,
    *,
    revision: str,
    operation_key: str = "p1-139-archive-zone-f1",
    retire_ground_plan: bool = False,
    expected_ground_plan_version: int | None = None,
):
    return warehouse_api.delete_twin_layout_feature(
        "3F",
        "zone-f1",
        expected_revision=revision,
        expected_version=1,
        operation_key=operation_key,
        expected_policy_version=1,
        expected_published_revision=revision,
        retire_ground_plan=retire_ground_plan,
        expected_ground_plan_version=expected_ground_plan_version,
        request=_request(),
        db=db,
        user=admin,
    )


def _published_ground_plan(
    *, area: WarehouseArea, admin: User, revision: str
) -> WarehouseGroundLayoutPlan:
    return WarehouseGroundLayoutPlan(
        area_id=area.id,
        status="published",
        target_slot_count=1,
        numbering_origin="south",
        row_direction="from_aisle_inward",
        slot_direction="left_to_right",
        row_start_no=1,
        slot_start_no=1,
        draft_map_revision=revision,
        published_map_revision=revision,
        preview_fingerprint="f" * 64,
        version=1,
        publish_idempotency_key="p1-139-existing-ground-plan",
        publish_request_hash="e" * 64,
        updated_by=admin.id,
        published_by=admin.id,
        published_at=datetime(2026, 9, 1, 7, 30),
    )


def test_empty_published_area_archives_without_inventory_write_and_replays(
    tmp_path: Path,
    monkeypatch,
) -> None:
    baseline, published, draft = _isolate_layout(tmp_path, monkeypatch, with_rack=True)
    baseline_before = baseline.read_bytes()
    revision = json.loads(baseline.read_text(encoding="utf-8"))["floors"]["3F"]["revision"]
    engine, factory = _database(tmp_path, revision=revision)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == "p1-139-admin"))
            area = db.scalar(select(WarehouseArea))
            assert admin is not None and area is not None
            result = _archive(db, admin, revision=revision)
            assert result["applied"] is True
            assert result["item"]["archived"] is True
            assert result["item"]["inactive_location_count"] == 1
            assert result["item"]["archived_child_rack_count"] == 1

            policy = db.scalar(select(WarehouseAreaStoragePolicy))
            area = db.scalar(select(WarehouseArea))
            location = db.scalar(select(WarehouseLocation))
            assert policy is not None and area is not None and location is not None
            assert policy.status == "archived"
            assert policy.version == 2
            assert policy.archive_operation_key == "p1-139-archive-zone-f1"
            archive_snapshot = json.loads(policy.archive_feature_snapshot_json)
            assert (
                archive_snapshot["effective_layout_objects"]["features"][0]["id"]
                == "zone-f1"
            )
            assert (
                archive_snapshot["effective_layout_objects"]["racks"][0]["id"]
                == "rack-f1-01"
            )
            assert area.construction_status == "archived"
            assert area.capacity_review_status == "excluded"
            assert location.is_active is False
            assert db.scalar(select(WarehouseGroundLayoutPlan)) is None
            assert db.scalar(select(func.count(InventoryLot.id))) == 0
            assert db.scalar(select(func.count(InventoryPallet.id))) == 0
            assert db.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.action == "TWIN_LAYOUT_FORMAL_AREA_ARCHIVE"
                )
            ) == 1

            assert not draft.exists()
            assert not published.exists()
            assert baseline.read_bytes() == baseline_before
            raw_floor = json.loads(baseline.read_text(encoding="utf-8"))["floors"]["3F"]
            operational_floor = overlay_formal_area_bindings(
                db,
                floor_code="3F",
                floor_layout=raw_floor,
                include_draft=False,
            )
            assert operational_floor["features"] == []
            assert operational_floor["racks"] == []

            replay = _archive(db, admin, revision=revision)
            assert replay["applied"] is False
            assert replay["idempotent_replay"] is True
            assert db.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.action == "TWIN_LAYOUT_FORMAL_AREA_ARCHIVE"
                )
            ) == 1
    finally:
        engine.dispose()


def test_archiving_preserves_active_return_history_after_return_lot_transfers_away(
    tmp_path: Path,
    monkeypatch,
) -> None:
    baseline, _published, _draft = _isolate_layout(tmp_path, monkeypatch)
    revision = json.loads(baseline.read_text(encoding="utf-8"))["floors"]["3F"][
        "revision"
    ]
    engine, factory = _database(tmp_path, revision=revision)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == "p1-139-admin"))
            location = db.scalar(select(WarehouseLocation))
            assert admin is not None and location is not None
            staging_location = WarehouseLocation(
                location_code="TMP-RETURN-01",
                location_name="临时退回存放位",
                warehouse_type="finished",
                is_active=True,
                warehouse_floor=3,
                area_code="TMP",
                storage_type="ground",
                address_kind="legacy",
                source_version="test",
                placement_status="placed",
            )
            db.add(staging_location)
            db.flush()
            return_lot = InventoryLot(
                lot_number="P1-139-RETURN-MOVED",
                inventory_type="finished",
                warehouse_location_id=staging_location.id,
                quantity_available=6,
                quantity_reserved=0,
                quantity_consumed=0,
                quantity_damaged=0,
                quantity_scrapped=0,
                unit="boxes",
                status="active",
                source_type="delivery_return",
                stock_date=date(2026, 9, 7),
                stock_date_accuracy="exact",
                last_movement_at=datetime(2026, 9, 7, 8, 0),
                version=2,
            )
            db.add(return_lot)
            db.commit()
            location_id = location.id
            staging_location_id = staging_location.id
            return_lot_id = return_lot.id

        # The archive guard only reads the immutable return fact and its return
        # lot.  Its receipt/allocation/movement dependencies are intentionally
        # outside this focused archive fixture, so insert the isolated fact
        # through a separate SQLite connection with FKs disabled.
        raw = sqlite3.connect(engine.url.database)
        try:
            raw.execute("PRAGMA foreign_keys = OFF")
            cursor = raw.execute(
                """
                INSERT INTO ordered_finished_receipt_returns (
                    return_receipt_item_id, sequence_no, delivery_item_id,
                    delivery_inventory_allocation_id, source_inventory_lot_id,
                    return_inventory_lot_id, return_location_id, reservation_id,
                    return_in_movement_id, source_reverse_movement_id,
                    source_transfer_movement_id, quantity, resolution_action,
                    status, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    910_001,
                    1,
                    910_001,
                    910_001,
                    return_lot_id,
                    return_lot_id,
                    location_id,
                    None,
                    910_001,
                    910_002,
                    910_003,
                    6,
                    "accept_short",
                    "active",
                    "p1-139-return-history-after-transfer",
                ),
            )
            return_fact_id = int(cursor.lastrowid)
            raw.commit()
        finally:
            raw.close()

        with factory() as db:
            admin = db.scalar(select(User).where(User.username == "p1-139-admin"))
            assert admin is not None
            result = _archive(db, admin, revision=revision)
            assert result["applied"] is True
            db.expire_all()
            persisted_fact = db.get(OrderedFinishedReceiptReturn, return_fact_id)
            persisted_lot = db.get(InventoryLot, return_lot_id)
            assert persisted_fact is not None and persisted_lot is not None
            assert persisted_fact.status == "active"
            assert persisted_fact.return_location_id == location_id
            assert persisted_lot.warehouse_location_id == staging_location_id
            assert persisted_lot.quantity_available == 6
            assert db.scalar(select(WarehouseLocation.is_active)) is False
    finally:
        engine.dispose()


def test_three_floor_archive_does_not_require_one_floor_production_projection_database(
    tmp_path: Path,
    monkeypatch,
) -> None:
    baseline, _published, _draft = _isolate_layout(tmp_path, monkeypatch)
    revision = json.loads(baseline.read_text(encoding="utf-8"))["floors"]["3F"]["revision"]
    engine, factory = _database(tmp_path, revision=revision)

    def unexpected_projection_lookup(*_args, **_kwargs):
        raise AssertionError("3F archive must not read the 1F production projection database")

    monkeypatch.setattr(
        warehouse_api,
        "list_production_projection_mappings",
        unexpected_projection_lookup,
    )
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == "p1-139-admin"))
            assert admin is not None
            result = _archive(db, admin, revision=revision)
            assert result["applied"] is True
            assert result["item"]["archived"] is True
            assert db.scalar(select(func.count(InventoryLot.id))) == 0
            assert db.scalar(select(func.count(InventoryPallet.id))) == 0
    finally:
        engine.dispose()


def test_one_floor_archive_still_fails_closed_when_production_projection_is_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    baseline, _published, _draft = _isolate_layout(tmp_path, monkeypatch)
    revision = json.loads(baseline.read_text(encoding="utf-8"))["floors"]["3F"]["revision"]
    engine, factory = _database(tmp_path, revision=revision)

    def unavailable_projection(*_args, **_kwargs):
        raise warehouse_api.WarehouseTwinProductionError("isolated projection missing")

    monkeypatch.setattr(
        warehouse_api,
        "list_production_projection_mappings",
        unavailable_projection,
    )
    try:
        with factory() as db:
            blockers = warehouse_api._zone_asset_and_production_blockers(
                db,
                floor_layout={
                    "floor_code": "1F",
                    "layout_id": "layout-1f",
                    "pallets": [],
                },
                feature={
                    "id": "zone-1f",
                    "feature_code": "ZONE-1F-ERP-F1",
                },
                area_code="F1",
                fail_closed_on_mapping_error=True,
            )
            assert blockers == ["生产任务地图占用状态暂无法核对"]
    finally:
        engine.dispose()


def test_published_ground_plan_blocks_archive_without_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    baseline, _published, draft = _isolate_layout(tmp_path, monkeypatch)
    revision = json.loads(baseline.read_text(encoding="utf-8"))["floors"]["3F"]["revision"]
    engine, factory = _database(tmp_path, revision=revision)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == "p1-139-admin"))
            area = db.scalar(select(WarehouseArea))
            assert admin is not None and area is not None
            db.add(_published_ground_plan(area=area, admin=admin, revision=revision))
            db.commit()

            with pytest.raises(HTTPException, match="已发布地堆排位方案") as caught:
                _archive(db, admin, revision=revision)
            assert caught.value.status_code == 409
            db.expire_all()
            assert db.scalar(select(WarehouseAreaStoragePolicy.status)) == "published"
            assert db.scalar(select(WarehouseArea.construction_status)) == "enabled"
            assert db.scalar(select(WarehouseLocation.is_active)) is True
            assert db.scalar(select(WarehouseGroundLayoutPlan.status)) == "published"
            assert db.scalar(select(func.count(InventoryLot.id))) == 0
            assert db.scalar(select(func.count(InventoryPallet.id))) == 0
            assert db.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.action == "TWIN_LAYOUT_FORMAL_AREA_ARCHIVE"
                )
            ) == 0
            assert not draft.exists()
    finally:
        engine.dispose()


def test_explicit_ground_plan_retirement_preserves_plan_and_slots_while_archiving(
    tmp_path: Path,
    monkeypatch,
) -> None:
    baseline, _published, _draft = _isolate_layout(tmp_path, monkeypatch)
    revision = json.loads(baseline.read_text(encoding="utf-8"))["floors"]["3F"]["revision"]
    engine, factory = _database(tmp_path, revision=revision)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == "p1-139-admin"))
            area = db.scalar(select(WarehouseArea))
            location = db.scalar(select(WarehouseLocation))
            assert admin is not None and area is not None and location is not None
            plan = _published_ground_plan(area=area, admin=admin, revision=revision)
            db.add(plan)
            db.flush()
            db.add(
                WarehouseGroundLayoutSlot(
                    plan_id=plan.id,
                    location_id=location.id,
                    route_sequence=1,
                    row_no=1,
                    slot_no=1,
                    x_mm=0,
                    y_mm=0,
                    width_mm=1200,
                    depth_mm=1000,
                )
            )
            db.commit()

            result = _archive(
                db,
                admin,
                revision=revision,
                retire_ground_plan=True,
                expected_ground_plan_version=1,
            )
            assert result["applied"] is True
            assert result["item"]["retired_ground_plan_id"] == plan.id
            db.expire_all()
            retirement = db.scalar(select(WarehouseGroundLayoutPlanRetirement))
            assert retirement is not None
            assert retirement.plan_id == plan.id
            assert retirement.area_id == area.id
            assert json.loads(retirement.snapshot_json)["slots"][0]["location_id"] == location.id
            assert db.scalar(select(func.count(WarehouseGroundLayoutPlan.id))) == 1
            assert db.scalar(select(func.count(WarehouseGroundLayoutSlot.id))) == 1
            assert db.scalar(select(WarehouseArea.construction_status)) == "archived"
            assert db.scalar(select(WarehouseAreaStoragePolicy.status)) == "archived"
            assert db.scalar(select(WarehouseLocation.is_active)) is False
            assert db.scalar(select(func.count(InventoryLot.id))) == 0
    finally:
        engine.dispose()


def test_ground_plan_retirement_refuses_stale_plan_version_without_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    baseline, _published, draft = _isolate_layout(tmp_path, monkeypatch)
    revision = json.loads(baseline.read_text(encoding="utf-8"))["floors"]["3F"]["revision"]
    engine, factory = _database(tmp_path, revision=revision)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == "p1-139-admin"))
            area = db.scalar(select(WarehouseArea))
            assert admin is not None and area is not None
            db.add(_published_ground_plan(area=area, admin=admin, revision=revision))
            db.commit()

            with pytest.raises(HTTPException, match="地堆排位版本已变化") as caught:
                _archive(
                    db,
                    admin,
                    revision=revision,
                    retire_ground_plan=True,
                    expected_ground_plan_version=2,
                )
            assert caught.value.status_code == 409
            db.expire_all()
            assert db.scalar(select(WarehouseAreaStoragePolicy.status)) == "published"
            assert db.scalar(select(WarehouseArea.construction_status)) == "enabled"
            assert db.scalar(select(WarehouseLocation.is_active)) is True
            assert db.scalar(select(func.count(WarehouseGroundLayoutPlanRetirement.id))) == 0
            assert not draft.exists()
    finally:
        engine.dispose()


def test_archived_area_cannot_be_restored_through_ordinary_edit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    baseline, _published, _draft = _isolate_layout(tmp_path, monkeypatch)
    revision = json.loads(baseline.read_text(encoding="utf-8"))["floors"]["3F"]["revision"]
    engine, factory = _database(tmp_path, revision=revision)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == "p1-139-admin"))
            area = db.scalar(select(WarehouseArea))
            floor = db.scalar(select(WarehouseFloor))
            assert admin is not None and area is not None and floor is not None
            _archive(db, admin, revision=revision)

            payload = warehouse_api.WarehouseAreaPayload(
                floor_id=floor.id,
                area_code=area.area_code,
                area_name="尝试恢复的区域",
                planned_location_count=area.planned_location_count,
                planned_pallet_capacity=area.planned_pallet_capacity,
                capacity_review_status="pending",
                capacity_eligible=False,
                confirmed_pallet_capacity=None,
                construction_status="enabled",
            )
            with pytest.raises(HTTPException, match="归档") as caught:
                warehouse_api.update_warehouse_area(
                    area.id,
                    payload,
                    request=_request(),
                    db=db,
                    user=admin,
                )
            assert caught.value.status_code == 409
            db.expire_all()
            assert db.scalar(select(WarehouseArea.construction_status)) == "archived"
            assert db.scalar(select(WarehouseAreaStoragePolicy.status)) == "archived"
            assert db.scalar(select(WarehouseLocation.is_active)) is False
            assert db.scalar(select(func.count(InventoryLot.id))) == 0
    finally:
        engine.dispose()


def test_inventory_onboarding_cannot_create_reference_after_area_archive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    baseline, _published, _draft = _isolate_layout(tmp_path, monkeypatch)
    revision = json.loads(baseline.read_text(encoding="utf-8"))["floors"]["3F"]["revision"]
    engine, factory = _database(tmp_path, revision=revision)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == "p1-139-admin"))
            location = db.scalar(select(WarehouseLocation))
            assert admin is not None and location is not None
            _archive(db, admin, revision=revision)

            line = InventoryOnboardingLine(
                location_id=location.id,
                location_code_snapshot=location.location_code,
            )
            with pytest.raises(onboarding.InventoryOnboardingError) as caught:
                onboarding.match_onboarding_line(
                    db,
                    batch=object(),
                    line=line,
                )
            assert caught.value.status_code == 409
            assert caught.value.code == "INVENTORY_ONBOARDING_LOCATION_ARCHIVED"
            assert db.scalar(select(func.count(InventoryOnboardingLine.id))) == 0
    finally:
        engine.dispose()


def test_inventory_onboarding_existing_lot_only_cannot_bypass_archived_area(
    tmp_path: Path,
    monkeypatch,
) -> None:
    baseline, _published, _draft = _isolate_layout(tmp_path, monkeypatch)
    revision = json.loads(baseline.read_text(encoding="utf-8"))["floors"]["3F"]["revision"]
    engine, factory = _database(tmp_path, revision=revision)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == "p1-139-admin"))
            location = db.scalar(select(WarehouseLocation))
            assert admin is not None and location is not None
            lot = InventoryLot(
                lot_number="P1-139-ZERO-LOT",
                inventory_type="finished",
                warehouse_location_id=location.id,
                quantity_available=0,
                quantity_reserved=0,
                quantity_consumed=0,
                quantity_damaged=0,
                quantity_scrapped=0,
                unit="boxes",
                status="active",
                source_type="manual",
                stock_date=date(2026, 9, 1),
                stock_date_accuracy="exact",
                last_movement_at=datetime(2026, 9, 1, 8, 0),
                version=1,
            )
            db.add(lot)
            db.commit()
            _archive(db, admin, revision=revision)

            line = InventoryOnboardingLine(
                original_values_json={"mapped": {"existing_lot_id": lot.id}},
            )
            with pytest.raises(onboarding.InventoryOnboardingError) as caught:
                onboarding.match_onboarding_line(
                    db,
                    batch=object(),
                    line=line,
                )
            assert caught.value.status_code == 409
            assert caught.value.code == "INVENTORY_ONBOARDING_LOCATION_ARCHIVED"
            assert line.location_id is None
            assert db.scalar(select(func.count(InventoryOnboardingLine.id))) == 0
    finally:
        engine.dispose()


def test_inventory_onboarding_cannot_freeze_excluded_inactive_location(
    tmp_path: Path,
    monkeypatch,
) -> None:
    baseline, _published, _draft = _isolate_layout(tmp_path, monkeypatch)
    revision = json.loads(baseline.read_text(encoding="utf-8"))["floors"]["3F"]["revision"]
    engine, factory = _database(tmp_path, revision=revision)
    try:
        with factory() as db:
            location = db.scalar(select(WarehouseLocation))
            assert location is not None
            location.is_active = False
            db.commit()

            line = InventoryOnboardingLine(
                location_id=location.id,
                location_code_snapshot=location.location_code,
                action_decision="exclude",
            )
            with pytest.raises(onboarding.InventoryOnboardingError) as caught:
                onboarding.match_onboarding_line(
                    db,
                    batch=object(),
                    line=line,
                )
            assert caught.value.status_code == 409
            assert caught.value.code == "INVENTORY_ONBOARDING_LOCATION_INACTIVE"
    finally:
        engine.dispose()


def test_floor_identity_put_rechecks_archive_after_persistent_claim(
    tmp_path: Path,
    monkeypatch,
) -> None:
    baseline, _published, _draft = _isolate_layout(tmp_path, monkeypatch)
    revision = json.loads(baseline.read_text(encoding="utf-8"))["floors"]["3F"]["revision"]
    engine, factory = _database(tmp_path, revision=revision)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == "p1-139-admin"))
            floor = db.scalar(select(WarehouseFloor))
            assert admin is not None and floor is not None

            def archive_just_before_claim(session, *, floor_id: int) -> bool:
                assert floor_id == floor.id
                area = session.scalar(select(WarehouseArea))
                policy = session.scalar(select(WarehouseAreaStoragePolicy))
                assert area is not None and policy is not None
                area.construction_status = "archived"
                policy.status = "archived"
                policy.archived_at = datetime(2026, 9, 1, 9, 0)
                policy.archived_by = admin.id
                policy.archive_operation_key = "p1-139-floor-put-race"
                policy.archive_request_hash = "a" * 64
                policy.archive_feature_snapshot_json = "{}"
                session.commit()
                return True

            monkeypatch.setattr(
                warehouse_api,
                "claim_warehouse_floor_projection_by_id",
                archive_just_before_claim,
            )
            payload = warehouse_api.WarehouseFloorPayload(
                floor_code="13F",
                floor_name="十三楼",
                floor_number=13,
                construction_status="enabled",
            )
            with pytest.raises(HTTPException, match="归档") as caught:
                warehouse_api.update_warehouse_floor(
                    floor.id,
                    payload,
                    request=_request(),
                    db=db,
                    user=admin,
                )
            assert caught.value.status_code == 409
            db.expire_all()
            current_floor = db.get(WarehouseFloor, floor.id)
            assert current_floor is not None
            assert current_floor.floor_code == "3F"
            assert current_floor.floor_number == 3
    finally:
        engine.dispose()


def test_area_put_rechecks_archive_after_source_and_target_floor_claims(
    tmp_path: Path,
    monkeypatch,
) -> None:
    baseline, _published, _draft = _isolate_layout(tmp_path, monkeypatch)
    revision = json.loads(baseline.read_text(encoding="utf-8"))["floors"]["3F"]["revision"]
    engine, factory = _database(tmp_path, revision=revision)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == "p1-139-admin"))
            floor = db.scalar(select(WarehouseFloor))
            area = db.scalar(select(WarehouseArea))
            assert admin is not None and floor is not None and area is not None

            def archive_just_before_claim(session, *, floor_id: int) -> bool:
                assert floor_id == floor.id
                current_area = session.get(WarehouseArea, area.id)
                policy = session.scalar(select(WarehouseAreaStoragePolicy))
                assert current_area is not None and policy is not None
                current_area.construction_status = "archived"
                policy.status = "archived"
                policy.archived_at = datetime(2026, 9, 1, 9, 0)
                policy.archived_by = admin.id
                policy.archive_operation_key = "p1-139-area-put-race"
                policy.archive_request_hash = "b" * 64
                policy.archive_feature_snapshot_json = "{}"
                session.commit()
                return True

            monkeypatch.setattr(
                warehouse_api,
                "claim_warehouse_floor_projection_by_id",
                archive_just_before_claim,
            )
            payload = warehouse_api.WarehouseAreaPayload(
                floor_id=floor.id,
                area_code="F9",
                area_name="尝试改名迁出的区域",
                planned_location_count=area.planned_location_count,
                planned_pallet_capacity=area.planned_pallet_capacity,
                capacity_review_status="pending",
                capacity_eligible=False,
                construction_status="enabled",
            )
            with pytest.raises(HTTPException, match="归档") as caught:
                warehouse_api.update_warehouse_area(
                    area.id,
                    payload,
                    request=_request(),
                    db=db,
                    user=admin,
                )
            assert caught.value.status_code == 409
            db.expire_all()
            current_area = db.get(WarehouseArea, area.id)
            assert current_area is not None
            assert current_area.area_code == "F1"
            assert current_area.floor_id == floor.id
    finally:
        engine.dispose()


def test_ordinary_area_payload_rejects_direct_archive_status() -> None:
    with pytest.raises(ValueError, match="建设状态不合法"):
        warehouse_api.WarehouseAreaPayload(
            floor_id=1,
            area_code="F1",
            area_name="禁止绕过受控流程的区域",
            construction_status="archived",
        )


def _archived_state(db) -> tuple:
    policy = db.scalar(select(WarehouseAreaStoragePolicy))
    area = db.scalar(select(WarehouseArea))
    location = db.scalar(select(WarehouseLocation))
    assert policy is not None and area is not None and location is not None
    return (
        policy.status,
        policy.version,
        policy.archive_operation_key,
        policy.archive_request_hash,
        policy.archive_feature_snapshot_json,
        area.construction_status,
        location.is_active,
        db.scalar(select(func.count(OperationLog.id))),
        db.scalar(select(func.coalesce(func.sum(InventoryLot.quantity_available), 0))),
    )


@pytest.mark.parametrize(
    "action",
    ["formal", "resolve", "count", "enable", "layout", "publish"],
)
def test_archived_area_is_immutable_in_activation_service_paths(
    tmp_path: Path,
    monkeypatch,
    action: str,
) -> None:
    baseline, _published, _draft = _isolate_layout(tmp_path, monkeypatch)
    floor_layout = json.loads(baseline.read_text(encoding="utf-8"))["floors"]["3F"]
    revision = floor_layout["revision"]
    engine, factory = _database(tmp_path, revision=revision)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == "p1-139-admin"))
            location = db.scalar(select(WarehouseLocation))
            assert admin is not None and location is not None
            _archive(db, admin, revision=revision)
            before = _archived_state(db)
            with pytest.raises(activation.WarehouseAreaActivationError, match="归档"):
                if action == "formal":
                    activation.formal_area(db, floor_code="3F", area_code="F1")
                elif action == "resolve":
                    activation.resolve_area_location_management(
                        db, floor_code="3F", area_code="F1"
                    )
                elif action == "count":
                    activation.adjust_area_location_count(
                        db,
                        floor_code="3F",
                        area_code="F1",
                        target_count=1,
                        operator_id=admin.id,
                    )
                elif action == "enable":
                    activation.set_area_location_active(
                        db,
                        location_id=location.id,
                        is_active=True,
                        expected_version=1,
                        operator_id=admin.id,
                    )
                elif action == "layout":
                    activation.update_area_location_layout(
                        db,
                        floor_code="3F",
                        area_code="F1",
                        slots=[],
                        operator_id=admin.id,
                    )
                else:
                    activation.publish_floor_area_policies(
                        db,
                        floor_code="3F",
                        published_revision=revision,
                        operator_id=admin.id,
                        published_features=floor_layout["features"],
                    )
            db.rollback()
            db.expire_all()
            assert _archived_state(db) == before
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "action",
    ["create_location", "update_location", "enable_location", "disable_location", "policy"],
)
def test_archived_area_is_immutable_in_legacy_api_paths(
    tmp_path: Path,
    monkeypatch,
    action: str,
) -> None:
    baseline, _published, draft = _isolate_layout(tmp_path, monkeypatch)
    floor_layout = json.loads(baseline.read_text(encoding="utf-8"))["floors"]["3F"]
    revision = floor_layout["revision"]
    engine, factory = _database(tmp_path, revision=revision)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == "p1-139-admin"))
            area = db.scalar(select(WarehouseArea))
            location = db.scalar(select(WarehouseLocation))
            assert admin is not None and area is not None and location is not None
            _archive(db, admin, revision=revision)
            before = _archived_state(db)
            location_payload = warehouse_api.LocationPayload(
                location_code="F1-G02" if action == "create_location" else location.location_code,
                location_name="归档区域普通入口测试",
                warehouse_type="finished",
                warehouse_floor=3,
                area_code="F1",
                storage_type="ground",
            )
            with pytest.raises(HTTPException, match="归档") as caught:
                if action == "create_location":
                    warehouse_api.create_location(location_payload, db=db, user=admin)
                elif action == "update_location":
                    warehouse_api.update_location(
                        location.id, location_payload, db=db, _user=admin
                    )
                elif action == "enable_location":
                    warehouse_api.enable_location(location.id, db=db, _user=admin)
                elif action == "disable_location":
                    warehouse_api.disable_location(location.id, db=db, _user=admin)
                else:
                    if draft.exists():
                        draft.unlink()
                    warehouse_api._update_twin_zone_storage_policy_locked(
                        floor_code="3F",
                        feature_id="zone-f1",
                        payload=warehouse_api.TwinZoneStoragePolicyPayload(
                            expected_revision=revision,
                            expected_version=1,
                            operation_key="p1-139-policy-restore",
                            allowed_inventory_types=["finished"],
                            storage_layout="pallet_ground",
                            erp_area_code="F1",
                            area_name="尝试恢复的正式区域",
                            existing_area_id=area.id,
                        ),
                        request=_request(),
                        db=db,
                        user=admin,
                        commit=False,
                    )
            assert caught.value.status_code == 409
            db.rollback()
            db.expire_all()
            assert _archived_state(db) == before
    finally:
        engine.dispose()


@pytest.mark.parametrize("action", ["area", "rack", "location"])
@pytest.mark.parametrize("phase", ["preview", "confirm"])
def test_archived_area_is_immutable_in_address_governance(
    tmp_path: Path,
    monkeypatch,
    action: str,
    phase: str,
) -> None:
    baseline, _published, _draft = _isolate_layout(tmp_path, monkeypatch)
    revision = json.loads(baseline.read_text(encoding="utf-8"))["floors"]["3F"]["revision"]
    engine, factory = _database(tmp_path, revision=revision)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == "p1-139-admin"))
            area = db.scalar(select(WarehouseArea))
            floor = db.scalar(select(WarehouseFloor))
            location = db.scalar(select(WarehouseLocation))
            assert admin is not None and area is not None and floor is not None and location is not None
            _archive(db, admin, revision=revision)
            target_area = None
            if action == "location":
                target_area = WarehouseArea(
                    floor_id=floor.id,
                    area_code="B01",
                    area_name="三楼地址治理目标区",
                    planned_location_count=0,
                    planned_pallet_capacity=0,
                    construction_status="enabled",
                    capacity_review_status="pending",
                    capacity_eligible=False,
                    address_zone_code="B",
                    address_subzone_no=1,
                )
                db.add(target_area)
                db.commit()
            command = (
                address.AddressChangeCommand(
                    action_kind="area",
                    area_id=area.id,
                    new_zone_code="A",
                    new_subzone_no=1,
                )
                if action == "area"
                else (
                    address.AddressChangeCommand(
                        action_kind="rack",
                        area_id=area.id,
                        current_rack_code="A",
                        new_rack_code="B",
                    )
                    if action == "rack"
                    else address.AddressChangeCommand(
                        action_kind="location",
                        area_id=target_area.id,
                        location_id=location.id,
                        new_address_kind="ground_slot",
                        new_ground_row_no=1,
                        new_slot_no=1,
                    )
                )
            )
            before = _archived_state(db)
            before_address = (
                area.area_code,
                area.address_version,
                location.location_code,
                location.address_area_id,
                location.address_version,
                db.scalar(select(func.count(WarehouseLocationAlias.id))),
                db.scalar(select(func.count(WarehouseLocationAddressMutation.id))),
            )
            with pytest.raises(address.WarehouseLocationAddressError, match="归档"):
                if phase == "preview":
                    address.build_address_change_preview(db, command)
                else:
                    address.confirm_address_change(
                        db,
                        command,
                        preview_fingerprint="f" * 64,
                        idempotency_key=f"p1-139-address-{action}",
                        actor_user_id=admin.id,
                    )
            db.rollback()
            db.expire_all()
            assert _archived_state(db) == before
            area = db.get(WarehouseArea, area.id)
            location = db.get(WarehouseLocation, location.id)
            assert area is not None and location is not None
            assert (
                area.area_code,
                area.address_version,
                location.location_code,
                location.address_area_id,
                location.address_version,
                db.scalar(select(func.count(WarehouseLocationAlias.id))),
                db.scalar(select(func.count(WarehouseLocationAddressMutation.id))),
            ) == before_address
    finally:
        engine.dispose()


def test_archived_locations_do_not_leak_into_operational_dashboard(
    tmp_path: Path,
    monkeypatch,
) -> None:
    baseline, _published, _draft = _isolate_layout(tmp_path, monkeypatch)
    revision = json.loads(baseline.read_text(encoding="utf-8"))["floors"]["3F"]["revision"]
    engine, factory = _database(tmp_path, revision=revision)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == "p1-139-admin"))
            assert admin is not None
            _archive(db, admin, revision=revision)
            lots, locations, pallets, _floors, _visible = warehouse_api._twin_dashboard_source_rows(
                db, admin
            )
            assert lots == []
            assert locations == []
            assert pallets == []
            dashboard = warehouse_api.get_warehouse_twin_dashboard(
                days=30,
                db=db,
                user=admin,
                dispatch_idle_days=3,
            )
            assert dashboard["locations"] == []
            floor3 = next(row for row in dashboard["floors"] if row["floor_code"] == "3F")
            assert floor3["capacity"]["planned_pallet_capacity"] == 0
    finally:
        engine.dispose()


def test_archive_operation_key_is_global_and_cannot_cross_areas(
    tmp_path: Path,
    monkeypatch,
) -> None:
    baseline, _published, draft = _isolate_layout(tmp_path, monkeypatch)
    revision = _append_layout_zone(baseline, feature_id="zone-f2", area_code="F2")
    engine, factory = _database(tmp_path, revision=revision)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == "p1-139-admin"))
            floor = db.scalar(select(WarehouseFloor))
            assert admin is not None and floor is not None
            second_area = WarehouseArea(
                floor_id=floor.id,
                area_code="F2",
                area_name="三楼第二待归档区",
                planned_location_count=1,
                planned_pallet_capacity=1,
                construction_status="enabled",
                capacity_review_status="pending",
                capacity_eligible=False,
            )
            db.add(second_area)
            db.flush()
            second_policy = WarehouseAreaStoragePolicy(
                area_id=second_area.id,
                map_feature_id="zone-f2",
                allowed_inventory_types_json=json.dumps(["finished"]),
                storage_layout="pallet_ground",
                status="published",
                published_map_revision=revision,
                version=1,
                updated_by=admin.id,
            )
            second_location = WarehouseLocation(
                location_code="F2-G01",
                location_name="三楼第二待归档区第1位",
                warehouse_type="finished",
                is_active=True,
                warehouse_floor=3,
                area_code="F2",
                storage_type="ground",
                address_kind="ground_slot",
                address_area_id=second_area.id,
                ground_row_no=1,
                slot_no=1,
                placement_status="placed",
            )
            db.add_all([second_policy, second_location])
            db.commit()

            first = _archive(db, admin, revision=revision, operation_key="p1-139-shared-key")
            with pytest.raises(HTTPException, match="操作键已被其他区域") as caught:
                warehouse_api.delete_twin_layout_feature(
                    "3F",
                    "zone-f2",
                    expected_revision=first["revision"],
                    expected_version=1,
                    operation_key="p1-139-shared-key",
                    expected_policy_version=1,
                    expected_published_revision=revision,
                    request=_request(),
                    db=db,
                    user=admin,
                )
            assert caught.value.status_code == 409
            db.expire_all()
            second_policy = db.scalar(
                select(WarehouseAreaStoragePolicy).where(
                    WarehouseAreaStoragePolicy.map_feature_id == "zone-f2"
                )
            )
            second_area = db.scalar(
                select(WarehouseArea).where(WarehouseArea.area_code == "F2")
            )
            second_location = db.scalar(
                select(WarehouseLocation).where(WarehouseLocation.location_code == "F2-G01")
            )
            assert second_policy is not None and second_policy.status == "published"
            assert second_area is not None and second_area.construction_status == "enabled"
            assert second_location is not None and second_location.is_active is True
            assert db.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.action == "TWIN_LAYOUT_FORMAL_AREA_ARCHIVE"
                )
            ) == 1
            assert not draft.exists()
            raw_floor = json.loads(baseline.read_text(encoding="utf-8"))["floors"]["3F"]
            operational_floor = overlay_formal_area_bindings(
                db,
                floor_code="3F",
                floor_layout=raw_floor,
                include_draft=False,
            )
            assert [row["id"] for row in operational_floor["features"]] == [
                "zone-f2"
            ]
    finally:
        engine.dispose()


@pytest.mark.parametrize("with_inventory", [False, True])
def test_policyless_legacy_formal_area_never_uses_pure_draft_delete(
    tmp_path: Path,
    monkeypatch,
    with_inventory: bool,
) -> None:
    baseline, _published, draft = _isolate_layout(tmp_path, monkeypatch)
    revision = json.loads(baseline.read_text(encoding="utf-8"))["floors"]["3F"]["revision"]
    engine, factory = _database(tmp_path, revision=revision)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == "p1-139-admin"))
            policy = db.scalar(select(WarehouseAreaStoragePolicy))
            location = db.scalar(select(WarehouseLocation))
            assert admin is not None and policy is not None and location is not None
            db.delete(policy)
            if with_inventory:
                db.add(
                    InventoryLot(
                        lot_number="P1-139-LEGACY-LOT",
                        inventory_type="finished",
                        warehouse_location_id=location.id,
                        quantity_available=7,
                        quantity_reserved=0,
                        quantity_consumed=0,
                        quantity_damaged=0,
                        quantity_scrapped=0,
                        unit="boxes",
                        status="active",
                        source_type="manual",
                        stock_date=date(2026, 9, 1),
                        stock_date_accuracy="exact",
                        last_movement_at=datetime(2026, 9, 1, 8, 0),
                        version=1,
                    )
                )
            db.commit()

            with pytest.raises(HTTPException, match="旧版实测区域") as caught:
                warehouse_api.delete_twin_layout_feature(
                    "3F",
                    "zone-f1",
                    expected_revision=revision,
                    expected_version=1,
                    operation_key=f"p1-139-legacy-{with_inventory}",
                    request=_request(),
                    db=db,
                    user=admin,
                )
            assert caught.value.status_code == 409
            db.expire_all()
            assert db.scalar(select(WarehouseArea.construction_status)) == "enabled"
            assert db.scalar(select(WarehouseLocation.is_active)) is True
            assert db.scalar(select(func.sum(InventoryLot.quantity_available))) == (
                7 if with_inventory else None
            )
            assert db.scalar(select(func.count(OperationLog.id))) == 0
            assert not draft.exists()
    finally:
        engine.dispose()


def test_truly_unbound_unlocked_draft_zone_can_still_be_deleted(
    tmp_path: Path,
    monkeypatch,
) -> None:
    baseline, _published, draft = _isolate_layout(tmp_path, monkeypatch)
    payload = json.loads(baseline.read_text(encoding="utf-8"))
    floor = payload["floors"]["3F"]
    floor["features"][0]["is_locked"] = False
    floor["revision"] = _floor_revision(floor)
    baseline.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    engine = create_sqlite_engine(tmp_path / "p1-139-draft-only.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as db:
            admin = User(
                username="p1-139-draft-admin",
                password_hash="test-only",
                role="admin",
                real_name="P1-139 草稿管理员",
                is_active=True,
                must_change_password=False,
                customer_access_mode="all",
                ui_mode="standard",
            )
            db.add(admin)
            db.commit()
            result = warehouse_api.delete_twin_layout_feature(
                "3F",
                "zone-f1",
                expected_revision=floor["revision"],
                expected_version=1,
                operation_key="p1-139-pure-draft",
                request=_request(),
                db=db,
                user=admin,
            )
            assert result["applied"] is True
            assert json.loads(draft.read_text(encoding="utf-8"))["floors"]["3F"]["features"] == []
            assert db.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.action == "TWIN_LAYOUT_FEATURE_DELETE"
                )
            ) == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize("blocker", ["lot", "pallet"])
def test_inventory_or_current_pallet_blocks_archive_without_changes(
    tmp_path: Path,
    monkeypatch,
    blocker: str,
) -> None:
    baseline, _published, draft = _isolate_layout(tmp_path, monkeypatch)
    revision = json.loads(baseline.read_text(encoding="utf-8"))["floors"]["3F"]["revision"]
    engine, factory = _database(tmp_path, revision=revision)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == "p1-139-admin"))
            location = db.scalar(select(WarehouseLocation))
            assert admin is not None and location is not None
            if blocker == "lot":
                db.add(
                    InventoryLot(
                        lot_number="P1-139-LOT-001",
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
                        stock_date=date(2026, 9, 1),
                        stock_date_accuracy="exact",
                        last_movement_at=datetime(2026, 9, 1, 8, 0),
                        version=1,
                    )
                )
            else:
                db.add(
                    InventoryPallet(
                        pallet_code="P1-139-PALLET-001",
                        location_id=location.id,
                        status="active",
                        is_current=True,
                        version=1,
                    )
                )
            db.commit()

            with pytest.raises(HTTPException, match="不能删除") as caught:
                _archive(db, admin, revision=revision)
            assert caught.value.status_code == 409
            db.expire_all()
            assert db.scalar(select(WarehouseAreaStoragePolicy.status)) == "published"
            assert db.scalar(select(WarehouseArea.construction_status)) == "enabled"
            assert db.scalar(select(WarehouseLocation.is_active)) is True
            assert not draft.exists()
    finally:
        engine.dispose()


def test_archive_rejects_stale_versions_and_different_replay_key(
    tmp_path: Path,
    monkeypatch,
) -> None:
    baseline, _published, _draft = _isolate_layout(tmp_path, monkeypatch)
    revision = json.loads(baseline.read_text(encoding="utf-8"))["floors"]["3F"]["revision"]
    engine, factory = _database(tmp_path, revision=revision)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == "p1-139-admin"))
            assert admin is not None
            with pytest.raises(HTTPException, match="版本已变化"):
                warehouse_api.delete_twin_layout_feature(
                    "3F",
                    "zone-f1",
                    expected_revision=revision,
                    expected_version=1,
                    operation_key="p1-139-stale-version",
                    expected_policy_version=9,
                    expected_published_revision=revision,
                    request=_request(),
                    db=db,
                    user=admin,
                )
            _archive(db, admin, revision=revision)
            with pytest.raises(HTTPException, match="归档"):
                _archive(
                    db,
                    admin,
                    revision=revision,
                    operation_key="p1-139-different-operation",
                )
    finally:
        engine.dispose()


def test_optional_customer_default_area_reference_blocks_archive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    baseline, _published, draft = _isolate_layout(tmp_path, monkeypatch)
    revision = json.loads(baseline.read_text(encoding="utf-8"))["floors"]["3F"]["revision"]
    engine, factory = _database(tmp_path, revision=revision)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == "p1-139-admin"))
            area = db.scalar(select(WarehouseArea))
            assert admin is not None and area is not None
            customer = Customer(name="P1-139 默认区域客户")
            db.add(customer)
            db.flush()
            db.add(
                CustomerFinishedStoragePreference(
                    customer_id=customer.id,
                    warehouse_area_id=area.id,
                    priority=1,
                    created_by=admin.id,
                )
            )
            db.commit()
            with pytest.raises(HTTPException, match="客户默认成品区域") as caught:
                _archive(db, admin, revision=revision)
            assert caught.value.status_code == 409
            db.expire_all()
            assert db.scalar(select(WarehouseAreaStoragePolicy.status)) == "published"
            assert db.scalar(select(WarehouseLocation.is_active)) is True
            assert not draft.exists()
    finally:
        engine.dispose()


def test_archived_policy_hides_immutable_geometry_from_operational_overlay(
    tmp_path: Path,
) -> None:
    path = _layout(tmp_path / "overlay.json", with_rack=True)
    floor_layout = json.loads(path.read_text(encoding="utf-8"))["floors"]["3F"]
    floor_layout["pallets"] = [
        {
            "id": "pallet-f1-01",
            "pallet_code": "F1-01",
            "zone_id": "zone-f1",
            "zone_code": "ZONE-3F-ERP-F1",
            "x_mm": 500,
            "y_mm": 500,
        }
    ]
    revision = floor_layout["revision"]
    engine, factory = _database(tmp_path, revision=revision)
    try:
        with factory() as db:
            policy = db.scalar(select(WarehouseAreaStoragePolicy))
            admin = db.scalar(select(User))
            assert policy is not None and admin is not None
            policy.status = "archived"
            policy.archived_at = datetime(2026, 9, 1, 8, 0)
            policy.archived_by = admin.id
            policy.archive_operation_key = "p1-139-overlay-archive"
            policy.archive_request_hash = "a" * 64
            policy.archive_feature_snapshot_json = "{}"
            db.commit()
            overlaid = overlay_formal_area_bindings(
                db,
                floor_code="3F",
                floor_layout=floor_layout,
                include_draft=True,
            )
            assert overlaid["features"] == []
            assert overlaid["racks"] == []
            assert overlaid["pallets"] == []
    finally:
        engine.dispose()


def test_area_context_menu_and_existing_button_share_archive_flow() -> None:
    assert 'renderer.domElement.addEventListener("contextmenu", onContextMenu)' in CANVAS_SOURCE
    assert "onFeatureContextMenu={locationEditMode" in TWIN_SOURCE
    assert 'className="twin-feature-context-menu"' in TWIN_SOURCE
    assert "void deleteLayoutFeature(feature)" in TWIN_SOURCE
    assert "deleteSelectedLayoutFeature = () => deleteLayoutFeature" in TWIN_SOURCE
    assert 'query.set("expected_policy_version"' in TWIN_SOURCE
    assert 'query.set("expected_published_revision"' in TWIN_SOURCE
    assert 'query.set("retire_ground_plan", "true")' in TWIN_SOURCE
    assert 'query.set("expected_ground_plan_version"' in TWIN_SOURCE
    assert "removeZoneHierarchy(current, feature.id, feature.erp_area_code)" in TWIN_SOURCE
    assert "await Promise.all([refreshPlanningTwinFloor(), refreshDashboard()])" in TWIN_SOURCE
    assert 'locationEditMode && layoutMapToolsOpen && layoutMapTool === "adjust"' in TWIN_SOURCE
    route = next(
        route
        for route in warehouse_api.router.routes
        if route.path == "/twin-layout/floors/{floor_code}/features/{feature_id}"
        and "DELETE" in route.methods
    )
    assert any(
        dependency.call is warehouse_api.admin_only
        for dependency in route.dependant.dependencies
    )


def test_editor_archive_path_retires_empty_racks_but_normal_delete_still_blocks(
    tmp_path: Path,
) -> None:
    path = _layout(tmp_path / "editor.json", with_rack=True)
    revision = json.loads(path.read_text(encoding="utf-8"))["floors"]["3F"]["revision"]
    with pytest.raises(WarehouseTwinLayoutEditConflictError, match="锁定"):
        editor.delete_warehouse_twin_feature(
            "3F",
            "zone-f1",
            expected_revision=revision,
            expected_version=1,
            operation_key="p1-139-normal-delete",
            path=path,
        )
    archived = editor.delete_warehouse_twin_feature(
        "3F",
        "zone-f1",
        expected_revision=revision,
        expected_version=1,
        operation_key="p1-139-archive-delete",
        archive_empty_children=True,
        path=path,
    )
    assert archived.applied is True
    assert archived.value["archived_child_rack_count"] == 1
