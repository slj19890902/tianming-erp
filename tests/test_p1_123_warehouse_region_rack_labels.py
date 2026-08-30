from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import get_db
from app.core.database import create_sqlite_engine
from app.core.security import hash_password
from app.models import Base
from app.models.user import User
from app.models.warehouse_inventory import (
    InventoryPallet,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseLocation,
    WarehouseRackLevelLabelPrintJob,
)
from app.services.warehouse_location_address import employee_location_name
from app.services.warehouse_rack_cells import (
    WarehouseRackCellSyncError,
    sync_published_rack_cells,
)


ROOT = Path(__file__).resolve().parents[1]


def _layout(*, name: str = "聚晟达成品架", counts: list[int] | None = None) -> dict:
    normalized_counts = counts or [2, 3]
    return {
        "floor_code": "3F",
        "revision": "rack-rev-1",
        "bounds_mm": {"min_x": 0, "min_y": 0, "max_x": 10000, "max_y": 10000},
        "features": [
            {
                "id": "zone-fin-001",
                "feature_code": "ZONE-3F-FIN-001",
                "feature_kind": "zone",
                "name": "右区成品架",
            }
        ],
        "racks": [
            {
                "id": "rack-fin-001-01",
                "rack_code": "R01",
                "name": name,
                "area_feature_id": "zone-fin-001",
                "x_mm": 5000,
                "y_mm": 5000,
                "width_mm": 3000,
                "depth_mm": 1000,
                "height_mm": 2200,
                "rotation_deg": 0,
                "levels": len(normalized_counts),
                "level_cell_counts": normalized_counts,
            }
        ],
    }


@pytest.fixture()
def rack_factory(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "p1-123.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        user = User(
            username="p1-123-admin",
            password_hash=hash_password("123456"),
            role="admin",
            real_name="仓库测试管理员",
            must_change_password=False,
        )
        floor = WarehouseFloor(
            floor_code="3F",
            floor_name="三楼成品仓",
            floor_number=3,
            construction_status="enabled",
            planning_reference_pallet_capacity=0,
        )
        db.add_all([user, floor])
        db.flush()
        area = WarehouseArea(
            floor_id=floor.id,
            area_code="FIN-001",
            area_name="右区客户成品区",
            planned_location_count=1,
            planned_pallet_capacity=0,
            construction_status="enabled",
            capacity_review_status="pending",
            capacity_eligible=False,
        )
        db.add(area)
        db.flush()
        area.storage_policy = WarehouseAreaStoragePolicy(
            map_feature_id="zone-fin-001",
            allowed_inventory_types_json=json.dumps(["finished"]),
            storage_layout="rack",
            status="published",
            draft_map_revision="rack-rev-1",
            published_map_revision="rack-rev-1",
            version=1,
            updated_by=user.id,
        )
        db.add(
            WarehouseLocation(
                location_code="3F-FIN-001-PLAN-01",
                location_name="旧规划货位",
                warehouse_type="finished",
                is_active=True,
                warehouse_floor=3,
                area_code="FIN-001",
                storage_type="rack",
                sort_order=1,
                is_temporary=False,
                source_version="CURRENT_MAP",
                address_kind="legacy",
                address_area_id=area.id,
                address_version=1,
                placement_status="placed",
            )
        )
        db.commit()
    try:
        yield factory
    finally:
        engine.dispose()


def test_published_rack_cells_keep_stable_ids_and_block_occupied_shrink(
    rack_factory,
) -> None:
    with rack_factory() as db:
        first = sync_published_rack_cells(db, floor_layout=_layout(), operator_id=1)
        db.commit()
        assert len(first.created_location_ids) == 5
        rows = list(
            db.scalars(
                select(WarehouseLocation)
                .where(WarehouseLocation.map_rack_id == "rack-fin-001-01")
                .order_by(WarehouseLocation.level_no, WarehouseLocation.slot_no)
            ).all()
        )
        original_ids = [row.id for row in rows]
        assert [row.is_active for row in rows] == [True] * 5
        assert db.scalar(
            select(WarehouseLocation).where(
                WarehouseLocation.location_code == "3F-FIN-001-PLAN-01"
            )
        ).is_active is False
        assert "聚晟达成品架" in employee_location_name(rows[0])
        assert "·A架·" not in employee_location_name(rows[0])

        occupied = rows[-1]
        db.add(
            InventoryPallet(
                pallet_code="P1-123-OCCUPIED",
                location_id=occupied.id,
                status="active",
                is_current=True,
                version=1,
            )
        )
        db.commit()
        with pytest.raises(WarehouseRackCellSyncError, match="仍有货"):
            sync_published_rack_cells(
                db, floor_layout=_layout(counts=[2, 2]), operator_id=1
            )
        db.rollback()

        pallet = db.scalar(
            select(InventoryPallet).where(
                InventoryPallet.pallet_code == "P1-123-OCCUPIED"
            )
        )
        pallet.is_current = False
        pallet.status = "closed"
        db.commit()
        reduced = sync_published_rack_cells(
            db,
            floor_layout=_layout(name="聚晟达成品架（南侧）", counts=[2, 2]),
            operator_id=1,
        )
        db.commit()
        assert occupied.id in reduced.disabled_location_ids
        active = list(
            db.scalars(
                select(WarehouseLocation)
                .where(
                    WarehouseLocation.map_rack_id == "rack-fin-001-01",
                    WarehouseLocation.is_active.is_(True),
                )
                .order_by(WarehouseLocation.level_no, WarehouseLocation.slot_no)
            ).all()
        )
        assert [row.id for row in active] == original_ids[:4]
        assert all(row.rack_display_name == "聚晟达成品架（南侧）" for row in active)


def test_unpublished_visual_rack_does_not_create_or_block_stock_cells(
    rack_factory,
) -> None:
    layout = _layout()
    layout["racks"].append(
        {
            **layout["racks"][0],
            "id": "rack-visual-planning-01",
            "rack_code": "V01",
            "name": "待确认规划架",
            "area_feature_id": "zone-not-published",
        }
    )
    with rack_factory() as db:
        result = sync_published_rack_cells(db, floor_layout=layout, operator_id=1)
        db.commit()
        assert len(result.created_location_ids) == 5
        assert db.scalar(
            select(WarehouseLocation).where(
                WarehouseLocation.map_rack_id == "rack-visual-planning-01"
            )
        ) is None


def test_rack_level_print_is_admin_only_idempotent_and_snapshot_based(
    rack_factory,
    monkeypatch,
) -> None:
    from app.api import warehouse as warehouse_api
    from app.api.auth import router as auth_router

    with rack_factory() as db:
        sync_published_rack_cells(
            db, floor_layout=_layout(counts=[3, 3, 3]), operator_id=1
        )
        db.commit()
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(warehouse_api.router, prefix="/api/warehouse")

    def override_get_db():
        with rack_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(
        warehouse_api,
        "load_warehouse_twin_floor",
        lambda _code: _layout(counts=[3, 3, 3]),
    )
    body = {
        "floor_code": "3F",
        "map_rack_id": "rack-fin-001-01",
        "expected_map_revision": "rack-rev-1",
        "template_version": "rack_level_80x40_v1",
        "source": "region_planning",
        "idempotency_key": "p1-123-label-print-0001",
    }
    with TestClient(app) as client:
        denied = client.post("/api/warehouse/rack-level-labels/prints", json=body)
        assert denied.status_code == 401
        login = client.post(
            "/api/auth/login",
            json={"username": "p1-123-admin", "password": "123456"},
        )
        assert login.status_code == 200, login.text
        created = client.post("/api/warehouse/rack-level-labels/prints", json=body)
        assert created.status_code == 200, created.text
        payload = created.json()
        assert payload["level_count"] == 3
        assert [item["level_no"] for item in payload["labels"]] == [1, 2, 3]
        repeated = client.post("/api/warehouse/rack-level-labels/prints", json=body)
        assert repeated.status_code == 200
        assert repeated.json()["id"] == payload["id"]
        assert repeated.json()["replayed"] is True
        detail = client.get(
            f"/api/warehouse/rack-level-labels/prints/{payload['id']}"
        )
        assert detail.status_code == 200
        assert detail.json()["area_name"] == "右区客户成品区"
    with rack_factory() as db:
        assert len(db.scalars(select(WarehouseRackLevelLabelPrintJob)).all()) == 1
        assert len(
            db.scalars(
                select(WarehouseLocation).where(
                    WarehouseLocation.map_rack_id == "rack-fin-001-01",
                    WarehouseLocation.is_active.is_(True),
                )
            ).all()
        ) == 9


def test_region_planning_ui_and_label_page_are_explicit() -> None:
    source = (ROOT / "factory_twin/frontend/src/WarehouseTwinApp.tsx").read_text(
        encoding="utf-8"
    )
    label = (ROOT / "static/warehouse-rack-level-label.html").read_text(
        encoding="utf-8"
    )
    for text in (">编辑</button>", ">货位/货架</button>", ">发布</button>"):
        assert text in source
    for floor in ("2F", "4F", "5F"):
        assert f'<b>{floor}</b>' in source
    assert "规划中" in source
    assert "rack_level_80x40_v1" in source
    assert "@page{size:80mm 40mm;margin:0}" in label
    assert "内部" not in label
