from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.api import warehouse as warehouse_api
from app.api.auth import router as auth_router
from app.api.deps import get_db
from app.api.warehouse import router as warehouse_router
from app.core.database import create_sqlite_engine
from app.core.security import hash_password
from app.models import Base
from app.models.access_control import UserCustomerScope
from app.models.customer import Customer
from app.models.product import Product
from app.models.user import User
from app.models.warehouse_inventory import (
    InventoryLot,
    InventoryPallet,
    InventoryPalletItem,
    InventoryLotTransfer,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseGroundLayoutPlan,
    WarehouseGroundLayoutSlot,
    WarehouseGroundOccupancy,
    WarehouseGroundOccupancySlot,
)
from app.services.warehouse_ground_slots import build_ground_slot_preview


def _measured_layout() -> dict:
    return {
        "floor_code": "3F",
        "revision": "p1-87-map-r1",
        "bounds_mm": {"min_x": 0, "min_y": 0, "max_x": 3600, "max_y": 2000},
        "structures": [],
        "placements": [],
        "racks": [],
        "features": [
            {
                "id": "ZONE-3F-A01",
                "feature_code": "ZONE-3F-A01",
                "feature_kind": "zone",
                "points": [[0, 0], [3600, 0], [3600, 2000], [0, 2000]],
            }
        ],
    }


@pytest.fixture()
def p187_app(tmp_path, monkeypatch):
    engine = create_sqlite_engine(tmp_path / "p187.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(
        warehouse_api, "load_warehouse_twin_floor", lambda _floor_code: _measured_layout()
    )

    with factory() as db:
        admin = User(
            username="p187-admin",
            password_hash=hash_password("123456"),
            role="admin",
            real_name="P1-87 admin",
            is_active=True,
            must_change_password=False,
            customer_access_mode="all",
        )
        workshop = User(
            username="p187-workshop",
            password_hash=hash_password("123456"),
            role="workshop",
            real_name="P1-87 workshop",
            is_active=True,
            must_change_password=False,
            customer_access_mode="selected",
        )
        customer = Customer(name="昆山华诚电子有限公司", customer_code="HC")
        other_customer = Customer(name="常熟瑞丰食品有限公司", customer_code="RF")
        floor = WarehouseFloor(
            floor_code="3F",
            floor_name="三楼",
            floor_number=3,
            construction_status="enabled",
        )
        db.add_all([admin, workshop, customer, other_customer, floor])
        db.flush()
        product = Product(
            customer_id=customer.id,
            product_code="HC-BOX-001",
            customer_material_code="HC-BOX-001",
            product_name="五层加强纸箱",
            length_mm=Decimal("520"),
            width_mm=Decimal("350"),
            height_mm=Decimal("300"),
        )
        other_product = Product(
            customer_id=other_customer.id,
            product_code="RF-BOX-001",
            customer_material_code="RF-BOX-001",
            product_name="三层瓦楞外箱",
        )
        area = WarehouseArea(
            floor_id=floor.id,
            area_code="A01",
            area_name="三楼 A1 成品地堆区",
            address_zone_code="A",
            address_subzone_no=1,
            construction_status="enabled",
            capacity_review_status="confirmed",
            capacity_eligible=True,
            confirmed_pallet_capacity=6,
            capacity_reviewed_by="P1-87 test",
            capacity_reviewed_at=date(2026, 8, 22),
        )
        db.add_all([product, other_product, area])
        db.flush()
        db.add(UserCustomerScope(user_id=workshop.id, customer_id=customer.id))
        db.add(
            WarehouseAreaStoragePolicy(
                area_id=area.id,
                map_feature_id="ZONE-3F-A01",
                allowed_inventory_types_json='["finished"]',
                storage_layout="pallet_ground",
                status="published",
                published_map_revision="p1-87-map-r1",
                version=1,
                updated_by=admin.id,
            )
        )
        db.commit()
        ids = {
            "admin": admin.id,
            "workshop": workshop.id,
            "customer": customer.id,
            "product": product.id,
            "other_customer": other_customer.id,
            "other_product": other_product.id,
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(warehouse_router, prefix="/api/warehouse")

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, factory, ids
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login", json={"username": "p187-admin", "password": "123456"}
    )
    assert response.status_code == 200, response.text


def _login_workshop(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login", json={"username": "p187-workshop", "password": "123456"}
    )
    assert response.status_code == 200, response.text


def _publish_six_slots(client: TestClient) -> dict:
    draft = client.post(
        "/api/warehouse/ground-layout/floors/3F/areas/A01/draft",
        json={
            "target_slot_count": 6,
            "numbering_origin": "south",
            "row_direction": "from_aisle_inward",
            "slot_direction": "left_to_right",
            "row_start_no": 1,
            "slot_start_no": 1,
            "expected_policy_version": 1,
            "expected_map_revision": "p1-87-map-r1",
        },
    )
    assert draft.status_code == 200, draft.text
    body = draft.json()
    assert body["writes_inventory"] is False
    assert body["slots"][0]["location_code"] == "3F-A01-P01-01"
    assert body["slots"][4]["location_code"] == "3F-A01-P02-02"
    publish = client.post(
        "/api/warehouse/ground-layout/floors/3F/areas/A01/publish",
        json={
            "expected_plan_version": body["plan_version"],
            "preview_fingerprint": body["preview_fingerprint"],
            "idempotency_key": "p187-publish-a01",
        },
    )
    assert publish.status_code == 200, publish.text
    return publish.json()


def test_numbering_preview_uses_standard_footprint_and_custom_direction() -> None:
    preview = build_ground_slot_preview(
        _measured_layout(),
        feature_id="ZONE-3F-A01",
        floor_number=3,
        zone_code="A",
        subzone_no=1,
        target_slot_count=6,
        numbering_origin="north",
        row_direction="from_aisle_inward",
        slot_direction="right_to_left",
        row_start_no=2,
        slot_start_no=3,
    )
    assert len(preview) == 6
    assert preview[0]["location_code"] == "3F-A01-P02-03"
    assert preview[0]["location_name"] == "三楼 A1区·第2排·3号位"
    assert {(row["width_mm"], row["depth_mm"]) for row in preview} <= {
        (1200.0, 1000.0),
        (1000.0, 1200.0),
    }
    assert all(row["layout_kind"] == "physical_pallet" for row in preview)


def test_draft_publish_is_inventory_neutral_and_keeps_stable_locations(p187_app) -> None:
    app, factory, _ids = p187_app
    with TestClient(app) as client:
        _login(client)
        result = _publish_six_slots(client)
        replay = client.post(
            "/api/warehouse/ground-layout/floors/3F/areas/A01/publish",
            json={
                "expected_plan_version": 1,
                "preview_fingerprint": result["preview_fingerprint"],
                "idempotency_key": "p187-publish-a01",
            },
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True
        assert [row["location_id"] for row in replay.json()["slots"]] == [
            row["location_id"] for row in result["slots"]
        ]

    with factory() as db:
        assert db.scalar(select(func.count(WarehouseGroundLayoutPlan.id))) == 1
        assert db.scalar(select(func.count(WarehouseGroundLayoutSlot.id))) == 6
        assert db.scalar(select(func.count(InventoryLot.id))) == 0
        assert db.scalar(select(func.count(InventoryPallet.id))) == 0


def test_workshop_can_use_scoped_candidates_but_cannot_publish_layout(p187_app) -> None:
    app, _factory, ids = p187_app
    with TestClient(app) as admin_client:
        _login(admin_client)
        _publish_six_slots(admin_client)

    with TestClient(app) as workshop_client:
        _login_workshop(workshop_client)
        planning = workshop_client.post(
            "/api/warehouse/ground-layout/floors/3F/areas/A01/draft",
            json={
                "target_slot_count": 6,
                "numbering_origin": "south",
                "row_direction": "from_aisle_inward",
                "slot_direction": "left_to_right",
                "row_start_no": 1,
                "slot_start_no": 1,
                "expected_policy_version": 2,
                "expected_map_revision": "p1-87-map-r1",
            },
        )
        assert planning.status_code == 403

        allowed = workshop_client.get(
            "/api/warehouse/ground-storage/candidates",
            params={
                "floor_code": "3F",
                "area_code": "A01",
                "customer_id": ids["customer"],
                "product_id": ids["product"],
                "incoming_quantity": 100,
            },
        )
        assert allowed.status_code == 200, allowed.text
        assert len(allowed.json()["items"]) == 6

        denied = workshop_client.get(
            "/api/warehouse/ground-storage/candidates",
            params={
                "floor_code": "3F",
                "area_code": "A01",
                "customer_id": ids["other_customer"],
                "product_id": ids["other_product"],
                "incoming_quantity": 100,
            },
        )
        assert denied.status_code == 403


def test_map_candidates_colocation_and_two_slot_occupancy_keep_lots_separate(
    p187_app,
) -> None:
    app, factory, ids = p187_app
    with TestClient(app) as client:
        _login(client)
        published = _publish_six_slots(client)
        location_ids = [row["location_id"] for row in published["slots"]]
        versions = {row["location_id"]: row["layout_version"] for row in published["slots"]}

        candidates = client.get(
            "/api/warehouse/ground-storage/candidates",
            params={
                "floor_code": "3F",
                "area_code": "A01",
                "customer_id": ids["customer"],
                "product_id": ids["product"],
                "incoming_quantity": 100,
            },
        )
        assert candidates.status_code == 200, candidates.text
        assert {row["status"] for row in candidates.json()["items"]} == {"empty"}
        assert candidates.json()["legend"]["empty"]["color"] == "green"
        assert "pallet_id" not in candidates.text

        first = client.post(
            "/api/warehouse/ground-storage/finished-inbound",
            json={
                "location_id": location_ids[0],
                "expected_layout_version": versions[location_ids[0]],
                "customer_id": ids["customer"],
                "product_id": ids["product"],
                "quantity": 100,
                "capacity_quantity": 200,
                "stock_date": "2026-08-22",
                "idempotency_key": "p187-inbound-first",
            },
        )
        assert first.status_code == 201, first.text

        same = client.get(
            "/api/warehouse/ground-storage/candidates",
            params={
                "floor_code": "3F",
                "area_code": "A01",
                "customer_id": ids["customer"],
                "product_id": ids["product"],
                "incoming_quantity": 50,
            },
        ).json()
        same_target = next(row for row in same["items"] if row["location_id"] == location_ids[0])
        assert same_target["status"] == "same_product"
        assert same_target["color"] == "blue"
        assert same_target["current_quantity"] == 100
        assert same_target["remaining_capacity"] == 100

        conflict = client.get(
            "/api/warehouse/ground-storage/candidates",
            params={
                "floor_code": "3F",
                "area_code": "A01",
                "customer_id": ids["other_customer"],
                "product_id": ids["other_product"],
                "incoming_quantity": 1,
            },
        ).json()
        conflict_target = next(
            row for row in conflict["items"] if row["location_id"] == location_ids[0]
        )
        assert conflict_target["status"] == "conflict"
        assert conflict_target["color"] == "red"
        assert conflict_target["current_product"] is None

        full = client.get(
            "/api/warehouse/ground-storage/candidates",
            params={
                "floor_code": "3F",
                "area_code": "A01",
                "customer_id": ids["customer"],
                "product_id": ids["product"],
                "incoming_quantity": 101,
            },
        ).json()
        full_target = next(row for row in full["items"] if row["location_id"] == location_ids[0])
        assert full_target["status"] == "capacity_full"
        assert full_target["color"] == "gray"

        second = client.post(
            "/api/warehouse/ground-storage/finished-inbound",
            json={
                "location_id": location_ids[0],
                "expected_layout_version": versions[location_ids[0]],
                "customer_id": ids["customer"],
                "product_id": ids["product"],
                "quantity": 50,
                "capacity_quantity": 200,
                "stock_date": "2026-08-22",
                "idempotency_key": "p187-inbound-second",
            },
        )
        assert second.status_code == 201, second.text

        non_adjacent = client.post(
            "/api/warehouse/ground-storage/finished-inbound",
            json={
                "location_id": location_ids[1],
                "expected_layout_version": versions[location_ids[1]],
                "secondary_location_id": location_ids[5],
                "expected_secondary_layout_version": versions[location_ids[5]],
                "customer_id": ids["customer"],
                "product_id": ids["product"],
                "quantity": 80,
                "capacity_quantity": 80,
                "stock_date": "2026-08-22",
                "idempotency_key": "p187-inbound-non-adjacent",
            },
        )
        assert non_adjacent.status_code == 409
        assert non_adjacent.json()["detail"]["code"] == "GROUND_SLOTS_NOT_ADJACENT"

        large = client.post(
            "/api/warehouse/ground-storage/finished-inbound",
            json={
                "location_id": location_ids[1],
                "expected_layout_version": versions[location_ids[1]],
                "secondary_location_id": location_ids[2],
                "expected_secondary_layout_version": versions[location_ids[2]],
                "customer_id": ids["customer"],
                "product_id": ids["product"],
                "quantity": 80,
                "capacity_quantity": 80,
                "stock_date": "2026-08-22",
                "idempotency_key": "p187-inbound-large",
            },
        )
        assert large.status_code == 201, large.text
        assert large.json()["occupancy"]["slot_count"] == 2

    with factory() as db:
        assert db.scalar(select(func.count(InventoryLot.id))) == 3
        assert db.scalar(select(func.count(InventoryPallet.id))) == 2
        assert db.scalar(select(func.count(InventoryPalletItem.id))) == 3
        assert db.scalar(select(func.count(WarehouseGroundOccupancy.id))) == 2
        assert db.scalar(select(func.count(WarehouseGroundOccupancySlot.id))) == 3


def test_ground_lot_transfer_is_conservative_replayable_and_blocks_legacy_bypass(
    p187_app,
) -> None:
    app, factory, ids = p187_app
    with TestClient(app) as client:
        _login(client)
        published = _publish_six_slots(client)
        slots = published["slots"]
        source = slots[0]
        target = slots[3]
        inbound = client.post(
            "/api/warehouse/ground-storage/finished-inbound",
            json={
                "location_id": source["location_id"],
                "expected_layout_version": source["layout_version"],
                "customer_id": ids["customer"],
                "product_id": ids["product"],
                "quantity": 120,
                "capacity_quantity": 150,
                "stock_date": "2026-08-22",
                "idempotency_key": "p187-transfer-source",
            },
        )
        assert inbound.status_code == 201, inbound.text
        source_lot_id = inbound.json()["lot_id"]
        with factory() as db:
            expected_lot_version = db.get(InventoryLot, source_lot_id).version

        payload = {
            "location_id": target["location_id"],
            "expected_layout_version": target["layout_version"],
            "expected_lot_version": expected_lot_version,
            "quantity": 120,
            "capacity_quantity": 150,
            "idempotency_key": "p187-transfer-map",
        }
        moved = client.post(
            f"/api/warehouse/ground-storage/lots/{source_lot_id}/transfer",
            json=payload,
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["location_name"] == target["location_name"]
        replay = client.post(
            f"/api/warehouse/ground-storage/lots/{source_lot_id}/transfer",
            json=payload,
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True
        changed = client.post(
            f"/api/warehouse/ground-storage/lots/{source_lot_id}/transfer",
            json={**payload, "quantity": 119},
        )
        assert changed.status_code == 409
        assert changed.json()["detail"]["code"] == "GROUND_IDEMPOTENCY_CONFLICT"

        legacy = client.post(
            "/api/warehouse/twin-operations/finished-inbound",
            json={
                "location_id": slots[4]["location_id"],
                "expected_layout_version": slots[4]["layout_version"],
                "customer_id": ids["customer"],
                "product_id": ids["product"],
                "quantity": 1,
                "stock_date": "2026-08-22",
                "idempotency_key": "p187-legacy-bypass",
                "confirmed": True,
            },
        )
        assert legacy.status_code == 409
        assert "地堆排位" in legacy.text

    with factory() as db:
        live_quantity = int(
            db.scalar(
                select(
                    func.coalesce(
                        func.sum(
                            InventoryLot.quantity_available
                            + InventoryLot.quantity_reserved
                            + InventoryLot.quantity_damaged
                        ),
                        0,
                    )
                ).where(InventoryLot.status.in_(("active", "frozen")))
            )
            or 0
        )
        assert live_quantity == 120
        assert db.scalar(select(func.count(InventoryLotTransfer.id))) == 1
        occupancies = db.scalars(
            select(WarehouseGroundOccupancy).order_by(WarehouseGroundOccupancy.id)
        ).all()
        assert [row.status for row in occupancies] == ["released", "active"]


def test_ground_inbound_audit_failure_rolls_back_every_business_fact(
    p187_app, monkeypatch
) -> None:
    app, factory, ids = p187_app
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client)
        published = _publish_six_slots(client)
        slot = published["slots"][0]

        def fail_audit(*_args, **_kwargs):
            raise RuntimeError("injected audit failure")

        monkeypatch.setattr(warehouse_api, "append_audit_event", fail_audit)
        failed = client.post(
            "/api/warehouse/ground-storage/finished-inbound",
            json={
                "location_id": slot["location_id"],
                "expected_layout_version": slot["layout_version"],
                "customer_id": ids["customer"],
                "product_id": ids["product"],
                "quantity": 30,
                "capacity_quantity": 100,
                "stock_date": "2026-08-22",
                "idempotency_key": "p187-audit-rollback",
            },
        )
        assert failed.status_code == 500

    with factory() as db:
        assert db.scalar(select(func.count(InventoryLot.id))) == 0
        assert db.scalar(select(func.count(InventoryPallet.id))) == 0
        assert db.scalar(select(func.count(WarehouseGroundOccupancy.id))) == 0
