from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

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
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseLocation,
)
from app.services.location_candidates import list_operational_locations
from app.services.warehouse_area_activation import publish_floor_area_policies


@pytest.fixture()
def routing_app(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "p1-48a.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="p1-48a-admin",
            password_hash=hash_password("123456"),
            role="admin",
            real_name="P1-48A 管理员",
            is_active=True,
            must_change_password=False,
            customer_access_mode="all",
            ui_mode="standard",
        )
        workshop = User(
            username="p1-48a-workshop",
            password_hash=hash_password("123456"),
            role="workshop",
            real_name="P1-48A 生产员工",
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
        db.add_all([admin, workshop, floor])
        db.flush()
        dynamic = WarehouseArea(
            floor_id=floor.id,
            area_code="FG-004",
            area_name="三楼动态成品区",
            planned_location_count=0,
            planned_pallet_capacity=0,
            construction_status="layout_building",
            capacity_review_status="pending",
            capacity_eligible=False,
        )
        old = WarehouseArea(
            floor_id=floor.id,
            area_code="A1",
            area_name="三楼既有 A1 区",
            planned_location_count=1,
            planned_pallet_capacity=0,
            construction_status="enabled",
            capacity_review_status="pending",
            capacity_eligible=False,
        )
        db.add_all([dynamic, old])
        db.flush()
        policy = WarehouseAreaStoragePolicy(
            area_id=dynamic.id,
            map_feature_id="zone-3f-fg-004",
            allowed_inventory_types_json=json.dumps(["finished"]),
            storage_layout="pallet_ground",
            status="draft",
            version=1,
            updated_by=admin.id,
        )
        old_policy = WarehouseAreaStoragePolicy(
            area_id=old.id,
            map_feature_id="zone-3f-a1",
            allowed_inventory_types_json=json.dumps(["finished"]),
            storage_layout="pallet_ground",
            status="published",
            published_map_revision="old-v11",
            version=1,
            updated_by=admin.id,
        )
        old_location = WarehouseLocation(
            location_code="A1-L001",
            location_name="A1 既有 001 号位",
            warehouse_type="finished",
            is_active=True,
            warehouse_floor=3,
            area_code="A1",
            storage_type="ground",
            sort_order=1,
            source_version="V11",
            placement_status="placed",
        )
        old_location.floor3_layout = Floor3LocationLayout(
            left_pct=10,
            top_pct=10,
            width_pct=8,
            height_pct=8,
            z_index=0,
            version=1,
            source_type="manual",
            created_by=admin.id,
            updated_by=admin.id,
        )
        db.add_all([policy, old_policy, old_location])
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(warehouse_router, prefix="/api/warehouse")

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, factory
    finally:
        engine.dispose()


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": "123456"}
    )
    assert response.status_code == 200, response.text


def test_dynamic_floor3_area_uses_formal_lifecycle_and_keeps_location_ids(
    routing_app,
) -> None:
    app, factory = routing_app
    with TestClient(app) as client:
        _login(client, "p1-48a-admin")
        route = client.get(
            "/api/warehouse/spatial-layout/floors/3F/areas/FG-004/management"
        )
        assert route.status_code == 200, route.text
        assert route.json() == {
            "floor_code": "3F",
            "area_code": "FG-004",
            "management_mode": "formal_area",
            "source_version": "CURRENT_MAP",
                "available_actions": [
                    "location_count",
                    "layout",
                    "auto_arrange",
                    "disable_empty",
                    "enable_empty",
                ],
                "policy_version": 1,
                "published_map_revision": None,
                "requires_area_confirmation": False,
            }

        created = client.post(
            "/api/warehouse/spatial-layout/floors/3F/areas/FG-004/location-count",
            json={
                "target_count": 2,
                "confirmed": True,
                "expected_policy_version": 1,
                "expected_layout_versions": {},
            },
        )
        assert created.status_code == 200, created.text
        created_body = created.json()
        assert created_body["management_mode"] == "formal_area"
        assert created_body["created_count"] == 2
        location_ids = [item["location"]["id"] for item in created_body["items"]]
        assert {item["location"]["source_version"] for item in created_body["items"]} == {
            "CURRENT_MAP"
        }
        assert {item["location"]["placement_status"] for item in created_body["items"]} == {
            "unplaced"
        }
        replay = client.post(
            "/api/warehouse/spatial-layout/floors/3F/areas/FG-004/location-count",
            json={
                "target_count": 2,
                "confirmed": True,
                "expected_policy_version": created_body["policy_version"],
                "expected_layout_versions": {
                    str(item["location"]["id"]): item["layout"]["version"]
                    for item in created_body["items"]
                },
            },
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["created_count"] == 0
        assert replay.json()["active_count"] == 2
        with factory() as db:
            assert [
                row.location.id
                for row in list_operational_locations(
                    db, warehouse_types={"finished"}
                )
                if row.location.area_code == "FG-004"
            ] == []

        slots = []
        for item in created_body["items"]:
            layout = item["layout"]
            slots.append(
                {
                    "location_id": item["location"]["id"],
                    "expected_version": layout["version"],
                    "left_pct": layout["left_pct"],
                    "top_pct": layout["top_pct"],
                    "width_pct": layout["width_pct"],
                    "height_pct": layout["height_pct"],
                    "z_index": layout["z_index"],
                }
            )
        layout_response = client.patch(
            "/api/warehouse/spatial-layout/floors/3F/areas/FG-004",
            json={
                "expected_policy_version": created_body["policy_version"],
                "slots": slots,
            },
        )
        assert layout_response.status_code == 200, layout_response.text
        first_version = layout_response.json()["items"][0]["version"]
        disabled = client.post(
            f"/api/warehouse/spatial-layout/locations/{location_ids[0]}/disable",
            json={
                "expected_version": first_version,
                "expected_policy_version": layout_response.json()["policy_version"],
            },
        )
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["location"]["id"] == location_ids[0]
        assert disabled.json()["location"]["is_active"] is False
        enabled = client.post(
            f"/api/warehouse/spatial-layout/locations/{location_ids[0]}/enable",
            json={
                "expected_version": disabled.json()["layout"]["version"],
                "expected_policy_version": disabled.json()["policy_version"],
            },
        )
        assert enabled.status_code == 200, enabled.text
        assert enabled.json()["location"]["id"] == location_ids[0]

        with factory() as db:
            policy = db.scalar(
                select(WarehouseAreaStoragePolicy).where(
                    WarehouseAreaStoragePolicy.map_feature_id == "zone-3f-fg-004"
                )
            )
            assert policy is not None
            published = publish_floor_area_policies(
                db,
                floor_code="3F",
                published_revision="p1-48a-revision",
                operator_id=1,
                published_features=[
                    {
                        "id": "zone-3f-fg-004",
                        "feature_kind": "zone",
                        "erp_area_code": "FG-004",
                        "allowed_inventory_types": ["finished"],
                        "storage_layout": "pallet_ground",
                    }
                ],
            )
            db.commit()
            assert published == [policy]
            candidates = list_operational_locations(
                db, warehouse_types={"finished"}, empty_only=True
            )
            assert [row.location.id for row in candidates if row.location.area_code == "FG-004"] == location_ids


def test_old_floor3_area_stays_v11_and_unified_endpoint_delegates(
    routing_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, factory = routing_app
    monkeypatch.setattr(
        warehouse_api,
        "load_warehouse_twin_floor",
        lambda _floor_code: {
            "floor_code": "3F",
            "revision": "old-v11",
            "bounds_mm": {"min_x": 0, "min_y": 0, "max_x": 10_000, "max_y": 10_000},
            "structures": [],
            "placements": [],
            "racks": [],
            "features": [
                {
                    "id": "zone-3f-a1",
                    "feature_kind": "zone",
                    "feature_code": "ZONE-3F-ERP-A1",
                    "points": [[0, 0], [10_000, 0], [10_000, 10_000], [0, 10_000]],
                }
            ],
        },
    )
    with factory() as db:
        old_location = db.scalar(
            select(WarehouseLocation).where(WarehouseLocation.area_code == "A1")
        )
        assert old_location is not None and old_location.floor3_layout is not None
        old_snapshot = {str(old_location.id): old_location.floor3_layout.version}
    with TestClient(app) as client:
        _login(client, "p1-48a-admin")
        route = client.get(
            "/api/warehouse/spatial-layout/floors/3F/areas/A1/management"
        )
        assert route.status_code == 200, route.text
        assert route.json()["management_mode"] == "floor3_v11"
        grown = client.post(
            "/api/warehouse/spatial-layout/floors/3F/areas/A1/location-count",
            json={
                "target_count": 2,
                "confirmed": True,
                "expected_map_revision": "old-v11",
                "expected_policy_version": route.json()["policy_version"],
                "expected_layout_versions": old_snapshot,
            },
        )
        assert grown.status_code == 200, grown.text
        assert grown.json()["management_mode"] == "floor3_v11"
        with factory() as db:
            assert db.scalar(
                select(func.count(WarehouseLocation.id)).where(
                    WarehouseLocation.area_code == "A1",
                    WarehouseLocation.source_version == "V11",
                    WarehouseLocation.is_active.is_(True),
                )
            ) == 2


def test_published_v11_area_auto_count_and_manual_layout_share_one_safe_contract(
    routing_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, factory = routing_app
    measured_layout = {
        "floor_code": "3F",
        "revision": "old-v11",
        "bounds_mm": {"min_x": 0, "min_y": 0, "max_x": 10_000, "max_y": 10_000},
        "structures": [],
        "placements": [],
        "racks": [],
        "features": [
            {
                "id": "zone-3f-a1",
                "feature_kind": "zone",
                "feature_code": "ZONE-3F-ERP-A1",
                "points": [[0, 0], [10_000, 0], [10_000, 10_000], [0, 10_000]],
            }
        ],
    }
    monkeypatch.setattr(
        warehouse_api,
        "load_warehouse_twin_floor",
        lambda _floor_code: measured_layout,
    )

    with TestClient(app) as client:
        _login(client, "p1-48a-admin")
        management = client.get(
            "/api/warehouse/spatial-layout/floors/3F/areas/A1/management"
        ).json()
        with factory() as db:
            location = db.scalar(
                select(WarehouseLocation).where(
                    WarehouseLocation.area_code == "A1",
                    WarehouseLocation.source_version == "V11",
                )
            )
            assert location is not None and location.floor3_layout is not None
            location_id = location.id
            version = location.floor3_layout.version

        arranged = client.post(
            "/api/warehouse/spatial-layout/floors/3F/areas/A1/auto-arrange",
            json={
                "confirmed": True,
                "adopt_historical_layouts": True,
                "expected_map_revision": management["published_map_revision"],
                "expected_policy_version": management["policy_version"],
                "expected_layout_versions": {str(location_id): version},
            },
        )
        assert arranged.status_code == 200, arranged.text
        assert arranged.json()["auto_arranged_count"] == 1
        assert arranged.json()["historical_adopted_count"] == 1
        arranged_layout = arranged.json()["items"][0]
        assert arranged_layout["layout_kind"] == "physical_pallet"

        stale = client.post(
            "/api/warehouse/spatial-layout/floors/3F/areas/A1/auto-arrange",
            json={
                "confirmed": True,
                "expected_map_revision": "old-v11",
                "expected_policy_version": 1,
                "expected_layout_versions": {str(location_id): version},
            },
        )
        assert stale.status_code == 409

        manual = client.patch(
            "/api/warehouse/spatial-layout/floors/3F/areas/A1",
            json={
                "expected_map_revision": "old-v11",
                "expected_policy_version": arranged.json()["policy_version"],
                "slots": [
                    {
                        "location_id": location_id,
                        "expected_version": arranged_layout["version"],
                        "left_pct": 5,
                        "top_pct": 5,
                        "width_pct": arranged_layout["width_pct"],
                        "height_pct": arranged_layout["height_pct"],
                        "z_index": 0,
                    }
                ],
            },
        )
        assert manual.status_code == 200, manual.text
        manual_layout = manual.json()["items"][0]
        assert manual_layout["source_type"] == "manual"
        assert manual_layout["version"] == arranged_layout["version"] + 1

        grown = client.post(
            "/api/warehouse/spatial-layout/floors/3F/areas/A1/location-count",
            json={
                "target_count": 2,
                "confirmed": True,
                "expected_map_revision": "old-v11",
                "expected_policy_version": manual.json()["policy_version"],
                "expected_layout_versions": {
                    str(location_id): manual_layout["version"]
                },
            },
        )
        assert grown.status_code == 200, grown.text
        assert grown.json()["created_count"] == 1
        assert grown.json()["active_count"] == 2

        with factory() as db:
            sources = set(
                db.scalars(
                    select(WarehouseLocation.source_version).where(
                        WarehouseLocation.area_code == "A1"
                    )
                ).all()
            )
            assert sources == {"V11"}
            manual_log = db.scalar(
                select(OperationLog)
                .where(OperationLog.entity_id == location_id)
                .order_by(OperationLog.id.desc())
            )
            assert manual_log is not None
            details = json.loads(manual_log.details)
            assert details["before"]["version"] == arranged_layout["version"]
            assert details["after"]["version"] == manual_layout["version"]

        _login(client, "p1-48a-workshop")
        forbidden = client.post(
            "/api/warehouse/spatial-layout/floors/3F/areas/A1/auto-arrange",
            json={
                "confirmed": True,
                "expected_map_revision": "old-v11",
                "expected_policy_version": 1,
                "expected_layout_versions": {
                    str(location_id): manual_layout["version"]
                },
            },
        )
        assert forbidden.status_code == 403


def test_dynamic_floor3_raw_policy_creates_shared_pallet_location(
    routing_app,
) -> None:
    app, factory = routing_app
    with factory() as db:
        floor = db.scalar(select(WarehouseFloor).where(WarehouseFloor.floor_code == "3F"))
        admin = db.scalar(select(User).where(User.username == "p1-48a-admin"))
        assert floor is not None and admin is not None
        area = WarehouseArea(
            floor_id=floor.id,
            area_code="RAW-001",
            area_name="三楼动态原料区",
            planned_location_count=0,
            planned_pallet_capacity=0,
            construction_status="layout_building",
            capacity_review_status="pending",
            capacity_eligible=False,
        )
        db.add(area)
        db.flush()
        db.add(
            WarehouseAreaStoragePolicy(
                area_id=area.id,
                map_feature_id="zone-3f-raw-001",
                allowed_inventory_types_json=json.dumps(["raw_material"]),
                storage_layout="pallet_ground",
                status="draft",
                version=1,
                updated_by=admin.id,
            )
        )
        db.commit()
    with TestClient(app) as client:
        _login(client, "p1-48a-admin")
        response = client.post(
            "/api/warehouse/spatial-layout/floors/3F/areas/RAW-001/location-count",
            json={
                "target_count": 1,
                "confirmed": True,
                "expected_policy_version": 1,
                "expected_layout_versions": {},
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["created_count"] == 1
        assert response.json()["items"][0]["location"]["warehouse_type"] == "shared"
    with factory() as db:
        assert db.scalar(
            select(func.count(WarehouseLocation.id)).where(
                WarehouseLocation.area_code == "RAW-001"
            )
        ) == 1


@pytest.mark.parametrize(
    ("area_code", "allowed", "expected_type"),
    [
        ("SEMI-006", ["semi_finished"], "semi_finished"),
        ("SHARED-007", ["finished", "semi_finished"], "shared"),
    ],
)
def test_dynamic_floor3_inventory_policy_controls_candidate_type(
    routing_app,
    area_code: str,
    allowed: list[str],
    expected_type: str,
) -> None:
    app, factory = routing_app
    feature_id = f"zone-3f-{area_code.lower()}"
    with factory() as db:
        floor = db.scalar(select(WarehouseFloor).where(WarehouseFloor.floor_code == "3F"))
        admin = db.scalar(select(User).where(User.username == "p1-48a-admin"))
        assert floor is not None and admin is not None
        area = WarehouseArea(
            floor_id=floor.id,
            area_code=area_code,
            area_name=f"三楼动态 {area_code} 区",
            planned_location_count=0,
            planned_pallet_capacity=0,
            construction_status="layout_building",
            capacity_review_status="pending",
            capacity_eligible=False,
        )
        db.add(area)
        db.flush()
        db.add(
            WarehouseAreaStoragePolicy(
                area_id=area.id,
                map_feature_id=feature_id,
                allowed_inventory_types_json=json.dumps(allowed),
                storage_layout="pallet_ground",
                status="draft",
                version=1,
                updated_by=admin.id,
            )
        )
        db.commit()
    with TestClient(app) as client:
        _login(client, "p1-48a-admin")
        created = client.post(
            f"/api/warehouse/spatial-layout/floors/3F/areas/{area_code}/location-count",
            json={
                "target_count": 1,
                "confirmed": True,
                "expected_policy_version": 1,
                "expected_layout_versions": {},
            },
        )
        assert created.status_code == 200, created.text
        item = created.json()["items"][0]
        location_id = item["location"]["id"]
        layout = item["layout"]
        placed = client.patch(
            f"/api/warehouse/spatial-layout/floors/3F/areas/{area_code}",
            json={
                "expected_policy_version": created.json()["policy_version"],
                "slots": [
                    {
                        "location_id": location_id,
                        "expected_version": layout["version"],
                        "left_pct": layout["left_pct"],
                        "top_pct": layout["top_pct"],
                        "width_pct": layout["width_pct"],
                        "height_pct": layout["height_pct"],
                        "z_index": layout["z_index"],
                    }
                ]
            },
        )
        assert placed.status_code == 200, placed.text
    with factory() as db:
        publish_floor_area_policies(
            db,
            floor_code="3F",
            published_revision=f"p1-48a-{area_code.lower()}",
            operator_id=1,
            published_features=[
                {
                    "id": feature_id,
                    "feature_kind": "zone",
                    "erp_area_code": area_code,
                    "allowed_inventory_types": allowed,
                    "storage_layout": "pallet_ground",
                }
            ],
        )
        db.commit()
        candidates = list_operational_locations(
            db,
            warehouse_types={expected_type},
            empty_only=True,
        )
        assert [row.location.id for row in candidates if row.location.area_code == area_code] == [
            location_id
        ]


def test_nonempty_dynamic_location_cannot_be_disabled(routing_app) -> None:
    app, factory = routing_app
    with TestClient(app) as client:
        _login(client, "p1-48a-admin")
        created = client.post(
            "/api/warehouse/spatial-layout/floors/3F/areas/FG-004/location-count",
            json={
                "target_count": 1,
                "confirmed": True,
                "expected_policy_version": 1,
                "expected_layout_versions": {},
            },
        )
        assert created.status_code == 200, created.text
        item = created.json()["items"][0]
        location_id = item["location"]["id"]
        expected_version = item["layout"]["version"]
    with factory() as db:
        db.add(
            InventoryLot(
                lot_number="P1-48A-LIVE-LOT",
                inventory_type="finished",
                warehouse_location_id=location_id,
                quantity_available=10,
                quantity_reserved=0,
                quantity_consumed=0,
                quantity_damaged=0,
                quantity_scrapped=0,
                unit="boxes",
                status="active",
                source_type="manual",
                stock_date=date(2026, 8, 12),
                stock_date_accuracy="exact",
                last_movement_at=datetime(2026, 8, 12, 8, 0),
                version=1,
            )
        )
        db.commit()
    with TestClient(app) as client:
        _login(client, "p1-48a-admin")
        response = client.post(
            f"/api/warehouse/spatial-layout/locations/{location_id}/disable",
            json={
                "expected_version": expected_version,
                "expected_policy_version": created.json()["policy_version"],
            },
        )
        assert response.status_code == 409, response.text
        assert "库存" in response.text
    with factory() as db:
        assert db.get(WarehouseLocation, location_id).is_active is True


def test_mixed_floor3_sources_fail_closed_without_partial_mutation(routing_app) -> None:
    app, factory = routing_app
    with factory() as db:
        conflict = WarehouseLocation(
            location_code="FG-004-V11-CONFLICT",
            location_name="冲突来源",
            warehouse_type="finished",
            is_active=True,
            warehouse_floor=3,
            area_code="FG-004",
            storage_type="ground",
            sort_order=99,
            source_version="V11",
            placement_status="placed",
        )
        db.add(conflict)
        db.commit()
    with TestClient(app) as client:
        _login(client, "p1-48a-admin")
        response = client.post(
            "/api/warehouse/spatial-layout/floors/3F/areas/FG-004/location-count",
            json={"target_count": 3, "confirmed": True},
        )
        assert response.status_code == 409, response.text
        assert "当前实测三楼区域" in response.text and "核对" in response.text
    with factory() as db:
        assert db.scalar(
            select(func.count(WarehouseLocation.id)).where(
                WarehouseLocation.area_code == "FG-004"
            )
        ) == 1


def test_unversioned_location_in_dynamic_area_is_also_fail_closed(routing_app) -> None:
    app, factory = routing_app
    with factory() as db:
        db.add(
            WarehouseLocation(
                location_code="FG-004-UNVERSIONED",
                location_name="未归属来源",
                warehouse_type="finished",
                is_active=True,
                warehouse_floor=3,
                area_code="FG-004",
                storage_type="ground",
                sort_order=100,
                source_version=None,
                placement_status="placed",
            )
        )
        db.commit()
    with TestClient(app) as client:
        _login(client, "p1-48a-admin")
        response = client.get(
            "/api/warehouse/spatial-layout/floors/3F/areas/FG-004/management"
        )
        assert response.status_code == 409, response.text
        assert "来源冲突" in response.text


def test_management_route_is_admin_only_and_frontend_does_not_guess_by_floor(
    routing_app,
) -> None:
    app, _factory = routing_app
    with TestClient(app) as client:
        _login(client, "p1-48a-workshop")
        forbidden = client.get(
            "/api/warehouse/spatial-layout/floors/3F/areas/FG-004/management"
        )
        assert forbidden.status_code == 403

    source = (
        Path(__file__).resolve().parents[1]
        / "factory_twin"
        / "frontend"
        / "src"
        / "WarehouseTwinApp.tsx"
    ).read_text(encoding="utf-8")
    assert "interface AreaLocationManagement" in source
    assert "/spatial-layout/floors/${encodeURIComponent(floorCode)}/areas/${encodeURIComponent(selectedAreaCode)}/management" in source
    assert 'management.available_actions.includes("layout")' in source
    assert 'areaLocationManagement?.available_actions.includes("location_count")' in source
    assert 'areaLocationManagement?.available_actions.includes("disable_empty")' in source
    assert 'floorCode === "3F"\n          ? `/api/warehouse/floor3/layout/areas/' not in source
    assert '/api/warehouse/floor3/layout/slots/${selectedLocation.location_id}/disable' not in source
