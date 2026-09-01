from __future__ import annotations

import json
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
from app.models.user import User
from app.models.warehouse_inventory import (
    InventoryLot,
    InventoryPallet,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseGroundLayoutPlan,
    WarehouseLocation,
)
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
            placement_status="placed",
        )
        db.add_all([policy, location])
        db.commit()
    return engine, factory


def _archive(db, admin: User, *, revision: str, operation_key: str = "p1-139-archive-zone-f1"):
    return warehouse_api.delete_twin_layout_feature(
        "3F",
        "zone-f1",
        expected_revision=revision,
        expected_version=1,
        operation_key=operation_key,
        expected_policy_version=1,
        expected_published_revision=revision,
        request=_request(),
        db=db,
        user=admin,
    )


def test_empty_published_area_archives_without_inventory_write_and_replays(
    tmp_path: Path,
    monkeypatch,
) -> None:
    baseline, published, draft = _isolate_layout(tmp_path, monkeypatch, with_rack=True)
    revision = json.loads(baseline.read_text(encoding="utf-8"))["floors"]["3F"]["revision"]
    engine, factory = _database(tmp_path, revision=revision)
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.username == "p1-139-admin"))
            area = db.scalar(select(WarehouseArea))
            assert admin is not None and area is not None
            db.add(
                WarehouseGroundLayoutPlan(
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
            )
            db.commit()
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
            assert json.loads(policy.archive_feature_snapshot_json)["feature"]["id"] == "zone-f1"
            assert area.construction_status == "archived"
            assert area.capacity_review_status == "excluded"
            assert location.is_active is False
            ground_plan = db.scalar(select(WarehouseGroundLayoutPlan))
            assert ground_plan is not None
            assert (ground_plan.status, ground_plan.version) == ("published", 1)
            assert db.scalar(select(func.count(InventoryLot.id))) == 0
            assert db.scalar(select(func.count(InventoryPallet.id))) == 0
            assert db.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.action == "TWIN_LAYOUT_FORMAL_AREA_ARCHIVE"
                )
            ) == 1

            draft_floor = json.loads(draft.read_text(encoding="utf-8"))["floors"]["3F"]
            assert not draft_floor["features"]
            assert not draft_floor["racks"]
            assert draft_floor["retired_racks"][0]["id"] == "rack-f1-01"
            assert not published.exists()

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
            with pytest.raises(HTTPException, match="已经归档"):
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
            db.execute(
                text(
                    "CREATE TABLE customer_finished_storage_area_preferences ("
                    "id INTEGER PRIMARY KEY, warehouse_area_id INTEGER NOT NULL)"
                )
            )
            db.execute(
                text(
                    "INSERT INTO customer_finished_storage_area_preferences "
                    "(id, warehouse_area_id) VALUES (1, :area_id)"
                ),
                {"area_id": area.id},
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
    path = _layout(tmp_path / "overlay.json")
    floor_layout = json.loads(path.read_text(encoding="utf-8"))["floors"]["3F"]
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
