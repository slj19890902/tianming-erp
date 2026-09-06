from __future__ import annotations

from datetime import date, datetime
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
    Floor3LocationLayout,
    InventoryLot,
    FinishedGoodsInventoryDetail,
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
    WarehouseLocation,
)
from app.services import location_candidates
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


def _published_runtime_identity() -> dict:
    """Return the canonical current-map identity after the fixture is published."""

    return {
        "revision": "p1-87-map-r1",
        "zones_by_id": {"ZONE-3F-A01": "A01"},
        "zone_ids_by_area": {"A01": ("ZONE-3F-A01",)},
    }


@pytest.fixture()
def p187_app(tmp_path, monkeypatch):
    engine = create_sqlite_engine(tmp_path / "p187.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(
        warehouse_api, "load_warehouse_twin_floor", lambda _floor_code: _measured_layout()
    )
    monkeypatch.setattr(
        warehouse_api,
        "load_published_warehouse_twin_floor_for_edit",
        lambda _floor_code: _measured_layout(),
    )
    monkeypatch.setattr(
        location_candidates,
        "load_warehouse_twin_published_floor_identity",
        lambda floor_number: (
            _published_runtime_identity() if int(floor_number) == 3 else None
        ),
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


def test_explicit_empty_ground_slot_retirement_preserves_plan_and_rejects_occupied(p187_app):
    from sqlalchemy import text
    app, factory, ids = p187_app
    with TestClient(app) as client:
        _login(client)
        published = _publish_six_slots(client)
        slots = published["slots"]
        with factory() as db:
            plan = db.scalar(select(WarehouseGroundLayoutPlan))
            plan_version = plan.version
            for table in ("warehouse_ground_layout_plans", "warehouse_ground_layout_slots"):
                db.execute(text(f"CREATE TRIGGER keep_{table} BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT,'immutable original plan'); END"))
            db.commit()
            before = {t: db.execute(text(f"SELECT * FROM {t} ORDER BY id")).all() for t in ("warehouse_ground_layout_plans", "warehouse_ground_layout_slots")}
        inbound = client.post("/api/warehouse/ground-storage/finished-inbound", json={
            "location_id": slots[0]["location_id"], "secondary_location_id": slots[1]["location_id"],
            "expected_secondary_layout_version": slots[1]["layout_version"],
            "expected_layout_version": slots[0]["layout_version"], "customer_id": ids["customer"], "product_id": ids["product"],
            "quantity": 1, "capacity_quantity": 2, "stock_date": "2026-09-06", "idempotency_key": "retire-occupied-check",
        })
        assert inbound.status_code == 201, inbound.text
        def payload(slot):
            management = client.get("/api/warehouse/spatial-layout/floors/3F/areas/A01/management").json()
            return {"expected_version": slot["layout_version"], "expected_map_revision": management["published_map_revision"],
                    "expected_policy_version": management["policy_version"], "retire_published_ground_slot": True,
                    "expected_ground_plan_version": plan_version}
        for slot in slots[:2]:
            response = client.post(f'/api/warehouse/spatial-layout/locations/{slot["location_id"]}/disable', json=payload(slot))
            assert response.status_code == 409, response.text
            assert "占用" in response.text
        slot = slots[2]
        url = f'/api/warehouse/spatial-layout/locations/{slot["location_id"]}/disable'
        body = payload(slot)
        assert client.post(url, json={**body, "retire_published_ground_slot": False}).status_code == 409
        assert client.post(url, json={**body, "expected_ground_plan_version": plan_version + 1}).status_code == 409
        _login_workshop(client)
        assert client.post(url, json=body).status_code == 403
        _login(client)
        result = client.post(url, json=body)
        assert result.status_code == 200, result.text
        assert result.json()["location"]["is_active"] is False
        assert client.post(url, json=body).status_code == 409
    with factory() as db:
        for table, rows in before.items():
            assert db.execute(text(f"SELECT * FROM {table} ORDER BY id")).all() == rows
        assert db.scalar(select(func.count(WarehouseLocation.id))) == 6
        assert db.scalar(select(func.count(WarehouseLocation.id)).where(WarehouseLocation.is_active.is_(True))) == 5
        assert db.scalar(select(func.sum(InventoryLot.quantity_available))) == 1


@pytest.mark.parametrize("legacy_reflected", [False, True])
def test_new_map_requires_verified_unchanged_ground_positions_and_invalidates_stale_receipt(p187_app, monkeypatch, legacy_reflected):
    import copy
    from sqlalchemy import text
    from app.services import warehouse_twin_layout
    from app.services.warehouse_ground_map_application import record_map_applications
    from app.services.warehouse_ground_slots import published_ground_plan, WarehouseGroundSlotError
    from app.services.warehouse_area_activation import WarehouseAreaActivationError
    app, factory, ids = p187_app
    with TestClient(app) as client:
        _login(client)
        published = _publish_six_slots(client)
        slot = published["slots"][2]
        m = client.get("/api/warehouse/spatial-layout/floors/3F/areas/A01/management").json()
        disabled = client.post(f'/api/warehouse/spatial-layout/locations/{slot["location_id"]}/disable', json={
            "expected_version": slot["layout_version"], "expected_map_revision": m["published_map_revision"],
            "expected_policy_version": m["policy_version"], "retire_published_ground_slot": True,
            "expected_ground_plan_version": m["ground_plan_version"],
        })
        assert disabled.status_code == 200, disabled.text
    identity = {**_published_runtime_identity(), "revision": "verified-new-map"}
    monkeypatch.setattr(location_candidates, "load_warehouse_twin_published_floor_identity", lambda _floor: identity)
    monkeypatch.setattr(warehouse_twin_layout, "load_warehouse_twin_published_floor_identity", lambda _floor: identity)
    measured = {**_measured_layout(), "revision": identity["revision"]}
    with factory() as db:
        actor = db.get(User, ids["admin"])
        policy = db.scalar(select(WarehouseAreaStoragePolicy))
        policy.published_map_revision = identity["revision"]
        if legacy_reflected:
            legacy_plan = db.scalar(select(WarehouseGroundLayoutPlan))
            for slot in legacy_plan.slots:
                slot.y_mm = Decimal(2000) - slot.y_mm - slot.depth_mm
            db.commit()
            with pytest.raises(WarehouseAreaActivationError, match="物理坐标"):
                record_map_applications(db, floor_layout=measured, actor=actor, operation_key="unrecognized-origin")
            legacy_plan.publish_idempotency_key = "p0-26-current-map-e5f192ba605185db:publish:1"
        db.commit()
        original = {t: db.execute(text(f"SELECT * FROM {t} ORDER BY id")).all() for t in ("warehouse_ground_layout_plans", "warehouse_ground_layout_slots")}
        with pytest.raises(WarehouseGroundSlotError, match="重新核对"):
            published_ground_plan(db, floor_code="3F", area_code="A01")
        assert record_map_applications(db, floor_layout=measured, actor=actor, operation_key="apply-map") == 1
        db.commit()
        assert record_map_applications(db, floor_layout=measured, actor=actor, operation_key="apply-map-retry") == 0
        plan = published_ground_plan(db, floor_code="3F", area_code="A01")
        assert plan.published_map_revision == "p1-87-map-r1"
        rows = list(db.scalars(select(WarehouseLocation).where(WarehouseLocation.is_active.is_(True))))
        contexts = location_candidates.load_warehouse_location_projection_contexts(db, rows)
        assert all(c["ground_layout"]["published_map_revision"] == identity["revision"] for c in contexts.values())
        assert len(contexts) == 5
        drifted = copy.deepcopy(measured)
        drifted["bounds_mm"]["max_x"] += 20
        drifted["features"][0]["points"] = [[x + 20, y] for x, y in drifted["features"][0]["points"]]
        with pytest.raises(WarehouseAreaActivationError, match="物理坐标"):
            record_map_applications(db, floor_layout=drifted, actor=actor, operation_key="bad-map")
        row = rows[0]
        row.floor3_layout.version += 1
        db.flush()
        context = location_candidates.load_warehouse_location_projection_contexts(db, [row])[row.id]
        assert context["ground_layout"]["published_map_revision"] == "p1-87-map-r1"
        with pytest.raises(WarehouseGroundSlotError, match="重新核对"):
            published_ground_plan(db, floor_code="3F", area_code="A01")
        for table, values in original.items():
            assert db.execute(text(f"SELECT * FROM {table} ORDER BY id")).all() == values


def test_admin_can_drag_published_ground_slots_with_gaps_and_replay_safely(
    p187_app,
) -> None:
    app, factory, _ids = p187_app
    with TestClient(app) as client:
        _login(client)
        draft = client.post(
            "/api/warehouse/ground-layout/floors/3F/areas/A01/draft",
            json={
                "target_slot_count": 4,
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
        published = client.post(
            "/api/warehouse/ground-layout/floors/3F/areas/A01/publish",
            json={
                "expected_plan_version": draft.json()["plan_version"],
                "preview_fingerprint": draft.json()["preview_fingerprint"],
                "idempotency_key": "p187-publish-free-layout",
            },
        )
        assert published.status_code == 200, published.text
        management = client.get(
            "/api/warehouse/spatial-layout/floors/3F/areas/A01/management"
        )
        assert management.status_code == 200, management.text
        assert management.json()["available_actions"] == ["published_layout"]

        slots = published.json()["slots"]
        moved = slots[-1]
        with factory() as db:
            before_layouts = {
                row.location_id: (
                    row.left_pct,
                    row.top_pct,
                    row.width_pct,
                    row.height_pct,
                    row.z_index,
                    row.version,
                )
                for row in db.scalars(select(Floor3LocationLayout)).all()
            }
            before_ground_positions = {
                row.location_id: (row.x_mm, row.y_mm, row.width_mm, row.depth_mm)
                for row in db.scalars(select(WarehouseGroundLayoutSlot)).all()
            }
            layout = db.scalar(
                select(Floor3LocationLayout).where(
                    Floor3LocationLayout.location_id == moved["location_id"]
                )
            )
            assert layout is not None
            before = {
                "plans": int(db.scalar(select(func.count(WarehouseGroundLayoutPlan.id))) or 0),
                "slots": int(db.scalar(select(func.count(WarehouseGroundLayoutSlot.id))) or 0),
                "lots": int(db.scalar(select(func.count(InventoryLot.id))) or 0),
                "pallets": int(db.scalar(select(func.count(InventoryPallet.id))) or 0),
            }
            payload_slot = {
                "location_id": moved["location_id"],
                "expected_version": layout.version,
                "left_pct": float(layout.left_pct) + float(layout.width_pct),
                "top_pct": float(layout.top_pct),
                "width_pct": float(layout.width_pct),
                "height_pct": float(layout.height_pct),
                "z_index": layout.z_index,
            }

        payload = {
            "slots": [payload_slot],
            "expected_map_revision": "p1-87-map-r1",
            "expected_policy_version": management.json()["policy_version"],
            "expected_plan_version": management.json()["ground_plan_version"],
            "idempotency_key": "p187-free-gap-save-01",
        }
        changed = client.patch(
            "/api/warehouse/ground-layout/floors/3F/areas/A01/published-positions",
            json=payload,
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["changed_location_count"] == 1
        assert changed.json()["writes_inventory"] is False
        assert changed.json()["idempotent_replay"] is False

        replay = client.patch(
            "/api/warehouse/ground-layout/floors/3F/areas/A01/published-positions",
            json=payload,
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True
        conflicting_payload = {
            **payload,
            "slots": [
                {
                    **payload_slot,
                    "left_pct": payload_slot["left_pct"] + 0.1,
                }
            ],
        }
        conflict = client.patch(
            "/api/warehouse/ground-layout/floors/3F/areas/A01/published-positions",
            json=conflicting_payload,
        )
        assert conflict.status_code == 409, conflict.text
        assert "同一保存编号" in conflict.text
        with factory() as db:
            after = {
                "plans": int(db.scalar(select(func.count(WarehouseGroundLayoutPlan.id))) or 0),
                "slots": int(db.scalar(select(func.count(WarehouseGroundLayoutSlot.id))) or 0),
                "lots": int(db.scalar(select(func.count(InventoryLot.id))) or 0),
                "pallets": int(db.scalar(select(func.count(InventoryPallet.id))) or 0),
            }
            saved = db.scalar(
                select(Floor3LocationLayout).where(
                    Floor3LocationLayout.location_id == moved["location_id"]
                )
            )
            assert saved is not None
            assert saved.version == payload_slot["expected_version"] + 1
            after_layouts = {
                row.location_id: (
                    row.left_pct,
                    row.top_pct,
                    row.width_pct,
                    row.height_pct,
                    row.z_index,
                    row.version,
                )
                for row in db.scalars(select(Floor3LocationLayout)).all()
            }
            after_ground_positions = {
                row.location_id: (row.x_mm, row.y_mm, row.width_mm, row.depth_mm)
                for row in db.scalars(select(WarehouseGroundLayoutSlot)).all()
            }
            unchanged_location_ids = set(before_layouts) - {moved["location_id"]}
            assert {
                location_id: before_layouts[location_id]
                for location_id in unchanged_location_ids
            } == {
                location_id: after_layouts[location_id]
                for location_id in unchanged_location_ids
            }
            for location_id in unchanged_location_ids:
                before_ground = before_ground_positions[location_id]
                after_ground = after_ground_positions[location_id]
                assert float(after_ground[0]) == pytest.approx(
                    float(before_ground[0]), abs=0.02
                )
                assert float(after_ground[1]) == pytest.approx(
                    float(before_ground[1]), abs=0.02
                )
                assert after_ground[2:] == before_ground[2:]
            moved_ground = after_ground_positions[moved["location_id"]]
            assert float(moved_ground[0]) == pytest.approx(
                payload_slot["left_pct"] / 100 * 3600,
                abs=0.02,
            )
            assert float(moved_ground[1]) == pytest.approx(
                2000
                - (payload_slot["top_pct"] + payload_slot["height_pct"])
                / 100
                * 2000,
                abs=0.02,
            )
            assert after == before


def test_map_publish_blocks_zone_drift_when_formal_ground_locations_exist(
    p187_app,
    monkeypatch,
) -> None:
    app, factory, _ids = p187_app
    with TestClient(app) as client:
        _login(client)
        _publish_six_slots(client)

    published = _measured_layout()
    moved = {
        **published,
        "revision": "p1-87-map-draft-moved",
        "features": [
            {
                **published["features"][0],
                "points": [
                    [1000, 0],
                    [4600, 0],
                    [4600, 2000],
                    [1000, 2000],
                ],
            }
        ],
    }
    monkeypatch.setattr(
        warehouse_api,
        "load_warehouse_twin_layout_draft",
        lambda _floor_code: moved,
    )

    with factory() as db:
        geometry_blockers = warehouse_api._formal_location_zone_geometry_blockers(
            db,
            "3F",
            draft_layout=moved,
            published_layout=published,
        )
        assert geometry_blockers == [
            "A01 区已有正式货位，不能随区域边界一起移动或缩放；"
            "请放弃该区域几何草稿，改为逐个调整货位"
        ]
        assert geometry_blockers[0] in warehouse_api._formal_area_publish_blockers(
            db, "3F"
        )
        assert warehouse_api._formal_location_zone_geometry_blockers(
            db,
            "3F",
            draft_layout=published,
            published_layout=published,
        ) == []


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


def test_unstructured_fin_publish_adopts_existing_real_locations_without_duplication(
    p187_app,
) -> None:
    app, factory, ids = p187_app
    with factory() as db:
        floor = db.scalar(select(WarehouseFloor))
        area = db.scalar(select(WarehouseArea))
        assert floor is not None and area is not None
        floor.floor_code = "1F"
        floor.floor_name = "一楼"
        floor.floor_number = 1
        area.area_code = "FIN-001"
        area.area_name = "一楼成品待送区一"
        area.address_zone_code = None
        area.address_subzone_no = None
        preview = build_ground_slot_preview(
            _measured_layout(),
            feature_id="ZONE-3F-A01",
            floor_number=1,
            zone_code="F",
            subzone_no=1,
            target_slot_count=6,
            numbering_origin="south",
            row_direction="from_aisle_inward",
            slot_direction="left_to_right",
            row_start_no=1,
            slot_start_no=1,
        )
        locations: list[WarehouseLocation] = []
        for index, slot in enumerate(preview, start=1):
            location = WarehouseLocation(
                location_code=f"F1-FIN-001-L{index:03d}",
                location_name=f"成品待送堆放区 {index:03d} 号位",
                warehouse_type="finished",
                is_active=True,
                warehouse_floor=1,
                area_code="FIN-001",
                storage_type="ground",
                sort_order=index,
                source_version="TWIN_V1",
                address_kind="legacy",
                placement_status="placed",
            )
            location.floor3_layout = Floor3LocationLayout(
                left_pct=Decimal(str(slot["left_pct"])),
                top_pct=Decimal(str(slot["top_pct"])),
                width_pct=Decimal(str(slot["width_pct"])),
                height_pct=Decimal(str(slot["height_pct"])),
                z_index=index,
                version=1,
                source_type="seeded",
                layout_kind="physical_pallet",
                created_by=ids["admin"],
                updated_by=ids["admin"],
            )
            locations.append(location)
        db.add_all(locations)
        db.flush()
        db.add(
            InventoryPallet(
                pallet_code="PLT-P187-LEGACY-FIN",
                location_id=locations[0].id,
                status="active",
                is_current=True,
                version=1,
            )
        )
        db.commit()
        existing_ids = [int(location.id) for location in locations]

    with TestClient(app) as client:
        _login(client)
        draft = client.post(
            "/api/warehouse/ground-layout/floors/1F/areas/FIN-001/draft",
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
        assert [row["existing_location_id"] for row in draft.json()["slots"]] == existing_ids
        published = client.post(
            "/api/warehouse/ground-layout/floors/1F/areas/FIN-001/publish",
            json={
                "expected_plan_version": draft.json()["plan_version"],
                "preview_fingerprint": draft.json()["preview_fingerprint"],
                "idempotency_key": "p187-adopt-fin-001",
            },
        )
        assert published.status_code == 200, published.text
        assert [row["location_id"] for row in published.json()["slots"]] == existing_ids

    with factory() as db:
        assert db.scalar(select(func.count(WarehouseLocation.id))) == 6
        assert db.scalar(select(func.count(WarehouseGroundLayoutSlot.id))) == 6
        area = db.scalar(select(WarehouseArea))
        assert area is not None
        assert area.area_code == "FIN-001"
        assert area.address_zone_code is None
        pallet = db.scalar(select(InventoryPallet))
        assert pallet is not None
        assert pallet.location_id == existing_ids[0]
        assert pallet.is_current is True


def test_workshop_can_use_scoped_candidates_but_cannot_publish_layout(p187_app) -> None:
    app, factory, ids = p187_app
    with TestClient(app) as admin_client:
        _login(admin_client)
        published = _publish_six_slots(admin_client)

    first_slot = published["slots"][0]
    with factory() as db:
        first_layout = db.scalar(
            select(Floor3LocationLayout).where(
                Floor3LocationLayout.location_id == first_slot["location_id"]
            )
        )
        assert first_layout is not None
        restricted_payload = {
            "slots": [
                {
                    "location_id": first_slot["location_id"],
                    "expected_version": first_layout.version,
                    "left_pct": float(first_layout.left_pct),
                    "top_pct": float(first_layout.top_pct),
                    "width_pct": float(first_layout.width_pct),
                    "height_pct": float(first_layout.height_pct),
                    "z_index": first_layout.z_index,
                }
            ],
            "expected_map_revision": "p1-87-map-r1",
            "expected_policy_version": 2,
            "expected_plan_version": published["plan_version"],
            "idempotency_key": "p187-workshop-cannot-move",
        }

    with TestClient(app) as workshop_client:
        _login_workshop(workshop_client)
        denied_position_save = workshop_client.patch(
            "/api/warehouse/ground-layout/floors/3F/areas/A01/published-positions",
            json=restricted_payload,
        )
        assert denied_position_save.status_code == 403
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


@pytest.mark.parametrize("double", [False, True])
def test_auto_projection_growth_preserves_immutable_occupancy_history(p187_app, double):
    import importlib.util
    from pathlib import Path
    from sqlalchemy import text
    from app.services.warehouse_inventory import _ensure_finished_projection_postcondition

    app, factory, ids = p187_app
    with TestClient(app) as client:
        _login(client)
        slots = _publish_six_slots(client)["slots"]
    path = Path(__file__).resolve().parents[1] / "alembic/versions/jm71v8x9z60_delivery_ground_occupancy_restore.py"
    spec = importlib.util.spec_from_file_location("occupancy_guards", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with factory() as db:
        lot = InventoryLot(lot_number="AUTO-PROJECTION", inventory_type="finished", unit="boxes",
            warehouse_location_id=slots[0]["location_id"], quantity_available=10,
            quantity_reserved=0, quantity_consumed=0, quantity_damaged=0, quantity_scrapped=0,
            status="active", source_type="production_completion", stock_date=date(2026, 9, 6),
            stock_date_accuracy="exact", last_movement_at=datetime(2026, 9, 6), version=1)
        lot.finished_detail = FinishedGoodsInventoryDetail(owner_customer_id=ids["customer"],
            owner_customer_name_snapshot="测试客户", product_id=ids["product"],
            product_name_snapshot="测试成品", inventory_code_snapshot="AUTO-PROJECTION", is_general=False)
        db.add(lot)
        db.flush()
        _ensure_finished_projection_postcondition(db, lot=lot, operator_id=ids["admin"], create_missing=True,
            ground_secondary_location_id=slots[1]["location_id"] if double else None)
        db.commit()
        original = db.scalar(select(WarehouseGroundOccupancy))
        original_id, original_capacity = original.id, original.capacity_quantity
        original_slots = {s.location_id for s in original.slots}
        db.execute(text(migration._occupancy_guard_sql(allow_restore=True)))
        db.execute(text(migration._slot_guard_sql(allow_restore=True)))
        db.commit()
        lot.quantity_available = 15
        db.flush()
        _ensure_finished_projection_postcondition(db, lot=lot, operator_id=ids["admin"], create_missing=True)
        db.commit()
        old = db.get(WarehouseGroundOccupancy, original_id)
        active = db.scalar(select(WarehouseGroundOccupancy).where(WarehouseGroundOccupancy.status == "active"))
        assert old.status == "released" and old.capacity_quantity == original_capacity
        assert old.version == 2 and all(s.status == "released" for s in old.slots)
        assert active.id != old.id and active.capacity_quantity == 15
        assert {s.location_id for s in active.slots} == original_slots
        assert active.footprint_kind == ("double" if double else "single")


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
