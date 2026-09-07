from __future__ import annotations

from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from threading import Barrier

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def floor3_app(tmp_path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.warehouse import router as warehouse_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User
    from app.models.warehouse_inventory import WarehouseLocation

    engine = create_sqlite_engine(tmp_path / "floor3-api.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="floor3-admin",
            password_hash=hash_password("123456"),
            role="admin",
            real_name="三楼测试管理员",
            must_change_password=False,
        )
        scoped = User(
            username="floor3-scoped",
            password_hash=hash_password("123456"),
            role="workshop",
            real_name="三楼范围员工",
            must_change_password=False,
            customer_access_mode="selected",
        )
        tianhua = Customer(
            name="苏州天华超净科技股份有限公司",
            customer_code="TH",
        )
        other = Customer(name="其他客户", customer_code="QT")
        db.add_all([admin, scoped, tianhua, other])
        db.flush()
        db.add(UserCustomerScope(user_id=scoped.id, customer_id=tianhua.id))

        products = []
        for number in range(1, 6):
            products.append(
                Product(
                    customer_id=tianhua.id,
                    product_code=f"2130101{number}",
                    customer_material_code=f"2130101{number}",
                    product_name="同名纸箱" if number <= 2 else f"纸箱{number}",
                    length_mm=Decimal("100") + number,
                    width_mm=Decimal("80"),
                    height_mm=Decimal("50"),
                )
            )
        other_product = Product(
            customer_id=other.id,
            product_code="OTHER-001",
            customer_material_code="OTHER-001",
            product_name="其他客户纸箱",
        )
        db.add_all([*products, other_product])
        db.flush()
        order = Order(
            order_number="TM20260714001",
            customer_id=tianhua.id,
            customer_po="PO-FLOOR3-001",
            order_date=date(2026, 7, 14),
            total_amount=Decimal("10"),
            created_by=admin.id,
        )
        db.add(order)
        db.flush()
        db.add(
            OrderItem(
                order_id=order.id,
                product_id=products[0].id,
                item_order_number="TM20260714001-001",
                quantity=10,
                unit_price=Decimal("1"),
                subtotal=Decimal("10"),
                snapshot_product_name=products[0].product_name,
                snapshot_product_code=products[0].product_code,
            )
        )
        locations = [
            WarehouseLocation(
                location_code="A1-L01",
                location_name="A1-L01",
                warehouse_type="finished",
                warehouse_floor=3,
                area_code="A1",
                storage_type="ground",
                sort_order=1,
                source_version="V11",
            ),
            WarehouseLocation(
                location_code="A1-L02",
                location_name="A1-L02",
                warehouse_type="finished",
                warehouse_floor=3,
                area_code="A1",
                storage_type="ground",
                sort_order=2,
                source_version="V11",
            ),
            WarehouseLocation(
                location_code="F12-P01",
                location_name="F12-P01",
                warehouse_type="shared",
                warehouse_floor=3,
                area_code="F12",
                storage_type="temporary_aisle",
                sort_order=3,
                is_temporary=True,
                source_version="V11",
            ),
            WarehouseLocation(
                location_code="FG-A01",
                location_name="成品正式库存货位",
                warehouse_type="finished",
                warehouse_floor=1,
                area_code="FG",
                storage_type="rack",
                sort_order=4,
            ),
            WarehouseLocation(
                location_code="LEGACY-3F-01",
                location_name="既有三楼正式库存货位",
                warehouse_type="finished",
                warehouse_floor=3,
                area_code="LEGACY",
                storage_type="rack",
                sort_order=5,
                source_version=None,
            ),
            WarehouseLocation(
                location_code="V11-WRONG-FLOOR",
                location_name="错误楼层的V11货位",
                warehouse_type="finished",
                warehouse_floor=2,
                area_code="INVALID",
                storage_type="rack",
                sort_order=6,
                source_version="V11",
            ),
            WarehouseLocation(
                location_code="F34-P01",
                location_name="F34-P01",
                warehouse_type="shared",
                warehouse_floor=3,
                area_code="F34",
                storage_type="temporary_aisle",
                sort_order=7,
                is_temporary=True,
                source_version="V11",
            ),
            WarehouseLocation(
                location_code="A1-R01",
                location_name="A1-R01",
                warehouse_type="finished",
                warehouse_floor=3,
                area_code="A1",
                storage_type="rack",
                sort_order=8,
                source_version="V11",
            ),
        ]
        db.add_all(locations)
        db.commit()
        ids = {
            "admin": admin.id,
            "scoped": scoped.id,
            "tianhua": tianhua.id,
            "other": other.id,
            "products": [row.id for row in products],
            "other_product": other_product.id,
            "locations": [row.id for row in locations],
            "formal_location": locations[3].id,
            "legacy_floor3_location": locations[4].id,
            "v11_wrong_floor_location": locations[5].id,
            "f34_location": locations[6].id,
            "rack_location": locations[7].id,
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(warehouse_router, prefix="/api/warehouse")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, ids, factory
    finally:
        engine.dispose()


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "123456"},
    )
    assert response.status_code == 200, response.text


def _matched_item(customer_id: int, product_id: int, code: str) -> dict:
    return {
        "customer_id": customer_id,
        "product_id": product_id,
        "inventory_code": code,
        "item_type": "finished",
        "quantity": 10,
        "unit": "boxes",
        "match_status": "matched",
    }


def _ensure_location_layout_version(factory, location_id: int, *, left_pct: int = 10) -> int:
    from app.models.warehouse_inventory import Floor3LocationLayout, WarehouseLocation

    with factory() as db:
        location = db.get(WarehouseLocation, location_id)
        assert location is not None
        if location.floor3_layout is None:
            location.floor3_layout = Floor3LocationLayout(
                left_pct=Decimal(left_pct),
                top_pct=Decimal(10),
                width_pct=Decimal(8),
                height_pct=Decimal(8),
                z_index=0,
                version=1,
                source_type="seeded",
                layout_kind="physical_pallet",
            )
            db.commit()
        return int(location.floor3_layout.version)


def test_map_projection_preserves_formal_four_decimal_precision() -> None:
    from app.services.location_candidates import _map_number

    assert _map_number(Decimal("34.2857")) == 34.2857
    assert _map_number(Decimal("42.8571")) == 42.8571
    assert _map_number(Decimal("8.5714")) == 8.5714


def test_published_candidates_keep_accepted_v11_map_locations_until_area_policy_exists(
    floor3_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.warehouse_inventory import (
        Floor3LocationLayout,
        WarehouseArea,
        WarehouseAreaStoragePolicy,
        WarehouseFloor,
        WarehouseGroundLayoutPlan,
        WarehouseGroundLayoutSlot,
        WarehouseLocation,
    )

    monkeypatch.setattr(
        "app.services.location_candidates.load_warehouse_twin_published_floor_identity",
        lambda _floor_number: {
            "revision": "legacy-v11-ground-v1",
            "zones_by_id": {"zone-3f-a1": "A1"},
            "zone_ids_by_area": {"A1": ("zone-3f-a1",)},
        },
    )

    app, ids, factory = floor3_app
    with factory() as db:
        floor = WarehouseFloor(
            floor_code="3F",
            floor_name="三楼",
            floor_number=3,
            construction_status="enabled",
            planning_reference_pallet_capacity=0,
        )
        db.add(floor)
        db.flush()
        area = WarehouseArea(
            floor_id=floor.id,
            area_code="A1",
            area_name="三楼 A1 成品区",
            construction_status="enabled",
        )
        db.add(area)
        db.flush()
        accepted = db.get(WarehouseLocation, ids["locations"][0])
        unplaced = db.get(WarehouseLocation, ids["locations"][1])
        assert accepted is not None and unplaced is not None
        accepted.placement_status = "placed"
        unplaced.placement_status = "unplaced"
        accepted.floor3_layout = Floor3LocationLayout(
            left_pct=Decimal("1"),
            top_pct=Decimal("1"),
            width_pct=Decimal("4"),
            height_pct=Decimal("4"),
            z_index=0,
            version=1,
            source_type="seeded",
        )
        unplaced.floor3_layout = Floor3LocationLayout(
            left_pct=Decimal("6"),
            top_pct=Decimal("1"),
            width_pct=Decimal("4"),
            height_pct=Decimal("4"),
            z_index=0,
            version=1,
            source_type="seeded",
        )
        plan = WarehouseGroundLayoutPlan(
            area_id=area.id,
            status="published",
            target_slot_count=1,
            numbering_origin="south",
            row_direction="from_aisle_inward",
            slot_direction="left_to_right",
            row_start_no=1,
            slot_start_no=1,
            draft_map_revision="legacy-v11-ground-v1",
            published_map_revision="legacy-v11-ground-v1",
            preview_fingerprint="e" * 64,
            version=1,
            publish_idempotency_key="legacy-v11-ground",
            publish_request_hash="f" * 64,
            updated_by=ids["admin"],
            published_by=ids["admin"],
            published_at=datetime.now(),
        )
        db.add(plan)
        db.flush()
        db.add(
            WarehouseGroundLayoutSlot(
                plan_id=plan.id,
                location_id=accepted.id,
                route_sequence=1,
                row_no=1,
                slot_no=1,
                x_mm=Decimal("1000"),
                y_mm=Decimal("1000"),
                width_mm=1200,
                depth_mm=1000,
            )
        )
        db.commit()
        area_id = area.id

    with TestClient(app) as client:
        _login(client, "floor3-admin")
        response = client.get(
            "/api/warehouse/location-candidates",
            params={
                "inventory_type": "finished",
                "empty_only": True,
                "pallet_storage_only": True,
                "published_only": True,
            },
        )
        assert response.status_code == 200, response.text
        returned_ids = {item["id"] for item in response.json()["items"]}
        assert ids["locations"][0] in returned_ids
        assert ids["locations"][1] not in returned_ids

    with factory() as db:
        db.add(
            WarehouseAreaStoragePolicy(
                area_id=area_id,
                map_feature_id="zone-3f-a1",
                allowed_inventory_types_json='["finished"]',
                storage_layout="pallet_ground",
                status="draft",
                version=1,
            )
        )
        db.commit()

    with TestClient(app) as client:
        _login(client, "floor3-admin")
        response = client.get(
            "/api/warehouse/location-candidates",
            params={
                "inventory_type": "finished",
                "empty_only": True,
                "pallet_storage_only": True,
                "published_only": True,
            },
        )
        assert response.status_code == 200, response.text
        assert ids["locations"][0] not in {
            item["id"] for item in response.json()["items"]
        }


def test_p1_16e2_merge_all_preserves_formal_lots_and_is_idempotent(
    floor3_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.warehouse_inventory import (
        InventoryLocationMovement,
        InventoryLot,
        InventoryMovement,
        InventoryPallet,
        InventoryPalletItem,
    )
    from app.services.warehouse_inventory import manual_finished_in

    app, ids, factory = floor3_app
    with factory() as db:
        first = manual_finished_in(
            db,
            customer_id=ids["tianhua"],
            product_id=ids["products"][0],
            location_id=ids["locations"][0],
            quantity=10,
            stock_date=date(2026, 7, 14),
            source_type="manual",
            remarks=None,
            operator_id=ids["admin"],
            idempotency_key="p1-16e2-source-lot-1",
            pallet_code="PLT-P1-16E2-SOURCE",
        )
        source_pallet_id = first.pallet_item.pallet_id
        second = manual_finished_in(
            db,
            customer_id=ids["tianhua"],
            product_id=ids["products"][1],
            location_id=ids["locations"][0],
            quantity=7,
            stock_date=date(2026, 7, 10),
            source_type="manual",
            remarks=None,
            operator_id=ids["admin"],
            idempotency_key="p1-16e2-source-lot-2",
            pallet_id=source_pallet_id,
        )
        target_lot = manual_finished_in(
            db,
            customer_id=ids["tianhua"],
            product_id=ids["products"][2],
            location_id=ids["locations"][1],
            quantity=5,
            stock_date=date(2026, 7, 1),
            source_type="manual",
            remarks=None,
            operator_id=ids["admin"],
            idempotency_key="p1-16e2-target-lot",
            pallet_code="PLT-P1-16E2-TARGET",
        )
        first.quantity_available = 8
        first.quantity_reserved = 2
        first.estimated_unit_cost_snapshot = Decimal("1.2345")
        db.commit()
        source = db.get(InventoryPallet, source_pallet_id)
        target_pallet_id = target_lot.pallet_item.pallet_id
        target = db.get(InventoryPallet, target_pallet_id)
        source_version = source.version
        target_version = target.version
        lot_before = {
            row.id: (
                row.quantity_available,
                row.quantity_reserved,
                row.quantity_consumed,
                row.quantity_damaged,
                row.quantity_scrapped,
                row.stock_date,
                row.estimated_unit_cost_snapshot,
            )
            for row in (first, second, target_lot)
        }
        source_lot_ids = [first.id, second.id]

    payload = {
        "expected_version": source_version,
        "target_pallet_id": target_pallet_id,
        "expected_target_version": target_version,
        "confirmed": True,
        "idempotency_key": "p1-16e2-merge-success",
    }
    with TestClient(app) as client:
        _login(client, "floor3-admin")
        merged = client.post(
            f"/api/warehouse/pallets/{source_pallet_id}/merge-all",
            json=payload,
        )
        assert merged.status_code == 200, merged.text
        body = merged.json()
        assert body["idempotent_replay"] is False
        assert body["moved_item_count"] == 2
        assert body["source_pallet"]["is_current"] is False
        assert body["source_pallet"]["location_id"] is None
        assert body["target_pallet"]["item_count"] == 3

        replay = client.post(
            f"/api/warehouse/pallets/{source_pallet_id}/merge-all",
            json=payload,
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True

        source_location = client.get(
            f"/api/warehouse/floor3/locations/{ids['locations'][0]}"
        ).json()
        target_location = client.get(
            f"/api/warehouse/floor3/locations/{ids['locations'][1]}"
        ).json()
        assert source_location["occupancy_status"] == "empty"
        assert target_location["occupancy_status"] == "occupied"

    with factory() as db:
        source = db.get(InventoryPallet, source_pallet_id)
        target = db.get(InventoryPallet, target_pallet_id)
        assert source.status == "closed"
        assert source.version == source_version + 1
        assert target.status == "active"
        assert target.version == target_version + 1
        assert db.scalar(
            select(func.count(InventoryPalletItem.id)).where(
                InventoryPalletItem.pallet_id == target_pallet_id
            )
        ) == 3
        for lot_id, before in lot_before.items():
            lot = db.get(InventoryLot, lot_id)
            assert (
                lot.quantity_available,
                lot.quantity_reserved,
                lot.quantity_consumed,
                lot.quantity_damaged,
                lot.quantity_scrapped,
                lot.stock_date,
                lot.estimated_unit_cost_snapshot,
            ) == before
        for lot_id in source_lot_ids:
            lot = db.get(InventoryLot, lot_id)
            assert lot.warehouse_location_id == ids["locations"][1]
            assert lot.pallet_item.pallet_id == target_pallet_id
        lot_movements = db.scalars(
            select(InventoryMovement).where(
                InventoryMovement.movement_type == "location_transfer",
                InventoryMovement.inventory_lot_id.in_(source_lot_ids),
            )
        ).all()
        assert len(lot_movements) == 2
        assert all(row.quantity == 0 for row in lot_movements)
        pallet_movements = db.scalars(
            select(InventoryLocationMovement).where(
                InventoryLocationMovement.idempotency_key.in_(
                    ("p1-16e2-merge-success", "p1-16e2-merge-success:target")
                )
            )
        ).all()
        assert {row.movement_type for row in pallet_movements} == {
            "clear",
            "add_item",
        }
        from app.services.asset_time_archive import (
            build_inventory_lot_detail_timeline,
        )

        for lot_id in source_lot_ids:
            timeline = build_inventory_lot_detail_timeline(
                db,
                db.get(InventoryLot, lot_id),
            )
            merge_events = [
                event
                for event in timeline
                if event.get("movement_subtype") == "loose_goods_merge"
            ]
            assert len(merge_events) == 1
            merge_event = merge_events[0]
            assert merge_event["event_type"] == "pallet_location_move"
            assert merge_event["direction"] == "move"
            assert merge_event["from_location_id"] == ids["locations"][0]
            assert merge_event["to_location_id"] == ids["locations"][1]
            assert merge_event["source_pallet_id"] == source_pallet_id
            assert merge_event["target_pallet_id"] == target_pallet_id
            assert not any(
                event.get("event_type") == "inventory_location_transfer"
                and event.get("movement_id") == merge_event["movement_id"]
                for event in timeline
            )
        assert not any(
            event.get("movement_subtype") == "loose_goods_merge"
            for event in build_inventory_lot_detail_timeline(db, target_lot)
        )
        assert db.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.description.in_(
                    (
                        "源栈板全部剩余货物已合并并释放",
                        "目标栈板接收全部零散货",
                    )
                )
            )
        ) == 2


def test_p1_16e2_merge_rejects_customer_type_and_stale_target(
    floor3_app,
) -> None:
    from app.models.warehouse_inventory import InventoryPallet, InventoryPalletItem, WarehouseLocation
    from app.services.floor3_locations import create_pallet

    app, ids, factory = floor3_app
    with factory() as db:
        extra_locations = []
        for index in range(3):
            extra_locations.append(
                WarehouseLocation(
                    location_code=f"A1-L1{index + 3}",
                    location_name=f"A1-L1{index + 3}",
                    warehouse_type="finished",
                    warehouse_floor=3,
                    area_code="A1",
                    storage_type="ground",
                    sort_order=20 + index,
                    source_version="V11",
                )
            )
        db.add_all(extra_locations)
        db.flush()
        source = create_pallet(
            db,
            location_id=ids["locations"][0],
            pallet_code="PLT-P1-16E2-RULE-SOURCE",
            items=[_matched_item(ids["tianhua"], ids["products"][0], "TH-A")],
            remarks=None,
            operator_id=ids["admin"],
        )
        other_customer = create_pallet(
            db,
            location_id=ids["locations"][1],
            pallet_code="PLT-P1-16E2-OTHER",
            items=[_matched_item(ids["other"], ids["other_product"], "QT-A")],
            remarks=None,
            operator_id=ids["admin"],
        )
        other_type_item = _matched_item(
            ids["tianhua"], ids["products"][1], "TH-SEMI"
        )
        other_type_item.update(item_type="semi_finished", unit="sheets")
        other_type = create_pallet(
            db,
            location_id=extra_locations[0].id,
            pallet_code="PLT-P1-16E2-SEMI",
            items=[other_type_item],
            remarks=None,
            operator_id=ids["admin"],
        )
        compatible = create_pallet(
            db,
            location_id=extra_locations[1].id,
            pallet_code="PLT-P1-16E2-COMPATIBLE",
            items=[_matched_item(ids["tianhua"], ids["products"][2], "TH-B")],
            remarks=None,
            operator_id=ids["admin"],
        )
        db.commit()
        source_id = source.id
        source_version = source.version
        other_customer_id = other_customer.id
        other_customer_version = other_customer.version
        other_type_id = other_type.id
        other_type_version = other_type.version
        compatible_id = compatible.id
        compatible_version = compatible.version

    with TestClient(app) as client:
        _login(client, "floor3-admin")
        wrong_customer = client.post(
            f"/api/warehouse/pallets/{source_id}/merge-all",
            json={
                "expected_version": source_version,
                "target_pallet_id": other_customer_id,
                "expected_target_version": other_customer_version,
                "confirmed": True,
                "idempotency_key": "p1-16e2-wrong-customer",
            },
        )
        assert wrong_customer.status_code == 409
        assert "同一客户" in wrong_customer.json()["detail"]

        wrong_type = client.post(
            f"/api/warehouse/pallets/{source_id}/merge-all",
            json={
                "expected_version": source_version,
                "target_pallet_id": other_type_id,
                "expected_target_version": other_type_version,
                "confirmed": True,
                "idempotency_key": "p1-16e2-wrong-type",
            },
        )
        assert wrong_type.status_code == 409
        assert "同一库存类型" in wrong_type.json()["detail"]

        stale_target = client.post(
            f"/api/warehouse/pallets/{source_id}/merge-all",
            json={
                "expected_version": source_version,
                "target_pallet_id": compatible_id,
                "expected_target_version": compatible_version + 99,
                "confirmed": True,
                "idempotency_key": "p1-16e2-stale-target",
            },
        )
        assert stale_target.status_code == 409
        assert "其他操作更新" in stale_target.json()["detail"]

    with factory() as db:
        source = db.get(InventoryPallet, source_id)
        assert source.is_current is True
        assert source.location_id == ids["locations"][0]
        assert source.version == source_version
        assert db.scalar(
            select(func.count(InventoryPalletItem.id)).where(
                InventoryPalletItem.pallet_id == source_id
            )
        ) == 1


def test_floor3_locations_filter_by_customer_and_combine_with_existing_filters(
    floor3_app,
) -> None:
    app, ids, _factory = floor3_app
    with TestClient(app) as client:
        _login(client, "floor3-admin")
        pallet_specs = [
            (
                ids["locations"][0],
                [
                    _matched_item(
                        ids["tianhua"],
                        ids["products"][0],
                        "FILTER-TIANHUA-A1",
                    )
                ],
            ),
            (
                ids["locations"][1],
                [
                    _matched_item(
                        ids["other"],
                        ids["other_product"],
                        "FILTER-OTHER-A1",
                    )
                ],
            ),
            (
                ids["locations"][2],
                [
                    _matched_item(
                        ids["tianhua"],
                        ids["products"][1],
                        "FILTER-TIANHUA-F12",
                    ),
                    _matched_item(
                        ids["other"],
                        ids["other_product"],
                        "FILTER-OTHER-F12",
                    ),
                ],
            ),
        ]
        for location_id, items in pallet_specs:
            created = client.post(
                "/api/warehouse/pallets",
                json={"location_id": location_id, "items": items},
            )
            assert created.status_code == 201, created.text

        tianhua = client.get(
            "/api/warehouse/floor3/locations",
            params={"customer_id": ids["tianhua"]},
        )
        assert tianhua.status_code == 200, tianhua.text
        assert [row["id"] for row in tianhua.json()["items"]] == [
            ids["locations"][0],
            ids["locations"][2],
        ]
        assert tianhua.json()["total"] == 2

        other = client.get(
            "/api/warehouse/floor3/locations",
            params={"customer_id": ids["other"]},
        )
        assert other.status_code == 200, other.text
        assert [row["id"] for row in other.json()["items"]] == [
            ids["locations"][1],
            ids["locations"][2],
        ]

        combined = client.get(
            "/api/warehouse/floor3/locations",
            params={
                "customer_id": ids["tianhua"],
                "q": "21301011",
                "area_code": "A1",
                "occupancy": "occupied",
            },
        )
        assert combined.status_code == 200, combined.text
        assert [row["id"] for row in combined.json()["items"]] == [
            ids["locations"][0]
        ]

        empty_intersection = client.get(
            "/api/warehouse/floor3/locations",
            params={
                "customer_id": ids["tianhua"],
                "area_code": "A1",
                "occupancy": "empty",
            },
        )
        assert empty_intersection.status_code == 200, empty_intersection.text
        assert empty_intersection.json() == {"items": [], "total": 0}


def test_floor3_locations_customer_filter_enforces_scope_and_positive_id(
    floor3_app,
) -> None:
    app, ids, _factory = floor3_app
    with TestClient(app) as client:
        _login(client, "floor3-admin")
        for location_id, customer_id, product_id, code in [
            (
                ids["locations"][0],
                ids["tianhua"],
                ids["products"][0],
                "SCOPED-TIANHUA",
            ),
            (
                ids["locations"][1],
                ids["other"],
                ids["other_product"],
                "SCOPED-OTHER",
            ),
        ]:
            created = client.post(
                "/api/warehouse/pallets",
                json={
                    "location_id": location_id,
                    "items": [_matched_item(customer_id, product_id, code)],
                },
            )
            assert created.status_code == 201, created.text
        client.post("/api/auth/logout")

        _login(client, "floor3-scoped")
        allowed = client.get(
            "/api/warehouse/floor3/locations",
            params={"customer_id": ids["tianhua"]},
        )
        assert allowed.status_code == 200, allowed.text
        assert [row["id"] for row in allowed.json()["items"]] == [
            ids["locations"][0]
        ]

        denied = client.get(
            "/api/warehouse/floor3/locations",
            params={"customer_id": ids["other"]},
        )
        assert denied.status_code == 403, denied.text

        invalid = client.get(
            "/api/warehouse/floor3/locations",
            params={"customer_id": 0},
        )
        assert invalid.status_code == 422, invalid.text


def test_floor3_customer_abbreviation_search_is_scoped_and_does_not_auto_select(
    floor3_app,
) -> None:
    app, ids, _factory = floor3_app
    with TestClient(app) as client:
        _login(client, "floor3-admin")
        for location_id, customer_id, product_id, code in [
            (
                ids["locations"][0],
                ids["tianhua"],
                ids["products"][0],
                "ABBR-TIANHUA",
            ),
            (
                ids["locations"][1],
                ids["other"],
                ids["other_product"],
                "ABBR-QT",
            ),
        ]:
            created = client.post(
                "/api/warehouse/pallets",
                json={
                    "location_id": location_id,
                    "items": [_matched_item(customer_id, product_id, code)],
                },
            )
            assert created.status_code == 201, created.text

        references = client.get("/api/warehouse/references/customers")
        assert references.status_code == 200, references.text
        assert {
            (row["id"], row["customer_code"]) for row in references.json()["items"]
        } == {
            (ids["tianhua"], "TH"),
            (ids["other"], "QT"),
        }

        tianhua = client.get(
            "/api/warehouse/floor3/locations",
            params={"q": "TH"},
        )
        assert tianhua.status_code == 200, tianhua.text
        assert ids["locations"][0] in {
            row["id"] for row in tianhua.json()["items"]
        }
        other = client.get(
            "/api/warehouse/floor3/locations",
            params={"q": "QT"},
        )
        assert other.status_code == 200, other.text
        assert [row["id"] for row in other.json()["items"]] == [
            ids["locations"][1]
        ]
        client.post("/api/auth/logout")

        _login(client, "floor3-scoped")
        scoped_references = client.get("/api/warehouse/references/customers")
        assert scoped_references.status_code == 200, scoped_references.text
        assert scoped_references.json()["items"] == [
                {
                    "id": ids["tianhua"],
                    "name": "苏州天华超净科技股份有限公司",
                    "chinese_short_name": None,
                    "customer_code": "TH",
                    "customer_number": None,
                }
            ]
        allowed = client.get(
            "/api/warehouse/floor3/locations",
            params={"q": "TH"},
        )
        assert allowed.status_code == 200, allowed.text
        assert [row["id"] for row in allowed.json()["items"]] == [
            ids["locations"][0]
        ]
        denied = client.get(
            "/api/warehouse/floor3/locations",
            params={"q": "QT"},
        )
        assert denied.status_code == 200, denied.text
        assert denied.json() == {"items": [], "total": 0}


def test_floor3_search_requires_explicit_candidate_choice(floor3_app) -> None:
    app, ids, _factory = floor3_app
    with TestClient(app) as client:
        _login(client, "floor3-admin")
        exact = client.get(
            "/api/warehouse/floor3/product-candidates",
            params={"customer_id": ids["tianhua"], "q": "21301011"},
        )
        assert exact.status_code == 200, exact.text
        body = exact.json()
        assert body["auto_bind_allowed"] is False
        assert body["selection_required"] is True
        assert body["items"][0]["product_id"] == ids["products"][0]
        assert body["items"][0]["match_type"] == "customer_inventory_code_exact"

        by_order = client.get(
            "/api/warehouse/floor3/product-candidates",
            params={"customer_id": ids["tianhua"], "q": "PO-FLOOR3-001"},
        )
        assert by_order.status_code == 200, by_order.text
        assert by_order.json()["items"][0]["product_id"] == ids["products"][0]
        assert by_order.json()["items"][0]["match_type"] == "customer_order_exact"

        duplicate_name = client.get(
            "/api/warehouse/floor3/product-candidates",
            params={"customer_id": ids["tianhua"], "q": "同名纸箱"},
        )
        assert duplicate_name.status_code == 200, duplicate_name.text
        assert {row["product_id"] for row in duplicate_name.json()["items"]} == set(
            ids["products"][:2]
        )
        assert duplicate_name.json()["auto_bind_allowed"] is False

        by_customer_name = client.get(
            "/api/warehouse/floor3/product-candidates",
            params={"q": "苏州天华超净科技股份有限公司"},
        )
        assert by_customer_name.status_code == 200, by_customer_name.text
        assert {row["product_id"] for row in by_customer_name.json()["items"]} == set(
            ids["products"]
        )
        assert by_customer_name.json()["auto_bind_allowed"] is False
        assert by_customer_name.json()["selection_required"] is True


def test_floor3_pallet_supports_five_items_move_clear_and_history(floor3_app) -> None:
    from app.models.warehouse_inventory import (
        InventoryLocationMovement,
        InventoryLot,
        InventoryPallet,
        InventoryPalletItem,
    )

    app, ids, factory = floor3_app
    source_layout_version = _ensure_location_layout_version(
        factory, ids["locations"][0]
    )
    target_layout_version = _ensure_location_layout_version(
        factory, ids["locations"][1], left_pct=30
    )
    with TestClient(app) as client:
        _login(client, "floor3-admin")
        items = [
            _matched_item(ids["tianhua"], product_id, f"CODE-{index}")
            for index, product_id in enumerate(ids["products"], start=1)
        ]
        created = client.post(
            "/api/warehouse/pallets",
            json={
                "location_id": ids["locations"][0],
                "expected_layout_version": source_layout_version,
                "pallet_code": "PLT-3F-TEST-001",
                "items": items,
            },
        )
        assert created.status_code == 201, created.text
        pallet_id = created.json()["pallet"]["id"]
        assert created.json()["pallet"]["item_count"] == 5

        occupied = client.post(
            "/api/warehouse/pallets",
            json={
                "location_id": ids["locations"][0],
                "pallet_code": "PLT-3F-TEST-OCCUPIED",
                "items": [items[0]],
            },
        )
        assert occupied.status_code == 409

        moved = client.post(
            f"/api/warehouse/pallets/{pallet_id}/move",
            json={
                "expected_version": created.json()["pallet"]["version"],
                "to_location_id": ids["locations"][1],
                "expected_target_layout_version": target_layout_version,
                "confirmed": True,
                "idempotency_key": "move-basic-001",
                "remarks": "现场移位",
            },
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["pallet"]["location_id"] == ids["locations"][1]

        first = client.get(
            f"/api/warehouse/floor3/locations/{ids['locations'][0]}"
        )
        second = client.get(
            f"/api/warehouse/floor3/locations/{ids['locations'][1]}"
        )
        assert first.json()["occupancy_status"] == "empty"
        assert second.json()["occupancy_status"] == "occupied"
        assert any(
            row["movement_type"] == "move" for row in first.json()["movement_history"]
        )

        cleared = client.post(
            f"/api/warehouse/pallets/{pallet_id}/clear",
            json={
                "expected_version": moved.json()["pallet"]["version"],
            },
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["pallet"]["is_current"] is False
        assert client.get(
            f"/api/warehouse/floor3/locations/{ids['locations'][1]}"
        ).json()["occupancy_status"] == "empty"

    with factory() as db:
        assert db.scalar(select(func.count(InventoryPalletItem.id))) == 5
        assert db.scalar(select(func.count(InventoryLocationMovement.id))) == 3
        assert db.scalar(select(func.count(InventoryLot.id))) == 0
        pallet = db.scalar(select(InventoryPallet).where(InventoryPallet.id == pallet_id))
        assert pallet is not None
        assert pallet.status == "closed"
        assert pallet.location_id is None


def test_released_snapshot_pallet_is_not_reused_as_empty(floor3_app) -> None:
    app, ids, factory = floor3_app
    item = _matched_item(ids["tianhua"], ids["products"][0], "SNAPSHOT-STILL-HERE")
    with TestClient(app) as client:
        _login(client, "floor3-admin")
        created = client.post(
            "/api/warehouse/pallets",
            json={
                "location_id": ids["locations"][0],
                "pallet_code": "PLT-SNAPSHOT-NOT-EMPTY",
                "items": [item],
            },
        )
        assert created.status_code == 201, created.text
        old_pallet_id = int(created.json()["pallet"]["id"])
        cleared = client.post(
            f"/api/warehouse/pallets/{old_pallet_id}/clear",
            json={"expected_version": created.json()["pallet"]["version"]},
        )
        assert cleared.status_code == 200, cleared.text

        inbound = client.post(
            "/api/warehouse/pallets",
            json={
                "location_id": ids["locations"][1],
                "items": [
                    _matched_item(
                        ids["tianhua"], ids["products"][1], "NEW-SNAPSHOT"
                    )
                ],
            },
        )
        assert inbound.status_code == 201, inbound.text
        assert int(inbound.json()["pallet"]["id"]) != old_pallet_id

    from app.models.warehouse_inventory import InventoryPallet

    with factory() as db:
        old_pallet = db.get(InventoryPallet, old_pallet_id)
        assert old_pallet is not None
        assert old_pallet.is_current is False
        assert old_pallet.location_id is None
        assert len(old_pallet.items) == 1


def test_floor3_same_product_can_be_registered_at_multiple_locations(floor3_app) -> None:
    app, ids, _factory = floor3_app
    with TestClient(app) as client:
        _login(client, "floor3-admin")
        item = _matched_item(ids["tianhua"], ids["products"][0], "21301011")
        first = client.post(
            "/api/warehouse/pallets",
            json={"location_id": ids["locations"][0], "items": [item]},
        )
        second = client.post(
            "/api/warehouse/pallets",
            json={"location_id": ids["locations"][1], "items": [item]},
        )
        assert first.status_code == 201, first.text
        assert second.status_code == 201, second.text


def test_floor3_requires_both_floor_three_and_v11_source_boundary(floor3_app) -> None:
    app, ids, _factory = floor3_app
    with TestClient(app) as client:
        _login(client, "floor3-admin")
        listing = client.get("/api/warehouse/floor3/locations")
        assert listing.status_code == 200, listing.text
        assert {row["id"] for row in listing.json()["items"]} == set(
            [*ids["locations"][:3], ids["f34_location"], ids["rack_location"]]
        )

        for location_id in (
            ids["legacy_floor3_location"],
            ids["v11_wrong_floor_location"],
        ):
            detail = client.get(f"/api/warehouse/floor3/locations/{location_id}")
            assert detail.status_code == 404
            created = client.post(
                "/api/warehouse/pallets",
                json={
                    "location_id": location_id,
                    "items": [
                        _matched_item(
                            ids["tianhua"], ids["products"][0], "BOUNDARY"
                        )
                    ],
                },
            )
            assert created.status_code == 409, created.text
            assert "目标货位不可用" in created.json()["detail"]
            assert "V11" not in created.json()["detail"]

        formal_locations = client.get("/api/warehouse/locations")
        assert formal_locations.status_code == 200, formal_locations.text
        assert ids["legacy_floor3_location"] in {
            row["id"] for row in formal_locations.json()["items"]
        }
        assert ids["v11_wrong_floor_location"] not in {
            row["id"] for row in formal_locations.json()["items"]
        }

        wrong_floor_v11_in = client.post(
            "/api/warehouse/finished/manual-in",
            json={
                "customer_id": ids["tianhua"],
                "product_id": ids["products"][0],
                "location_id": ids["v11_wrong_floor_location"],
                "quantity": 1,
                "stock_date": "2026-07-15",
            },
        )
        assert wrong_floor_v11_in.status_code == 409
        assert "楼层无效" in wrong_floor_v11_in.json()["detail"]

        formal_config_attempt = client.put(
            f"/api/warehouse/locations/{ids['locations'][0]}/disable"
        )
        assert formal_config_attempt.status_code == 409
        assert "三楼平面图" in formal_config_attempt.json()["detail"]


def test_floor3_pallet_mutations_use_expected_version_cas(floor3_app) -> None:
    from app.models.warehouse_inventory import InventoryPalletItem

    app, ids, factory = floor3_app
    with TestClient(app) as client:
        _login(client, "floor3-admin")
        created = client.post(
            "/api/warehouse/pallets",
            json={
                "location_id": ids["locations"][0],
                "items": [
                    _matched_item(ids["tianhua"], ids["products"][0], "CAS-1")
                ],
            },
        )
        assert created.status_code == 201, created.text
        pallet = created.json()["pallet"]
        assert pallet["version"] == 1

        first_writer = client.post(
            f"/api/warehouse/pallets/{pallet['id']}/items",
            json={
                "expected_version": 1,
                "item": _matched_item(
                    ids["tianhua"], ids["products"][1], "CAS-2"
                ),
            },
        )
        assert first_writer.status_code == 200, first_writer.text
        assert first_writer.json()["pallet"]["version"] == 2

        stale_add = client.post(
            f"/api/warehouse/pallets/{pallet['id']}/items",
            json={
                "expected_version": 1,
                "item": _matched_item(
                    ids["tianhua"], ids["products"][2], "CAS-STALE"
                ),
            },
        )
        assert stale_add.status_code == 409
        assert "其他操作更新" in stale_add.json()["detail"]

        stale_move = client.post(
            f"/api/warehouse/pallets/{pallet['id']}/move",
            json={
                "expected_version": 1,
                "to_location_id": ids["locations"][1],
                "expected_target_layout_version": 1,
                "confirmed": True,
                "idempotency_key": "move-stale-001",
            },
        )
        assert stale_move.status_code == 409

        moved = client.post(
            f"/api/warehouse/pallets/{pallet['id']}/move",
            json={
                "expected_version": 2,
                "to_location_id": ids["locations"][1],
                "confirmed": True,
                "idempotency_key": "move-cas-001",
            },
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["pallet"]["version"] == 3

        stale_clear = client.post(
            f"/api/warehouse/pallets/{pallet['id']}/clear",
            json={"expected_version": 2, "remarks": "过期页面清空"},
        )
        assert stale_clear.status_code == 409

        cleared = client.post(
            f"/api/warehouse/pallets/{pallet['id']}/clear",
            json={"expected_version": 3, "remarks": "当前版本清空"},
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["pallet"]["version"] == 4

    with factory() as db:
        assert db.scalar(
            select(func.count(InventoryPalletItem.id)).where(
                InventoryPalletItem.pallet_id == pallet["id"]
            )
        ) == 2


def test_floor3_competing_moves_return_conflict_without_claiming_loser_version(
    floor3_app,
) -> None:
    app, ids, _factory = floor3_app
    with TestClient(app) as client:
        _login(client, "floor3-admin")
        first = client.post(
            "/api/warehouse/pallets",
            json={
                "location_id": ids["locations"][0],
                "pallet_code": "PLT-COMPETE-ONE",
                "items": [
                    _matched_item(ids["tianhua"], ids["products"][0], "COMPETE-1")
                ],
            },
        )
        second = client.post(
            "/api/warehouse/pallets",
            json={
                "location_id": ids["locations"][1],
                "pallet_code": "PLT-COMPETE-TWO",
                "items": [
                    _matched_item(ids["tianhua"], ids["products"][1], "COMPETE-2")
                ],
            },
        )
        assert first.status_code == second.status_code == 201

        winner = client.post(
            f"/api/warehouse/pallets/{first.json()['pallet']['id']}/move",
            json={
                "expected_version": first.json()["pallet"]["version"],
                "to_location_id": ids["locations"][2],
                "confirmed": True,
                "idempotency_key": "move-winner-001",
            },
        )
        assert winner.status_code == 200, winner.text

        loser = client.post(
            f"/api/warehouse/pallets/{second.json()['pallet']['id']}/move",
            json={
                "expected_version": second.json()["pallet"]["version"],
                "to_location_id": ids["locations"][2],
                "confirmed": True,
                "idempotency_key": "move-loser-001",
            },
        )
        assert loser.status_code == 409, loser.text
        assert "目标货位已有当前栈板" in loser.json()["detail"]

        second_location = client.get(
            f"/api/warehouse/floor3/locations/{ids['locations'][1]}"
        )
        assert second_location.status_code == 200, second_location.text
        assert second_location.json()["current_pallet"]["version"] == 1


def test_floor3_pallet_item_validates_ddl_length_and_decimal_precision(
    floor3_app,
) -> None:
    app, ids, _factory = floor3_app
    with TestClient(app) as client:
        _login(client, "floor3-admin")
        too_long_order = client.post(
            "/api/warehouse/pallets",
            json={
                "location_id": ids["locations"][0],
                "items": [
                    {
                        **_matched_item(
                            ids["tianhua"], ids["products"][0], "DDL-ORDER"
                        ),
                        "order_no": "O" * 101,
                    }
                ],
            },
        )
        assert too_long_order.status_code == 422

        too_precise_quantity = client.post(
            "/api/warehouse/pallets",
            json={
                "location_id": ids["locations"][0],
                "items": [
                    {
                        **_matched_item(
                            ids["tianhua"], ids["products"][0], "DDL-QUANTITY"
                        ),
                        "quantity": "1.0001",
                    }
                ],
            },
        )
        assert too_precise_quantity.status_code == 422

        too_large_quantity = client.post(
            "/api/warehouse/pallets",
            json={
                "location_id": ids["locations"][0],
                "items": [
                    {
                        **_matched_item(
                            ids["tianhua"], ids["products"][0], "DDL-RANGE"
                        ),
                        "quantity": "100000000000.000",
                    }
                ],
            },
        )
        assert too_large_quantity.status_code == 422


def test_formal_inventory_service_binds_valid_v11_and_rejects_wrong_floor(
    floor3_app,
) -> None:
    from app.services.warehouse_inventory import (
        WarehouseInventoryError,
        manual_finished_in,
    )

    _app, ids, factory = floor3_app
    with factory() as db:
        lot = manual_finished_in(
            db,
            customer_id=ids["tianhua"],
            product_id=ids["products"][0],
            location_id=ids["locations"][0],
            quantity=1,
            stock_date=date(2026, 7, 15),
            source_type="manual",
            remarks=None,
            operator_id=ids["admin"],
            idempotency_key="floor3-service-bind-001",
        )
        assert lot.pallet_item is not None
        assert lot.pallet_item.pallet.location_id == ids["locations"][0]
        with pytest.raises(WarehouseInventoryError, match="楼层无效"):
            manual_finished_in(
                db,
                customer_id=ids["tianhua"],
                product_id=ids["products"][0],
                location_id=ids["v11_wrong_floor_location"],
                quantity=1,
                stock_date=date(2026, 7, 15),
                source_type="manual",
                remarks=None,
                operator_id=ids["admin"],
                idempotency_key=None,
            )


def test_floor3_scope_redacts_and_rejects_other_customer_content(floor3_app) -> None:
    app, ids, _factory = floor3_app
    with TestClient(app) as client:
        _login(client, "floor3-admin")
        created = client.post(
            "/api/warehouse/pallets",
            json={
                "location_id": ids["locations"][0],
                "pallet_code": "SECRET-OTHER-PALLET",
                "remarks": "其他客户秘密订单号 PO-SECRET-001",
                "items": [
                    _matched_item(
                        ids["other"], ids["other_product"], "OTHER-001"
                    )
                ],
            },
        )
        assert created.status_code == 201, created.text
        client.post("/api/auth/logout")

        _login(client, "floor3-scoped")
        listing = client.get("/api/warehouse/floor3/locations")
        assert listing.status_code == 200, listing.text
        occupied = next(
            row for row in listing.json()["items"] if row["occupancy_status"] == "occupied"
        )
        assert occupied["current_pallet"] == {"access_restricted": True}
        assert "SECRET-OTHER-PALLET" not in listing.text
        assert "PO-SECRET-001" not in listing.text

        detail = client.get(
            f"/api/warehouse/floor3/locations/{ids['locations'][0]}"
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["current_pallet"] == {"access_restricted": True}
        assert "SECRET-OTHER-PALLET" not in detail.text
        assert "PO-SECRET-001" not in detail.text

        secret_search = client.get(
            "/api/warehouse/floor3/locations",
            params={"q": "SECRET-OTHER-PALLET"},
        )
        assert secret_search.status_code == 200, secret_search.text
        assert secret_search.json()["items"] == []

        denied_search = client.get(
            "/api/warehouse/floor3/product-candidates",
            params={"customer_id": ids["other"], "q": "OTHER-001"},
        )
        assert denied_search.status_code == 403
        unscoped_pending = client.post(
            "/api/warehouse/pallets",
            json={
                "location_id": ids["locations"][1],
                "items": [
                    {
                        "inventory_code": "UNKNOWN",
                        "product_name": "待确认纸箱",
                        "item_type": "finished",
                        "quantity": 1,
                        "match_status": "pending",
                    }
                ],
            },
        )
        assert unscoped_pending.status_code == 403
        denied_write = client.post(
            f"/api/warehouse/pallets/{created.json()['pallet']['id']}/items",
            json={
                "expected_version": created.json()["pallet"]["version"],
                "item": {
                    "customer_id": ids["tianhua"],
                    "product_id": ids["products"][0],
                    "item_type": "finished",
                    "quantity": 1,
                    "match_status": "matched",
                },
            },
        )
        assert denied_write.status_code == 403

        hidden_customer_name = client.get(
            "/api/warehouse/floor3/product-candidates",
            params={"q": "其他客户"},
        )
        assert hidden_customer_name.status_code == 200, hidden_customer_name.text
        assert hidden_customer_name.json()["items"] == []


def test_lot_detail_hides_free_text_audit_fields_from_scoped_users(
    floor3_app,
) -> None:
    from app.models.warehouse_inventory import InventoryMovement, InventoryReservation
    from app.services.warehouse_inventory import manual_finished_in

    app, ids, factory = floor3_app
    with factory() as db:
        lot = manual_finished_in(
            db,
            customer_id=ids["tianhua"],
            product_id=ids["products"][0],
            location_id=ids["locations"][0],
            quantity=7,
            stock_date=date(2026, 7, 15),
            source_type="manual",
            remarks=None,
            operator_id=ids["admin"],
            idempotency_key="scoped-lot-detail-base",
        )
        db.flush()
        db.add(
            InventoryMovement(
                movement_number="SCOPED-DETAIL-001",
                inventory_lot_id=lot.id,
                movement_type="damage",
                quantity=1,
                unit="boxes",
                before_available=7,
                after_available=6,
                before_reserved=0,
                after_reserved=0,
                before_consumed=0,
                after_consumed=0,
                before_damaged=0,
                after_damaged=1,
                before_scrapped=0,
                after_scrapped=0,
                reason="SECRET-REASON-PO-001",
                remarks="SECRET-REMARK-CUSTOMER-001",
                operator_id=ids["admin"],
                idempotency_key="scoped-lot-detail-secret",
            )
        )
        db.add(
            InventoryReservation(
                reservation_number="SCOPED-RESERVATION-001",
                inventory_lot_id=lot.id,
                reservation_type="finished_order",
                reserved_stock_quantity=1,
                consumed_stock_quantity=0,
                released_stock_quantity=1,
                consumed_requirement_quantity=0,
                released_requirement_quantity=0,
                status="released",
                release_reason="SECRET-RELEASE-REASON-001",
                idempotency_key="scoped-reservation-secret",
            )
        )
        db.commit()
        lot_id = lot.id

    with TestClient(app) as scoped_client:
        _login(scoped_client, "floor3-scoped")
        scoped_detail = scoped_client.get(f"/api/warehouse/lots/{lot_id}")
        assert scoped_detail.status_code == 200, scoped_detail.text
        scoped_text = scoped_detail.text
        assert "SECRET-REASON-PO-001" not in scoped_text
        assert "SECRET-REMARK-CUSTOMER-001" not in scoped_text
        assert "SECRET-RELEASE-REASON-001" not in scoped_text
        scoped_event = next(
            event
            for event in scoped_detail.json()["timeline"]
            if event.get("movement_number") == "SCOPED-DETAIL-001"
        )
        assert scoped_event["reason"] is None
        assert scoped_event["remarks"] is None
        assert "operator_name" not in scoped_event
        assert scoped_detail.json()["reservations"][0]["release_reason"] is None
        assert scoped_detail.json()["movements"][0]["reason"] is None

    with TestClient(app) as admin_client:
        _login(admin_client, "floor3-admin")
        admin_detail = admin_client.get(f"/api/warehouse/lots/{lot_id}")
        assert admin_detail.status_code == 200, admin_detail.text
        admin_event = next(
            event
            for event in admin_detail.json()["timeline"]
            if event.get("movement_number") == "SCOPED-DETAIL-001"
        )
        assert admin_event["reason"] == "SECRET-REASON-PO-001"
        assert admin_event["remarks"] == "SECRET-REMARK-CUSTOMER-001"
        assert admin_event["operator_name"] == "三楼测试管理员"
        assert (
            admin_detail.json()["reservations"][0]["release_reason"]
            == "SECRET-RELEASE-REASON-001"
        )
        assert admin_detail.json()["movements"][0]["reason"] == "SECRET-REASON-PO-001"


def test_floor3_scope_treats_mixed_customer_pallet_as_restricted_occupancy(
    floor3_app,
) -> None:
    app, ids, _factory = floor3_app
    with TestClient(app) as client:
        _login(client, "floor3-admin")
        created = client.post(
            "/api/warehouse/pallets",
            json={
                "location_id": ids["locations"][1],
                "pallet_code": "SECRET-MIXED-PALLET",
                "remarks": "混合客户秘密备注",
                "items": [
                    _matched_item(ids["tianhua"], ids["products"][0], "21301011"),
                    _matched_item(ids["other"], ids["other_product"], "OTHER-001"),
                ],
            },
        )
        assert created.status_code == 201, created.text
        pallet_id = created.json()["pallet"]["id"]
        client.post("/api/auth/logout")

        _login(client, "floor3-scoped")
        detail = client.get(
            f"/api/warehouse/floor3/locations/{ids['locations'][1]}"
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["occupancy_status"] == "occupied"
        assert detail.json()["current_pallet"] == {"access_restricted": True}
        assert "SECRET-MIXED-PALLET" not in detail.text
        assert "混合客户秘密备注" not in detail.text
        assert "21301011" not in detail.text

        write_attempts = [
            client.post(
                f"/api/warehouse/pallets/{pallet_id}/items",
                json={
                    "expected_version": created.json()["pallet"]["version"],
                    "item": _matched_item(
                        ids["tianhua"], ids["products"][1], "21301012"
                    ),
                },
            ),
            client.post(
                f"/api/warehouse/pallets/{pallet_id}/move",
                json={
                    "expected_version": created.json()["pallet"]["version"],
                    "to_location_id": ids["locations"][2],
                    "confirmed": True,
                    "idempotency_key": "move-scoped-001",
                },
            ),
            client.post(
                f"/api/warehouse/pallets/{pallet_id}/clear",
                json={
                    "expected_version": created.json()["pallet"]["version"],
                    "remarks": "无权清空",
                },
            ),
            client.post(
                f"/api/warehouse/pallets/{pallet_id}/relocation-flag",
                json={
                    "expected_version": created.json()["pallet"]["version"],
                    "needs_relocation": True,
                    "remarks": "无权标记",
                },
            ),
        ]
        assert [response.status_code for response in write_attempts] == [403] * 4


def test_floor3_finished_manual_in_is_one_ledger_and_moves_with_pallet(
    floor3_app,
) -> None:
    app, ids, factory = floor3_app
    with TestClient(app) as client:
        _login(client, "floor3-admin")
        locations = client.get("/api/warehouse/locations")
        assert locations.status_code == 200, locations.text
        location_ids = {row["id"] for row in locations.json()["items"]}
        assert ids["formal_location"] in location_ids
        assert ids["legacy_floor3_location"] in location_ids
        assert ids["locations"][0] in location_ids
        assert ids["v11_wrong_floor_location"] not in location_ids

        payload = {
            "customer_id": ids["tianhua"],
            "product_id": ids["products"][0],
            "location_id": ids["locations"][0],
            "quantity": 10,
            "stock_date": "2026-07-14",
            "idempotency_key": "floor3-finished-in-001",
        }
        finished = client.post(
            "/api/warehouse/finished/manual-in",
            json=payload,
        )
        assert finished.status_code == 200, finished.text
        lot = finished.json()
        assert lot["quantity_available"] == 10
        assert lot["location"]["id"] == ids["locations"][0]
        assert lot["floor3_binding"]["pallet_id"]
        replay = client.post("/api/warehouse/finished/manual-in", json=payload)
        assert replay.status_code == 200, replay.text
        assert replay.json()["id"] == lot["id"]

        detail = client.get(
            f"/api/warehouse/floor3/locations/{ids['locations'][0]}"
        )
        assert detail.status_code == 200, detail.text
        pallet = detail.json()["current_pallet"]
        assert pallet["items"][0]["inventory_lot_id"] == lot["id"]
        assert pallet["items"][0]["official_inventory"] is True
        assert pallet["items"][0]["quantity"] == 10

        moved = client.post(
            f"/api/warehouse/pallets/{pallet['id']}/move",
            json={
                "expected_version": pallet["version"],
                "to_location_id": ids["locations"][1],
                "confirmed": True,
                "idempotency_key": "floor3-finished-move-001",
                "remarks": "成品转到相邻货位",
            },
        )
        assert moved.status_code == 200, moved.text
        lots = client.get("/api/warehouse/lots", params={"inventory_type": "finished"})
        moved_lot = next(row for row in lots.json()["items"] if row["id"] == lot["id"])
        assert moved_lot["location"]["id"] == ids["locations"][1]
        assert moved_lot["quantity_available"] == 10

        adjusted = client.post(
            f"/api/warehouse/lots/{lot['id']}/adjust",
            json={
                "expected_version": moved_lot["version"],
                "quantity_delta": 5,
                "reason": "模拟盘点增加",
            },
        )
        assert adjusted.status_code == 200, adjusted.text
        moved_detail = client.get(
            f"/api/warehouse/floor3/locations/{ids['locations'][1]}"
        ).json()
        assert moved_detail["current_pallet"]["items"][0]["quantity"] == 15
        assert moved_detail["current_pallet"]["items"][0]["quantity_available"] == 15

        blocked_clear = client.post(
            f"/api/warehouse/pallets/{pallet['id']}/clear",
            json={
                "expected_version": moved.json()["pallet"]["version"],
                "remarks": "不应允许清空仍有库存的栈板",
            },
        )
        assert blocked_clear.status_code == 409
        assert "正式成品库存" in blocked_clear.json()["detail"]

        semi_finished = client.post(
            "/api/warehouse/semi-finished/manual-in",
            json={
                "location_id": ids["locations"][0],
                "quantity": 10,
                "stock_date": "2026-07-14",
                "material_code": "K616K",
                "layer_count": 5,
                "flute_type": "AB",
                "board_length_mm": 1000,
                "board_width_mm": 800,
                "sheet_type": "raw",
            },
        )
        assert semi_finished.status_code == 409, semi_finished.text
        assert "目前只接入成品仓" in semi_finished.json()["detail"]

    with factory() as db:
        from app.models.warehouse_inventory import (
            InventoryLot,
            InventoryPallet,
            InventoryPalletItem,
        )

        assert db.scalar(select(func.count(InventoryLot.id))) == 1
        assert db.scalar(select(func.count(InventoryPallet.id))) == 1
        assert db.scalar(select(func.count(InventoryPalletItem.id))) == 1


def test_v11_inventory_lot_respects_customer_scope_and_admin_can_mutate(
    floor3_app,
) -> None:
    from app.models.warehouse_inventory import InventoryLot, InventoryMovement
    from app.services.warehouse_inventory import manual_finished_in

    app, ids, factory = floor3_app
    with factory() as db:
        lot = manual_finished_in(
            db,
            customer_id=ids["other"],
            product_id=ids["other_product"],
            location_id=ids["locations"][0],
            quantity=7,
            stock_date=date(2026, 7, 15),
            source_type="manual",
            remarks=None,
            operator_id=ids["admin"],
            idempotency_key=None,
        )
        db.commit()
        lot_id = lot.id

    adjust_payload = {
        "expected_version": 1,
        "quantity_delta": 5,
        "reason": "raw id must not bypass V11 isolation",
    }
    with TestClient(app) as client:
        _login(client, "floor3-scoped")
        scoped_attempt = client.post(
            f"/api/warehouse/lots/{lot_id}/adjust",
            json=adjust_payload,
        )
        assert scoped_attempt.status_code == 403, scoped_attempt.text

        client.post("/api/auth/logout")
        _login(client, "floor3-admin")
        admin_attempt = client.post(
            f"/api/warehouse/lots/{lot_id}/adjust",
            json=adjust_payload,
        )
        assert admin_attempt.status_code == 200, admin_attempt.text

    with factory() as db:
        lot = db.get(InventoryLot, lot_id)
        assert lot is not None
        assert lot.quantity_available == 12
        assert lot.version == 2
        assert db.scalar(
            select(func.count(InventoryMovement.id)).where(
                InventoryMovement.inventory_lot_id == lot_id,
                InventoryMovement.movement_type == "adjust",
            )
        ) == 1


def test_floor3_temporary_location_marks_pallet_for_relocation(floor3_app) -> None:
    app, ids, _factory = floor3_app
    with TestClient(app) as client:
        _login(client, "floor3-admin")
        created = client.post(
            "/api/warehouse/pallets",
            json={
                "location_id": ids["locations"][2],
                "items": [
                    _matched_item(ids["tianhua"], ids["products"][0], "21301011")
                ],
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["pallet"]["needs_relocation"] is True
        cannot_clear = client.post(
            f"/api/warehouse/pallets/{created.json()['pallet']['id']}/relocation-flag",
            json={
                "expected_version": created.json()["pallet"]["version"],
                "needs_relocation": False,
                "placement_confirmed": True,
                "remarks": "尝试取消",
            },
        )
        assert cannot_clear.status_code == 409
        location = client.get(
            f"/api/warehouse/floor3/locations/{ids['locations'][2]}"
        ).json()
        assert location["is_temporary"] is True
        assert location["area_code"] == "F12"


def test_floor3_operator_can_mark_and_clear_manual_relocation_flag(floor3_app) -> None:
    app, ids, _factory = floor3_app
    with TestClient(app) as client:
        _login(client, "floor3-admin")
        created = client.post(
            "/api/warehouse/pallets",
            json={
                "location_id": ids["locations"][0],
                "items": [
                    _matched_item(ids["tianhua"], ids["products"][0], "21301011")
                ],
            },
        )
        pallet_id = created.json()["pallet"]["id"]
        marked = client.post(
            f"/api/warehouse/pallets/{pallet_id}/relocation-flag",
            json={
                "expected_version": created.json()["pallet"]["version"],
                "needs_relocation": True,
            },
        )
        assert marked.status_code == 200, marked.text
        assert marked.json()["pallet"]["needs_relocation"] is True
        cleared = client.post(
            f"/api/warehouse/pallets/{pallet_id}/relocation-flag",
            json={
                "expected_version": marked.json()["pallet"]["version"],
                "needs_relocation": False,
            },
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["pallet"]["needs_relocation"] is False


def test_floor3_fixed_cross_type_pallet_requires_explicit_placement_confirmation(
    floor3_app,
) -> None:
    app, ids, _factory = floor3_app
    semi_finished_item = _matched_item(
        ids["tianhua"], ids["products"][0], "SEMI-E1-001"
    )
    semi_finished_item.update(item_type="semi_finished", unit="sheets")

    with TestClient(app) as client:
        _login(client, "floor3-admin")
        created = client.post(
            "/api/warehouse/pallets",
            json={
                "location_id": ids["locations"][0],
                "items": [semi_finished_item],
            },
        )
        assert created.status_code == 201, created.text
        pallet = created.json()["pallet"]
        assert pallet["needs_relocation"] is True

        missing_confirmation = client.post(
            f"/api/warehouse/pallets/{pallet['id']}/relocation-flag",
            json={
                "expected_version": pallet["version"],
                "needs_relocation": False,
                "remarks": "未做现场确认",
            },
        )
        assert missing_confirmation.status_code == 409
        assert "现场核对" in missing_confirmation.text

        confirmed = client.post(
            f"/api/warehouse/pallets/{pallet['id']}/relocation-flag",
            json={
                "expected_version": pallet["version"],
                "needs_relocation": False,
                "placement_confirmed": True,
                "remarks": "现场确认已归位：A1-L01",
            },
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["message"] == "已确认当前固定货位归位"
        assert confirmed.json()["pallet"]["needs_relocation"] is False

        detail = client.get(
            f"/api/warehouse/floor3/locations/{ids['locations'][0]}"
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["current_pallet"]["needs_relocation"] is False


def test_floor3_move_requires_confirmation_is_idempotent_and_supports_f12_f34(
    floor3_app,
) -> None:
    from app.models.warehouse_inventory import (
        InventoryLocationMovement,
        InventoryPalletItem,
    )

    app, ids, factory = floor3_app
    with TestClient(app) as client:
        _login(client, "floor3-admin")
        created = client.post(
            "/api/warehouse/pallets",
            json={
                "location_id": ids["locations"][0],
                "items": [_matched_item(ids["tianhua"], ids["products"][0], "MAP-MOVE")],
            },
        )
        assert created.status_code == 201, created.text
        pallet = created.json()["pallet"]

        missing_confirmation = client.post(
            f"/api/warehouse/pallets/{pallet['id']}/move",
            json={
                "expected_version": pallet["version"],
                "to_location_id": ids["locations"][2],
                "idempotency_key": "confirm-required-001",
            },
        )
        assert missing_confirmation.status_code == 422

        move_to_f12_payload = {
            "expected_version": pallet["version"],
            "to_location_id": ids["locations"][2],
            "confirmed": True,
            "idempotency_key": "f12-move-001",
        }
        moved_to_f12 = client.post(
            f"/api/warehouse/pallets/{pallet['id']}/move", json=move_to_f12_payload
        )
        assert moved_to_f12.status_code == 200, moved_to_f12.text
        assert moved_to_f12.json()["pallet"]["location_id"] == ids["locations"][2]
        assert moved_to_f12.json()["pallet"]["total_quantity"] == 10.0

        replay = client.post(
            f"/api/warehouse/pallets/{pallet['id']}/move", json=move_to_f12_payload
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True

        reused_for_different_business = client.post(
            f"/api/warehouse/pallets/{pallet['id']}/move",
            json={
                **move_to_f12_payload,
                "to_location_id": ids["f34_location"],
            },
        )
        assert reused_for_different_business.status_code == 409

        moved_to_f34 = client.post(
            f"/api/warehouse/pallets/{pallet['id']}/move",
            json={
                "expected_version": moved_to_f12.json()["pallet"]["version"],
                "to_location_id": ids["f34_location"],
                "confirmed": True,
                "idempotency_key": "f34-move-001",
            },
        )
        assert moved_to_f34.status_code == 200, moved_to_f34.text
        assert moved_to_f34.json()["pallet"]["location_id"] == ids["f34_location"]

    with factory() as db:
        moves = db.scalars(
            select(InventoryLocationMovement)
            .where(InventoryLocationMovement.pallet_id == pallet["id"])
            .where(InventoryLocationMovement.movement_type == "move")
            .order_by(InventoryLocationMovement.id)
        ).all()
        assert [(row.from_location_id, row.to_location_id) for row in moves] == [
            (ids["locations"][0], ids["locations"][2]),
            (ids["locations"][2], ids["f34_location"]),
        ]
        assert all(row.confirmed_at is not None for row in moves)
        assert [(row.pallet_version_before, row.pallet_version_after) for row in moves] == [
            (1, 2),
            (2, 3),
        ]
        assert db.scalar(
            select(func.sum(InventoryPalletItem.quantity)).where(
                InventoryPalletItem.pallet_id == pallet["id"]
            )
        ) == Decimal("10.000")


def test_floor3_move_service_rejects_rack_source_and_target(floor3_app) -> None:
    from app.services.floor3_locations import (
        Floor3LocationError,
        create_pallet,
        move_pallet,
    )

    _app, ids, factory = floor3_app
    with factory() as db:
        pallet_from_rack = create_pallet(
            db,
            location_id=ids["rack_location"],
            pallet_code="PLT-RACK-SOURCE",
            items=[_matched_item(ids["tianhua"], ids["products"][0], "RACK-SOURCE")],
            remarks=None,
            operator_id=ids["admin"],
        )
        with pytest.raises(Floor3LocationError, match="不能从货架格移出"):
            move_pallet(
                db,
                pallet_id=pallet_from_rack.id,
                expected_version=pallet_from_rack.version,
                to_location_id=ids["locations"][0],
                remarks=None,
                operator_id=ids["admin"],
                idempotency_key="rack-source-service-001",
            )
        assert pallet_from_rack.location_id == ids["rack_location"]
        assert pallet_from_rack.version == 1

        pallet_from_ground = create_pallet(
            db,
            location_id=ids["locations"][0],
            pallet_code="PLT-RACK-TARGET",
            items=[_matched_item(ids["tianhua"], ids["products"][0], "RACK-TARGET")],
            remarks=None,
            operator_id=ids["admin"],
        )
        with pytest.raises(Floor3LocationError, match="不能移入货架格"):
            move_pallet(
                db,
                pallet_id=pallet_from_ground.id,
                expected_version=pallet_from_ground.version,
                to_location_id=ids["rack_location"],
                remarks=None,
                operator_id=ids["admin"],
                idempotency_key="rack-target-service-001",
            )
        assert pallet_from_ground.location_id == ids["locations"][0]
        assert pallet_from_ground.version == 1


def test_floor3_move_api_rejects_racks_and_allows_ground_and_temporary_aisle(
    floor3_app,
) -> None:
    app, ids, _factory = floor3_app
    with TestClient(app) as client:
        _login(client, "floor3-admin")

        rack_pallet = client.post(
            "/api/warehouse/pallets",
            json={
                "location_id": ids["rack_location"],
                "pallet_code": "PLT-RACK-SOURCE-API",
                "items": [_matched_item(ids["tianhua"], ids["products"][0], "RACK-API")],
            },
        )
        assert rack_pallet.status_code == 201, rack_pallet.text
        rack_pallet_body = rack_pallet.json()["pallet"]
        rack_source = client.post(
            f"/api/warehouse/pallets/{rack_pallet_body['id']}/move",
            json={
                "expected_version": rack_pallet_body["version"],
                "to_location_id": ids["locations"][0],
                "confirmed": True,
                "idempotency_key": "rack-source-api-001",
            },
        )
        assert rack_source.status_code == 409, rack_source.text
        assert "真实木栈板不能从货架格移出" in rack_source.json()["detail"]

        ground_pallet = client.post(
            "/api/warehouse/pallets",
            json={
                "location_id": ids["locations"][0],
                "pallet_code": "PLT-GROUND-API",
                "items": [_matched_item(ids["tianhua"], ids["products"][0], "GROUND-API")],
            },
        )
        assert ground_pallet.status_code == 201, ground_pallet.text
        ground_pallet_body = ground_pallet.json()["pallet"]
        rack_target = client.post(
            f"/api/warehouse/pallets/{ground_pallet_body['id']}/move",
            json={
                "expected_version": ground_pallet_body["version"],
                "to_location_id": ids["rack_location"],
                "confirmed": True,
                "idempotency_key": "rack-target-api-001",
            },
        )
        assert rack_target.status_code == 409, rack_target.text
        assert "真实木栈板不能移入货架格" in rack_target.json()["detail"]

        ground_to_ground = client.post(
            f"/api/warehouse/pallets/{ground_pallet_body['id']}/move",
            json={
                "expected_version": ground_pallet_body["version"],
                "to_location_id": ids["locations"][1],
                "confirmed": True,
                "idempotency_key": "ground-to-ground-api-001",
            },
        )
        assert ground_to_ground.status_code == 200, ground_to_ground.text
        moved_to_ground = ground_to_ground.json()["pallet"]
        assert moved_to_ground["location_id"] == ids["locations"][1]
        assert moved_to_ground["needs_relocation"] is False

        ground_to_temporary = client.post(
            f"/api/warehouse/pallets/{ground_pallet_body['id']}/move",
            json={
                "expected_version": moved_to_ground["version"],
                "to_location_id": ids["locations"][2],
                "confirmed": True,
                "idempotency_key": "ground-to-temporary-api-001",
            },
        )
        assert ground_to_temporary.status_code == 200, ground_to_temporary.text
        moved_to_temporary = ground_to_temporary.json()["pallet"]
        assert moved_to_temporary["location_id"] == ids["locations"][2]
        assert moved_to_temporary["needs_relocation"] is True


def test_floor3_layout_rejects_combined_overflow_and_requires_state_versions(
    floor3_app,
) -> None:
    from app.services.floor3_locations import Floor3LocationError, create_layout_slot

    app, ids, factory = floor3_app
    invalid_slot = {
        "location_code": "A1-OUTSIDE-RIGHT",
        "location_name": "outside right edge",
        "left_pct": 95,
        "top_pct": 10,
        "width_pct": 6,
        "height_pct": 5,
        "z_index": 1,
    }
    with TestClient(app) as client:
        _login(client, "floor3-admin")
        right_overflow = client.post(
            "/api/warehouse/floor3/layout/areas/A1/slots",
            json=invalid_slot,
        )
        assert right_overflow.status_code == 422, right_overflow.text
        assert "右边界" in right_overflow.text

        created = client.post(
            "/api/warehouse/floor3/layout/areas/A1/slots",
            json={
                **invalid_slot,
                "location_code": "A1-STATE-CAS",
                "location_name": "state cas slot",
                "left_pct": 10,
                "width_pct": 5,
            },
        )
        assert created.status_code == 201, created.text
        location_id = created.json()["location"]["id"]
        created_version = created.json()["layout"]["version"]
        assert created_version == 2

        bottom_overflow = client.patch(
            "/api/warehouse/floor3/layout/areas/A1",
            json={
                "slots": [
                    {
                        "location_id": location_id,
                        "expected_version": created_version,
                        "left_pct": 10,
                        "top_pct": 96,
                        "width_pct": 5,
                        "height_pct": 5,
                        "z_index": 1,
                    }
                ]
            },
        )
        assert bottom_overflow.status_code == 422, bottom_overflow.text
        assert "下边界" in bottom_overflow.text

        missing_disable_version = client.post(
            f"/api/warehouse/floor3/layout/slots/{location_id}/disable",
            json={},
        )
        assert missing_disable_version.status_code == 422
        disabled = client.post(
            f"/api/warehouse/floor3/layout/slots/{location_id}/disable",
            json={"expected_version": created_version},
        )
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["layout"]["version"] == created_version + 1

        missing_enable_version = client.post(
            f"/api/warehouse/floor3/layout/slots/{location_id}/enable",
            json={},
        )
        assert missing_enable_version.status_code == 422
        enabled = client.post(
            f"/api/warehouse/floor3/layout/slots/{location_id}/enable",
            json={"expected_version": created_version + 1},
        )
        assert enabled.status_code == 200, enabled.text
        assert enabled.json()["layout"]["version"] == created_version + 2

        stale_disable = client.post(
            f"/api/warehouse/floor3/layout/slots/{location_id}/disable",
            json={"expected_version": created_version + 1},
        )
        assert stale_disable.status_code == 409, stale_disable.text
        detail = client.get(f"/api/warehouse/floor3/locations/{location_id}")
        assert detail.json()["is_active"] is True
        assert detail.json()["layout"]["version"] == created_version + 2

    with factory() as db:
        with pytest.raises(Floor3LocationError, match="右边界") as captured:
            create_layout_slot(
                db,
                area_code="A1",
                location_code="A1-SERVICE-OVERFLOW",
                location_name="service overflow",
                left_pct=Decimal("99"),
                top_pct=Decimal("1"),
                width_pct=Decimal("2"),
                height_pct=Decimal("2"),
                z_index=0,
                operator_id=ids["admin"],
            )
        assert captured.value.status_code == 400


def test_floor3_disable_and_occupy_race_never_leaves_disabled_occupancy(
    floor3_app,
) -> None:
    from app.models.warehouse_inventory import (
        Floor3LocationLayout,
        InventoryPallet,
        WarehouseLocation,
    )
    from app.services.floor3_locations import (
        Floor3LocationError,
        create_layout_slot,
        create_pallet,
        set_layout_slot_active,
    )

    _app, ids, factory = floor3_app
    with factory() as db:
        location = create_layout_slot(
            db,
            area_code="A1",
            location_code="A1-RACE-STATE",
            location_name="disable occupy race",
            left_pct=Decimal("20"),
            top_pct=Decimal("20"),
            width_pct=Decimal("5"),
            height_pct=Decimal("5"),
            z_index=0,
            operator_id=ids["admin"],
        )
        db.commit()
        location_id = location.id
        created_version = location.floor3_layout.version

    start = Barrier(2)

    def disable_slot() -> str:
        with factory() as db:
            start.wait()
            try:
                set_layout_slot_active(
                    db,
                    location_id=location_id,
                    is_active=False,
                    expected_version=created_version,
                    operator_id=ids["admin"],
                )
                db.commit()
                return "disabled"
            except Floor3LocationError:
                db.rollback()
                return "disable_blocked"

    def occupy_slot() -> str:
        with factory() as db:
            start.wait()
            try:
                create_pallet(
                    db,
                    location_id=location_id,
                    pallet_code="PLT-DISABLE-OCCUPY-RACE",
                    items=[
                        _matched_item(
                            ids["tianhua"], ids["products"][0], "RACE-OCCUPY"
                        )
                    ],
                    remarks=None,
                    operator_id=ids["admin"],
                )
                db.commit()
                return "occupied"
            except Floor3LocationError:
                db.rollback()
                return "occupy_blocked"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(disable_slot), executor.submit(occupy_slot)]
        outcomes = {future.result() for future in futures}

    assert outcomes in (
        {"disabled", "occupy_blocked"},
        {"occupied", "disable_blocked"},
    )
    with factory() as db:
        location = db.get(WarehouseLocation, location_id)
        layout = db.scalar(
            select(Floor3LocationLayout).where(
                Floor3LocationLayout.location_id == location_id
            )
        )
        pallet = db.scalar(
            select(InventoryPallet).where(
                InventoryPallet.location_id == location_id,
                InventoryPallet.is_current.is_(True),
            )
        )
        assert location is not None and layout is not None
        assert not (location.is_active is False and pallet is not None)
        if location.is_active:
            assert pallet is not None
            assert layout.version == created_version
        else:
            assert pallet is None
            assert layout.version == created_version + 1


def test_floor3_concurrent_duplicate_move_is_a_single_idempotent_operation(
    floor3_app,
) -> None:
    from app.models.warehouse_inventory import InventoryLocationMovement, InventoryPallet
    from app.services.floor3_locations import (
        Floor3LocationError,
        create_pallet,
        move_pallet,
    )

    _app, ids, factory = floor3_app
    with factory() as db:
        pallet = create_pallet(
            db,
            location_id=ids["locations"][0],
            pallet_code="PLT-CONCURRENT-IDEMPOTENCY",
            items=[
                _matched_item(
                    ids["tianhua"], ids["products"][0], "IDEMPOTENT-RACE"
                )
            ],
            remarks=None,
            operator_id=ids["admin"],
        )
        db.commit()
        pallet_id = pallet.id

    start = Barrier(2)

    def move_once() -> tuple[bool, int | None, int]:
        with factory() as db:
            start.wait()
            result = move_pallet(
                db,
                pallet_id=pallet_id,
                expected_version=1,
                to_location_id=ids["locations"][1],
                remarks="same concurrent request",
                operator_id=ids["admin"],
                idempotency_key="move-concurrent-same-key",
            )
            outcome = (
                result.replayed,
                result.pallet.location_id,
                result.pallet.version,
            )
            db.commit()
            return outcome

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(move_once) for _ in range(2)]
        outcomes = [future.result() for future in futures]

    assert sorted(outcome[0] for outcome in outcomes) == [False, True]
    assert {
        (location_id, version) for _replayed, location_id, version in outcomes
    } == {(ids["locations"][1], 2)}
    with factory() as db:
        pallet = db.get(InventoryPallet, pallet_id)
        movements = db.scalars(
            select(InventoryLocationMovement).where(
                InventoryLocationMovement.idempotency_key
                == "move-concurrent-same-key"
            )
        ).all()
        assert pallet is not None
        assert pallet.location_id == ids["locations"][1]
        assert pallet.version == 2
        assert len(movements) == 1

        with pytest.raises(Floor3LocationError, match="不同的移位业务"):
            move_pallet(
                db,
                pallet_id=pallet_id,
                expected_version=2,
                to_location_id=ids["locations"][1],
                remarks="same concurrent request",
                operator_id=ids["admin"],
                idempotency_key="move-concurrent-same-key",
            )


def test_floor3_layout_admin_operations_do_not_change_inventory_and_non_admin_is_forbidden(
    floor3_app,
) -> None:
    from app.models.warehouse_inventory import (
        InventoryLocationMovement,
        InventoryLot,
        InventoryPallet,
        InventoryPalletItem,
    )

    app, ids, factory = floor3_app
    slot_payload = {
        "location_code": "A1-MAP-01",
        "location_name": "A1 map slot 01",
        "left_pct": 10,
        "top_pct": 20,
        "width_pct": 5,
        "height_pct": 6,
        "z_index": 3,
    }
    with TestClient(app) as client:
        _login(client, "floor3-admin")
        created = client.post("/api/warehouse/floor3/layout/areas/A1/slots", json=slot_payload)
        assert created.status_code == 201, created.text
        location_id = created.json()["location"]["id"]
        assert created.json()["location"]["warehouse_type"] == "finished"
        assert created.json()["layout"]["version"] == 2

        detail = client.get(f"/api/warehouse/floor3/locations/{location_id}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["current_pallet"] is None
        assert detail.json()["layout"]["left_pct"] == 10.0

        patched = client.patch(
            "/api/warehouse/floor3/layout/areas/A1",
            json={
                "slots": [
                    {
                        "location_id": location_id,
                        "expected_version": 2,
                        "left_pct": 11,
                        "top_pct": 21,
                        "width_pct": 5,
                        "height_pct": 6,
                        "z_index": 4,
                    }
                ]
            },
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["items"][0]["version"] == 3

        disabled = client.post(
            f"/api/warehouse/floor3/layout/slots/{location_id}/disable",
            json={"expected_version": 3},
        )
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["location"]["is_active"] is False
        enabled = client.post(
            f"/api/warehouse/floor3/layout/slots/{location_id}/enable",
            json={"expected_version": 4},
        )
        assert enabled.status_code == 200, enabled.text
        assert enabled.json()["location"]["is_active"] is True

    with factory() as db:
        assert db.scalar(select(func.count(InventoryLot.id))) == 0
        assert db.scalar(select(func.count(InventoryPallet.id))) == 0
        assert db.scalar(select(func.count(InventoryPalletItem.id))) == 0
        assert db.scalar(select(func.count(InventoryLocationMovement.id))) == 0

    with TestClient(app) as client:
        _login(client, "floor3-admin")
        pallet = client.post(
            "/api/warehouse/pallets",
            json={
                "location_id": location_id,
                "expected_layout_version": enabled.json()["layout"]["version"],
                "items": [_matched_item(ids["tianhua"], ids["products"][0], "LAYOUT-KEEP")],
            },
        )
        assert pallet.status_code == 201, pallet.text
        before_items = pallet.json()["pallet"]["items"]
        occupied_disable = client.post(
            f"/api/warehouse/floor3/layout/slots/{location_id}/disable",
            json={"expected_version": enabled.json()["layout"]["version"]},
        )
        assert occupied_disable.status_code == 409
        assert client.get(f"/api/warehouse/floor3/locations/{location_id}").json()[
            "current_pallet"
        ]["items"] == before_items

        client.post("/api/auth/logout")
        _login(client, "floor3-scoped")
        forbidden = client.post("/api/warehouse/floor3/layout/areas/A1/slots", json={
            **slot_payload,
            "location_code": "A1-MAP-02",
        })
        assert forbidden.status_code == 403


def test_floor3_area_target_count_auto_codes_pending_layout_and_only_admin(
    floor3_app,
) -> None:
    from app.models.warehouse_inventory import WarehouseLocation

    app, _ids, factory = floor3_app
    with TestClient(app) as client:
        _login(client, "floor3-scoped")
        forbidden = client.post(
            "/api/warehouse/floor3/layout/areas/A1/location-count",
            json={"target_count": 4, "confirmed": True},
        )
        assert forbidden.status_code == 403

        _login(client, "floor3-admin")
        missing_confirmation = client.post(
            "/api/warehouse/floor3/layout/areas/A1/location-count",
            json={"target_count": 4, "confirmed": False},
        )
        assert missing_confirmation.status_code == 422
        created = client.post(
            "/api/warehouse/floor3/layout/areas/A1/location-count",
            json={"target_count": 5, "confirmed": True},
        )
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["active_count"] == 5
        assert body["created_count"] == 2
        assert [item["location"]["location_code"] for item in body["items"]] == [
            "A1-L003",
            "A1-L004",
        ]
        assert all(item["location"]["placement_status"] == "unplaced" for item in body["items"])
        first = body["items"][0]
        saved = client.patch(
            "/api/warehouse/floor3/layout/areas/A1",
            json={
                "slots": [
                    {
                        "location_id": first["location"]["id"],
                        "expected_version": first["layout"]["version"],
                        "left_pct": first["layout"]["left_pct"],
                        "top_pct": first["layout"]["top_pct"],
                        "width_pct": first["layout"]["width_pct"],
                        "height_pct": first["layout"]["height_pct"],
                        "z_index": first["layout"]["z_index"],
                    }
                ]
            },
        )
        assert saved.status_code == 200, saved.text

        reduced = client.post(
            "/api/warehouse/floor3/layout/areas/A1/location-count",
            json={"target_count": 4, "confirmed": True},
        )
        assert reduced.status_code == 200, reduced.text
        assert reduced.json()["disabled_count"] == 1
        assert reduced.json()["active_count"] == 4

    with factory() as db:
        rows = db.scalars(
            select(WarehouseLocation)
            .where(
                WarehouseLocation.warehouse_floor == 3,
                WarehouseLocation.area_code == "A1",
                WarehouseLocation.source_version == "V11",
            )
            .order_by(WarehouseLocation.location_code)
        ).all()
        assert sum(1 for row in rows if row.is_active) == 4
        assert {row.location_code for row in rows} >= {"A1-L003", "A1-L004"}
        assert next(row for row in rows if row.location_code == "A1-L003").placement_status == "placed"


def test_twin_inventory_correction_is_admin_confirmed_versioned_and_audited(
    floor3_app,
) -> None:
    from app.models.warehouse_inventory import InventoryLot, InventoryMovement

    app, ids, factory = floor3_app
    with TestClient(app) as client:
        _login(client, "floor3-admin")
        slot = client.post(
            "/api/warehouse/floor3/layout/areas/A1/slots",
            json={
                "location_code": "A1-CORRECTION-01",
                "location_name": "A1 现场纠偏位",
                "left_pct": 70,
                "top_pct": 70,
                "width_pct": 8,
                "height_pct": 8,
                "z_index": 0,
            },
        )
        assert slot.status_code == 201, slot.text
        location_id = slot.json()["location"]["id"]
        layout_version = slot.json()["layout"]["version"]
        inbound = client.post(
            "/api/warehouse/twin-operations/finished-inbound",
            json={
                "location_id": location_id,
                "expected_layout_version": layout_version,
                "customer_id": ids["tianhua"],
                "product_id": ids["products"][0],
                "quantity": 100,
                "stock_date": "2026-08-06",
                "idempotency_key": "twin-correction-inbound-001",
                "confirmed": True,
                "remarks": "现场差异补录测试",
            },
        )
        assert inbound.status_code == 201, inbound.text
        incompatible_inbound = client.post(
            "/api/warehouse/twin-operations/finished-inbound",
            json={
                "location_id": location_id,
                "expected_layout_version": layout_version,
                "customer_id": ids["tianhua"],
                "product_id": ids["products"][1],
                "quantity": 30,
                "stock_date": "2026-08-06",
                "idempotency_key": "twin-correction-inbound-002",
                "confirmed": True,
                "remarks": "已有栈板现场差异补录",
            },
        )
        assert incompatible_inbound.status_code == 409
        assert "同客户同存货编码" in incompatible_inbound.json()["detail"]
        correction_inbound = client.post(
            "/api/warehouse/twin-operations/finished-inbound",
            json={
                "location_id": location_id,
                "expected_layout_version": layout_version,
                "customer_id": ids["tianhua"],
                "product_id": ids["products"][0],
                "quantity": 30,
                "stock_date": "2026-08-06",
                "idempotency_key": "twin-correction-inbound-003",
                "confirmed": True,
                "remarks": "同客户同产品已有栈板差异补录",
            },
        )
        assert correction_inbound.status_code == 201, correction_inbound.text
        assert len(correction_inbound.json()["pallet"]["items"]) == 2
        overview = client.get("/api/warehouse/twin-dashboard/overview?days=30")
        location = next(item for item in overview.json()["locations"] if item["location_id"] == location_id)
        item = next(item for item in location["pallet"]["items"] if item["inventory_code"] == "21301011")
        lot_id = item["lot_id"]
        assert item["version"] == 1

        _login(client, "floor3-scoped")
        forbidden = client.post(
            f"/api/warehouse/twin-operations/lots/{lot_id}/quantity-correction",
            json={
                "expected_version": 1,
                "action": "decrease",
                "quantity": 20,
                "reason": "现场盘点差异",
                "idempotency_key": "twin-correction-decrease-001",
                "confirmed": True,
            },
        )
        assert forbidden.status_code == 403

        _login(client, "floor3-admin")
        unconfirmed = client.post(
            f"/api/warehouse/twin-operations/lots/{lot_id}/quantity-correction",
            json={
                "expected_version": 1,
                "action": "decrease",
                "quantity": 20,
                "reason": "现场盘点差异",
                "idempotency_key": "twin-correction-unconfirmed",
                "confirmed": False,
            },
        )
        assert unconfirmed.status_code == 422
        decreased = client.post(
            f"/api/warehouse/twin-operations/lots/{lot_id}/quantity-correction",
            json={
                "expected_version": 1,
                "action": "decrease",
                "quantity": 20,
                "reason": "现场盘点少二十只",
                "idempotency_key": "twin-correction-decrease-001",
                "confirmed": True,
            },
        )
        assert decreased.status_code == 200, decreased.text
        assert decreased.json()["lot"]["quantity_available"] == 80
        assert decreased.json()["lot"]["version"] == 2
        replay = client.post(
            f"/api/warehouse/twin-operations/lots/{lot_id}/quantity-correction",
            json={
                "expected_version": 1,
                "action": "decrease",
                "quantity": 20,
                "reason": "现场盘点少二十只",
                "idempotency_key": "twin-correction-decrease-001",
                "confirmed": True,
            },
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True

        removed = client.post(
            f"/api/warehouse/twin-operations/lots/{lot_id}/quantity-correction",
            json={
                "expected_version": 2,
                "action": "remove",
                "reason": "确认现场已无该货物",
                "idempotency_key": "twin-correction-remove-001",
                "confirmed": True,
            },
        )
        assert removed.status_code == 200, removed.text
        assert removed.json()["lot"]["quantity_available"] == 0
        assert removed.json()["lot"]["status"] == "closed"
        overview_after = client.get("/api/warehouse/twin-dashboard/overview?days=30")
        location_after = next(item for item in overview_after.json()["locations"] if item["location_id"] == location_id)
        assert len(location_after["pallet"]["items"]) == 1
    assert location_after["pallet"]["items"][0]["inventory_code"] == "21301011"

    with factory() as db:
        lot = db.get(InventoryLot, lot_id)
        assert lot is not None
        assert lot.quantity_available == 0
        assert lot.status == "closed"
        movements = db.scalars(
            select(InventoryMovement).where(
                InventoryMovement.inventory_lot_id == lot_id,
                InventoryMovement.movement_type == "adjust",
            )
        ).all()
        assert len(movements) == 2

def test_floor3_explicit_finished_rows_create_lots_and_keep_snapshot_default(
    floor3_app,
) -> None:
    app, ids, factory = floor3_app
    with TestClient(app) as client:
        _login(client, "floor3-admin")
        created = client.post(
            "/api/warehouse/pallets",
            json={
                "location_id": ids["locations"][0],
                "pallet_code": "PLT-EXPLICIT-FINISHED",
                "items": [
                    {
                        **_matched_item(ids["tianhua"], ids["products"][0], "IGNORED"),
                        "quantity": 4,
                        "create_finished_inventory": True,
                        "stock_date": "2026-07-16",
                        "idempotency_key": "floor3-explicit-finished-1",
                    },
                    {
                        **_matched_item(ids["tianhua"], ids["products"][1], "FORMAL-2"),
                        "quantity": 2,
                        "create_finished_inventory": True,
                        "stock_date": "2026-07-16",
                        "idempotency_key": "floor3-explicit-finished-2",
                    },
                ],
            },
        )
        assert created.status_code == 201, created.text
        pallet = created.json()["pallet"]
        assert len(pallet["items"]) == 2
        assert sum(item["official_inventory"] for item in pallet["items"]) == 2
        assert {item["quantity"] for item in pallet["items"]} == {2, 4}
        assert all(item["inventory_lot_id"] is not None for item in pallet["items"])

        with factory() as db:
            from app.models.warehouse_inventory import InventoryLot, InventoryPalletItem

            assert db.scalar(select(func.count(InventoryLot.id))) == 2
            assert db.scalar(select(func.count(InventoryPalletItem.id))) == 2


def test_floor3_add_finished_inventory_is_idempotent_and_uses_expected_version(
    floor3_app,
) -> None:
    app, ids, _factory = floor3_app
    with TestClient(app) as client:
        _login(client, "floor3-admin")
        created = client.post(
            "/api/warehouse/pallets",
            json={
                "location_id": ids["locations"][0],
                "items": [_matched_item(ids["tianhua"], ids["products"][0], "SNAPSHOT")],
            },
        )
        assert created.status_code == 201, created.text
        pallet = created.json()["pallet"]
        payload = {
            "expected_version": pallet["version"],
            "item": {
                "customer_id": ids["tianhua"],
                "product_id": ids["products"][1],
                "quantity": 3,
                "item_type": "finished",
                "match_status": "matched",
                "create_finished_inventory": True,
                "stock_date": "2026-07-16",
                "idempotency_key": "floor3-add-finished-1",
            },
        }
        added = client.post(
            f"/api/warehouse/pallets/{pallet['id']}/items", json=payload
        )
        assert added.status_code == 200, added.text
        replay = client.post(
            f"/api/warehouse/pallets/{pallet['id']}/items", json=payload
        )
        assert replay.status_code == 200, replay.text
        assert len(replay.json()["pallet"]["items"]) == 2
        assert sum(item["official_inventory"] for item in replay.json()["pallet"]["items"]) == 1


def test_floor3_promote_snapshot_to_finished_deletes_snapshot_and_is_replayable(
    floor3_app,
) -> None:
    from app.models.order import Order, OrderItem

    app, ids, factory = floor3_app
    with factory() as db:
        other_order = Order(
            order_number="TM20260723001",
            customer_id=ids["other"],
            customer_po="PO-OTHER-CUSTOMER",
            order_date=date(2026, 7, 23),
            total_amount=Decimal("10"),
            created_by=ids["admin"],
        )
        db.add(other_order)
        db.flush()
        other_item = OrderItem(
            order_id=other_order.id,
            product_id=ids["products"][0],
            item_order_number="TM20260723001-001",
            quantity=10,
            unit_price=Decimal("1"),
            subtotal=Decimal("10"),
            snapshot_product_name="跨客户错误尝试",
            snapshot_product_code="PROMOTE-ME",
        )
        db.add(other_item)
        db.commit()
        other_item_id = other_item.id

    with TestClient(app) as client:
        _login(client, "floor3-admin")
        source_item = {
            **_matched_item(ids["tianhua"], ids["products"][0], "PROMOTE-ME"),
            "item_type": "semi_finished",
            "unit": "sheets",
            "quantity": 300,
        }
        created = client.post(
            "/api/warehouse/pallets",
            json={
                "location_id": ids["locations"][0],
                "items": [source_item],
            },
        )
        assert created.status_code == 201, created.text
        pallet = created.json()["pallet"]
        item_id = pallet["items"][0]["id"]

        before = client.get("/api/warehouse/finished/candidates", params={"order_item_id": 1})
        assert before.status_code == 200, before.text
        assert before.json()["items"] == []

        payload = {
            "expected_version": pallet["version"],
            "stock_date": "2026-07-16",
            "idempotency_key": "floor3-promote-1",
            "confirmed": True,
        }
        not_confirmed = client.post(
            f"/api/warehouse/pallets/{pallet['id']}/items/{item_id}/promote-finished",
            json={**payload, "confirmed": False},
        )
        assert not_confirmed.status_code == 422
        promoted = client.post(
            f"/api/warehouse/pallets/{pallet['id']}/items/{item_id}/promote-finished",
            json=payload,
        )
        assert promoted.status_code == 200, promoted.text
        assert promoted.json()["lot"]["stock_date"] == "2026-07-16"
        assert len(promoted.json()["pallet"]["items"]) == 1
        assert promoted.json()["pallet"]["items"][0]["official_inventory"] is True
        lot = promoted.json()["lot"]

        marked_for_relocation = client.post(
            f"/api/warehouse/pallets/{pallet['id']}/relocation-flag",
            json={
                "expected_version": promoted.json()["pallet"]["version"],
                "needs_relocation": True,
                "remarks": "现场货位待复核，但正式库存仍可抵扣和出库",
            },
        )
        assert marked_for_relocation.status_code == 200, marked_for_relocation.text
        assert marked_for_relocation.json()["pallet"]["needs_relocation"] is True

        after = client.get("/api/warehouse/finished/candidates", params={"order_item_id": 1})
        assert after.status_code == 200, after.text
        assert [row["lot_id"] for row in after.json()["items"]] == [lot["id"]]
        assert after.json()["items"][0]["stock_date_accuracy"] == "exact"

        other_candidates = client.get(
            "/api/warehouse/finished/candidates",
            params={"order_item_id": other_item_id},
        )
        assert other_candidates.status_code == 200, other_candidates.text
        assert other_candidates.json()["items"] == []
        rejected_other = client.post(
            "/api/warehouse/finished/reservations",
            json={
                "order_item_id": other_item_id,
                "inventory_lot_id": lot["id"],
                "quantity": 1,
                "expected_version": lot["version"],
                "idempotency_key": "floor3-promote-other-customer",
                "warning_acknowledged_codes": [],
            },
        )
        assert rejected_other.status_code == 400
        assert "客户专用库存不能用于其他客户订单" in rejected_other.text

        reserved = client.post(
            "/api/warehouse/finished/reservations",
            json={
                "order_item_id": 1,
                "inventory_lot_id": lot["id"],
                "quantity": 10,
                "expected_version": lot["version"],
                "idempotency_key": "floor3-promote-reserve-own-customer",
                "warning_acknowledged_codes": [],
            },
        )
        assert reserved.status_code == 200, reserved.text
        replay = client.post(
            f"/api/warehouse/pallets/{pallet['id']}/items/{item_id}/promote-finished",
            json=payload,
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["replayed"] is True

        with factory() as db:
            from app.models.audit import OperationLog
            from app.models.warehouse_inventory import (
                InventoryLot,
                InventoryMovement,
                InventoryPalletItem,
                InventoryReservation,
            )

            assert db.scalar(select(func.count(InventoryLot.id))) == 1
            assert db.scalar(select(func.count(InventoryPalletItem.id))) == 1
            official_item = db.scalar(select(InventoryPalletItem))
            assert official_item is not None
            assert official_item.pallet_id == pallet["id"]
            assert db.scalar(select(func.count(InventoryMovement.id))) == 2
            assert db.scalar(select(func.count(InventoryReservation.id))) == 1
            assert db.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.description == "现场快照转正式成品库存"
                )
            ) == 1
def test_floor3_create_rejects_mixed_finished_and_snapshot_rows(floor3_app) -> None:
    app, ids, factory = floor3_app
    with TestClient(app) as client:
        _login(client, "floor3-admin")
        response = client.post(
            "/api/warehouse/pallets",
            json={
                "location_id": ids["locations"][0],
                "items": [
                    {
                        **_matched_item(ids["tianhua"], ids["products"][0], "FORMAL"),
                        "create_finished_inventory": True,
                        "stock_date": "2026-07-16",
                        "idempotency_key": "mixed-formal-1",
                    },
                    _matched_item(ids["tianhua"], ids["products"][1], "SNAPSHOT"),
                ],
            },
        )
        assert response.status_code == 422
        assert "分开保存" in response.json()["detail"][0]["msg"]
    with factory() as db:
        from app.models.warehouse_inventory import InventoryLot, InventoryPallet

        assert db.scalar(select(func.count(InventoryLot.id))) == 0
        assert db.scalar(select(func.count(InventoryPallet.id))) == 0


def test_floor3_add_finished_idempotency_key_cannot_cross_pallets(floor3_app) -> None:
    app, ids, _factory = floor3_app
    with TestClient(app) as client:
        _login(client, "floor3-admin")
        first = client.post(
            "/api/warehouse/pallets",
            json={
                "location_id": ids["locations"][0],
                "items": [
                    {
                        **_matched_item(ids["tianhua"], ids["products"][0], "FORMAL-1"),
                        "create_finished_inventory": True,
                        "stock_date": "2026-07-16",
                        "idempotency_key": "cross-pallet-key-1",
                    }
                ],
            },
        )
        assert first.status_code == 201, first.text
        other = client.post(
            "/api/warehouse/pallets",
            json={
                "location_id": ids["locations"][1],
                "items": [_matched_item(ids["tianhua"], ids["products"][1], "SNAPSHOT-2")],
            },
        )
        assert other.status_code == 201, other.text
        pallet = other.json()["pallet"]
        response = client.post(
            f"/api/warehouse/pallets/{pallet['id']}/items",
            json={
                "expected_version": pallet["version"],
                "item": {
                    **_matched_item(ids["tianhua"], ids["products"][1], "FORMAL-2"),
                    "create_finished_inventory": True,
                    "stock_date": "2026-07-16",
                    "idempotency_key": "cross-pallet-key-1",
                },
            },
        )
        assert response.status_code == 409
        assert "其它物理栈板" in response.json()["detail"]


def test_location_ledger_create_stays_pending_until_real_layout_is_recorded(
    floor3_app,
) -> None:
    app, _ids, _factory = floor3_app
    with TestClient(app) as client:
        _login(client, "floor3-admin")
        floor3_master = client.post(
            "/api/warehouse/space/floors",
            json={
                "floor_code": "3F",
                "floor_name": "三楼",
                "floor_number": 3,
                "construction_status": "ledger_building",
            },
        )
        assert floor3_master.status_code == 201, floor3_master.text
        floor1_master = client.post(
            "/api/warehouse/space/floors",
            json={
                "floor_code": "1F",
                "floor_name": "一楼",
                "floor_number": 1,
                "construction_status": "ledger_building",
            },
        )
        assert floor1_master.status_code == 201, floor1_master.text
        for floor_id, code, name in (
            (floor3_master.json()["id"], "A1", "三楼 A1 区"),
            (floor1_master.json()["id"], "C1", "一楼 C1 区"),
        ):
            area = client.post(
                "/api/warehouse/space/areas",
                json={
                    "floor_id": floor_id,
                    "area_code": code,
                    "area_name": name,
                    "planned_location_count": 10,
                    "planned_pallet_capacity": 10,
                    "construction_status": "ledger_building",
                },
            )
            assert area.status_code == 201, area.text

        floor3 = client.post(
            "/api/warehouse/locations",
            json={
                "location_code": "A1-L99",
                "location_name": "A1 新增联动位",
                "warehouse_type": "finished",
                "warehouse_floor": 3,
                "area_code": "a1",
                "storage_type": "ground",
                "remarks": "从全部库位台账新增",
            },
        )
        assert floor3.status_code == 200, floor3.text
        floor3_row = floor3.json()
        assert floor3_row["source_version"] is None
        assert floor3_row["warehouse_floor"] == 3
        assert floor3_row["area_code"] == "A1"
        assert floor3_row["placement_status"] == "unplaced"

        mapped = client.get(
            "/api/warehouse/floor3/locations",
            params={"area_code": "A1", "include_inactive": True},
        )
        assert mapped.status_code == 200, mapped.text
        assert floor3_row["id"] not in {
            row["id"] for row in mapped.json()["items"]
        }

        invalid_floor3_semi = client.post(
            "/api/warehouse/locations",
            json={
                "location_code": "SF-TEMP-01",
                "location_name": "编码不属于 A1 的半成品临时位",
                "warehouse_type": "semi_finished",
                "warehouse_floor": 3,
                "area_code": "A1",
                "storage_type": "temporary_aisle",
            },
        )
        assert invalid_floor3_semi.status_code == 422
        assert "三楼货位编码必须以区域编码加连字符开头" in str(
            invalid_floor3_semi.json()
        )

        floor3_semi = client.post(
            "/api/warehouse/locations",
            json={
                "location_code": "A1-SF-TEMP-01",
                "location_name": "A1 半成品临时位",
                "warehouse_type": "semi_finished",
                "warehouse_floor": 3,
                "area_code": "A1",
                "storage_type": "temporary_aisle",
                "remarks": "半成品临时存放台账",
            },
        )
        assert floor3_semi.status_code == 200, floor3_semi.text
        floor3_semi_row = floor3_semi.json()
        assert floor3_semi_row["warehouse_type"] == "semi_finished"
        assert floor3_semi_row["warehouse_floor"] == 3
        assert floor3_semi_row["area_code"] == "A1"
        assert floor3_semi_row["storage_type"] == "temporary_aisle"
        assert floor3_semi_row["is_temporary"] is True
        assert floor3_semi_row["source_version"] is None
        assert floor3_semi_row["placement_status"] == "unplaced"
        mapped_after_semi = client.get(
            "/api/warehouse/floor3/locations",
            params={"area_code": "A1", "include_inactive": True},
        )
        assert mapped_after_semi.status_code == 200, mapped_after_semi.text
        assert floor3_semi_row["id"] not in {
            row["id"] for row in mapped_after_semi.json()["items"]
        }
        semi_candidates = client.get(
            "/api/warehouse/location-candidates",
            params={"inventory_type": "semi_finished"},
        )
        assert semi_candidates.status_code == 200, semi_candidates.text
        assert floor3_semi_row["id"] not in {
            row["id"] for row in semi_candidates.json()["items"]
        }

        floor1 = client.post(
            "/api/warehouse/locations",
            json={
                "location_code": "1F-C1-L01",
                "location_name": "一楼 C1 预留位",
                "warehouse_type": "finished",
                "warehouse_floor": 1,
                "area_code": "C1",
                "storage_type": "ground",
                "remarks": "未来一楼区域",
            },
        )
        assert floor1.status_code == 200, floor1.text
        assert floor1.json()["warehouse_floor"] == 1
        assert floor1.json()["source_version"] is None
        assert floor1.json()["placement_status"] == "unplaced"
        assert floor1.json()["id"] not in {
            row["id"] for row in mapped.json()["items"]
        }

        progress = client.get("/api/warehouse/space/floors")
        assert progress.status_code == 200, progress.text
        floors = {row["floor_code"]: row for row in progress.json()["items"]}
        assert floors["3F"]["recorded_location_count"] == 5
        a1 = next(row for row in floors["3F"]["areas"] if row["area_code"] == "A1")
        assert a1["recorded_location_count"] == 5
        assert a1["laid_out_location_count"] == 3
        assert a1["pending_layout_count"] == 2


def test_location_ledger_rejects_unregistered_floor_area(floor3_app) -> None:
    app, _ids, _factory = floor3_app
    with TestClient(app) as client:
        _login(client, "floor3-admin")
        response = client.post(
            "/api/warehouse/locations",
            json={
                "location_code": "1F-UNKNOWN-L01",
                "location_name": "未登记区域库位",
                "warehouse_type": "finished",
                "warehouse_floor": 1,
                "area_code": "UNKNOWN",
                "storage_type": "ground",
            },
        )
        assert response.status_code == 409
        assert "先新增楼层和区域" in response.json()["detail"]

        floor3_semi = client.post(
            "/api/warehouse/locations",
            json={
                "location_code": "UNKNOWN-SF-TEMP-01",
                "location_name": "三楼未登记区域半成品临时库位",
                "warehouse_type": "semi_finished",
                "warehouse_floor": 3,
                "area_code": "UNKNOWN",
                "storage_type": "temporary_aisle",
            },
        )
        assert floor3_semi.status_code == 409
        assert "先新增楼层和区域" in floor3_semi.json()["detail"]

        missing_area = client.post(
            "/api/warehouse/locations",
            json={
                "location_code": "A1-SF-TEMP-02",
                "location_name": "三楼缺少区域半成品临时库位",
                "warehouse_type": "semi_finished",
                "warehouse_floor": 3,
                "storage_type": "temporary_aisle",
            },
        )
        assert missing_area.status_code == 422
        assert "三楼库位必须填写所属区域" in str(missing_area.json())


def test_floor3_official_create_requires_target_location_to_be_empty(floor3_app) -> None:
    app, ids, factory = floor3_app
    with TestClient(app) as client:
        _login(client, "floor3-admin")
        occupied = client.post(
            "/api/warehouse/pallets",
            json={
                "location_id": ids["locations"][0],
                "items": [_matched_item(ids["tianhua"], ids["products"][0], "OCCUPIED")],
            },
        )
        assert occupied.status_code == 201, occupied.text
        response = client.post(
            "/api/warehouse/pallets",
            json={
                "location_id": ids["locations"][0],
                "items": [
                    {
                        **_matched_item(ids["tianhua"], ids["products"][1], "FORMAL"),
                        "create_finished_inventory": True,
                        "stock_date": "2026-07-16",
                        "idempotency_key": "occupied-formal-1",
                    }
                ],
            },
        )
        assert response.status_code == 409
        assert "目标货位已有当前栈板" in response.json()["detail"]
    with factory() as db:
        from app.models.warehouse_inventory import InventoryLot

        assert db.scalar(select(func.count(InventoryLot.id))) == 0
def test_floor3_create_finished_idempotency_key_cannot_reuse_other_pallet(
    floor3_app,
) -> None:
    app, ids, _factory = floor3_app
    with TestClient(app) as client:
        _login(client, "floor3-admin")
        first = client.post(
            "/api/warehouse/pallets",
            json={
                "location_id": ids["locations"][0],
                "items": [
                    {
                        **_matched_item(ids["tianhua"], ids["products"][0], "FORMAL-1"),
                        "create_finished_inventory": True,
                        "stock_date": "2026-07-16",
                        "idempotency_key": "create-cross-pallet-key-1",
                    }
                ],
            },
        )
        assert first.status_code == 201, first.text
        response = client.post(
            "/api/warehouse/pallets",
            json={
                "location_id": ids["locations"][1],
                "items": [
                    {
                        **_matched_item(ids["tianhua"], ids["products"][1], "FORMAL-2"),
                        "create_finished_inventory": True,
                        "stock_date": "2026-07-16",
                        "idempotency_key": "create-cross-pallet-key-1",
                    }
                ],
            },
        )
        assert response.status_code == 409
        assert "其它物理栈板" in response.json()["detail"]


def test_phase2c9_admin_map_inbound_and_formal_pallet_move_are_idempotent(
    floor3_app,
) -> None:
    app, ids, factory = floor3_app
    source_layout_version = _ensure_location_layout_version(
        factory, ids["locations"][0], left_pct=10
    )
    target_layout_version = _ensure_location_layout_version(
        factory, ids["locations"][1], left_pct=30
    )
    inbound_payload = {
        "location_id": ids["locations"][0],
        "expected_layout_version": source_layout_version,
        "pallet_code": "MAP-PALLET-001",
        "customer_id": ids["tianhua"],
        "product_id": ids["products"][0],
        "quantity": 120,
        "stock_date": "2026-08-06",
        "idempotency_key": "phase2c9-map-inbound-001",
        "confirmed": True,
        "remarks": "地图人工确认入成品仓",
    }
    with TestClient(app) as client:
        _login(client, "floor3-admin")
        unconfirmed = client.post(
            "/api/warehouse/twin-operations/finished-inbound",
            json={**inbound_payload, "confirmed": False},
        )
        assert unconfirmed.status_code == 422
        created = client.post(
            "/api/warehouse/twin-operations/finished-inbound",
            json=inbound_payload,
        )
        assert created.status_code == 201, created.text
        pallet = created.json()["pallet"]
        assert created.json()["idempotent_replay"] is False
        assert pallet["pallet_code"] == "MAP-PALLET-001"
        assert pallet["location_id"] == ids["locations"][0]
        assert pallet["items"][0]["official_inventory"] is True

        replay = client.post(
            "/api/warehouse/twin-operations/finished-inbound",
            json=inbound_payload,
        )
        assert replay.status_code == 201, replay.text
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["pallet"]["id"] == pallet["id"]

        move_payload = {
            "expected_version": pallet["version"],
            "to_location_id": ids["locations"][1],
            "expected_target_layout_version": target_layout_version,
            "idempotency_key": "phase2c9-map-move-001",
            "confirmed": True,
            "remarks": "地图人工确认移位",
        }
        moved = client.post(
            f"/api/warehouse/twin-operations/pallets/{pallet['id']}/move",
            json=move_payload,
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["idempotent_replay"] is False
        assert moved.json()["pallet"]["location_id"] == ids["locations"][1]

        move_replay = client.post(
            f"/api/warehouse/twin-operations/pallets/{pallet['id']}/move",
            json=move_payload,
        )
        assert move_replay.status_code == 200, move_replay.text
        assert move_replay.json()["idempotent_replay"] is True

    with factory() as db:
        from app.models.warehouse_inventory import (
            InventoryLocationMovement,
            InventoryLot,
            InventoryMovement,
        )

        assert db.scalar(select(func.count(InventoryLot.id))) == 1
        assert db.scalar(
            select(func.count(InventoryMovement.id)).where(
                InventoryMovement.idempotency_key == "phase2c9-map-inbound-001"
            )
        ) == 1
        assert db.scalar(
            select(func.count(InventoryLocationMovement.id)).where(
                InventoryLocationMovement.idempotency_key == "phase2c9-map-move-001"
            )
        ) == 1


def test_phase2c13_layout_rack_writes_are_admin_only_and_audited(
    floor3_app,
    tmp_path,
    monkeypatch,
) -> None:
    import hashlib
    import json

    from app.models.audit import OperationLog
    from app.services import warehouse_twin_layout_editor as editor

    app, _ids, factory = floor3_app
    floor = {
        "layout_id": "layout-3f",
        "floor_code": "3F",
        "features": [
            {
                "id": "zone-f1",
                "feature_code": "ZONE-3F-ERP-F1",
                "name": "F1",
                "feature_kind": "zone",
                "subtype": "rack_storage",
                "points": [[0, 0], [10000, 0], [10000, 10000], [0, 10000]],
                "version": 1,
                "erp_area_code": "F1",
            }
        ],
        "racks": [],
        "pallets": [],
    }
    floor["revision"] = editor._floor_revision(floor)
    asset = tmp_path / "twin-layout.json"
    asset.write_text(
        json.dumps({"schema_version": 1, "generated_at": "old", "floors": {"3F": floor}}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(editor, "TWIN_LAYOUT_PATH", asset)
    draft = tmp_path / "twin-layout.draft.json"
    backups = tmp_path / "layout-backups"
    monkeypatch.setattr(editor, "TWIN_LAYOUT_DRAFT_PATH", draft)
    monkeypatch.setattr(editor, "TWIN_LAYOUT_BACKUP_DIR", backups)
    published_before = hashlib.sha256(asset.read_bytes()).hexdigest()
    body = {
        "expected_revision": floor["revision"],
        "operation_key": "api-create-rack-0001",
        "area_feature_id": "zone-f1",
        "name": "F1现场货架",
        "x_mm": 5000,
        "y_mm": 5000,
        "width_mm": 2800,
        "depth_mm": 1100,
        "height_mm": 2200,
        "levels": 3,
        "level_heights_mm": [700, 1450],
        "cargo_rows": 4,
        "level_cell_counts": [0, 2, 6],
        "bays": 1,
        "access_side": "south",
        "min_aisle_width_mm": 1500,
        "rotation_deg": 0,
        "color": "#38bdf8",
    }
    with TestClient(app) as employee:
        _login(employee, "floor3-scoped")
        denied = employee.post("/api/warehouse/twin-layout/floors/3F/racks", json=body)
        assert denied.status_code == 403
        denied_draft = employee.get("/api/warehouse/twin-layout/floors/3F/draft")
        assert denied_draft.status_code == 403
        denied_validate = employee.post(
            "/api/warehouse/twin-layout/floors/3F/draft/validate",
            json={"expected_revision": floor["revision"]},
        )
        assert denied_validate.status_code == 403
        denied_publish = employee.post(
            "/api/warehouse/twin-layout/floors/3F/draft/publish",
            json={
                "expected_published_revision": floor["revision"],
                "expected_draft_revision": floor["revision"],
                "operation_key": "employee-publish-denied",
            },
        )
        assert denied_publish.status_code == 403
        denied_discard = employee.post(
            "/api/warehouse/twin-layout/floors/3F/draft/discard",
            json={"expected_revision": floor["revision"]},
        )
        assert denied_discard.status_code == 403
    with TestClient(app) as admin:
        _login(admin, "floor3-admin")
        created = admin.post("/api/warehouse/twin-layout/floors/3F/racks", json=body)
        assert created.status_code == 201, created.text
        assert created.json()["item"]["area_code"] == "F1"
        assert created.json()["item"]["level_cell_counts"] == [0, 2, 6]
        assert created.json()["item"]["cell_plan_status"] == "configured"
        repeated = admin.post("/api/warehouse/twin-layout/floors/3F/racks", json=body)
        assert repeated.status_code == 201, repeated.text
        assert repeated.json()["applied"] is False
        assert hashlib.sha256(asset.read_bytes()).hexdigest() == published_before
        assert json.loads(asset.read_text(encoding="utf-8"))["floors"]["3F"]["racks"] == []
        draft_view = admin.get("/api/warehouse/twin-layout/floors/3F/draft")
        assert draft_view.status_code == 200, draft_view.text
        assert draft_view.json()["draft_control"]["status"] == "draft"
        draft_revision = created.json()["revision"]
        validated = admin.post(
            "/api/warehouse/twin-layout/floors/3F/draft/validate",
            json={"expected_revision": draft_revision},
        )
        assert validated.status_code == 200, validated.text
        assert validated.json()["status"] == "validated"
        published = admin.post(
            "/api/warehouse/twin-layout/floors/3F/draft/publish",
            json={
                "expected_published_revision": floor["revision"],
                "expected_draft_revision": draft_revision,
                "operation_key": "api-publish-layout-0001",
            },
        )
        assert published.status_code == 200, published.text
        assert published.json()["inventory_changed"] is False
        assert json.loads(asset.read_text(encoding="utf-8"))["floors"]["3F"]["racks"][0]["area_code"] == "F1"
        replayed_publish = admin.post(
            "/api/warehouse/twin-layout/floors/3F/draft/publish",
            json={
                "expected_published_revision": floor["revision"],
                "expected_draft_revision": draft_revision,
                "operation_key": "api-publish-layout-0001",
            },
        )
        assert replayed_publish.status_code == 200
        assert replayed_publish.json()["applied"] is False
        assert len(list(backups.glob("*.json"))) == 1
    with factory() as db:
        logs = db.scalars(
            select(OperationLog).where(OperationLog.action == "TWIN_RACK_CREATE")
        ).all()
        assert len(logs) == 1
        assert "inventory_changed" in logs[0].details
        publish_logs = db.scalars(
            select(OperationLog).where(OperationLog.action == "TWIN_LAYOUT_PUBLISH")
        ).all()
        assert len(publish_logs) == 1
        assert "backup_name" in publish_logs[0].details


def test_phase2c9_non_admin_cannot_call_map_write_endpoints(floor3_app) -> None:
    app, ids, factory = floor3_app
    with TestClient(app) as client:
        _login(client, "floor3-scoped")
        inbound = client.post(
            "/api/warehouse/twin-operations/finished-inbound",
            json={
                "location_id": ids["locations"][0],
                "expected_layout_version": 1,
                "customer_id": ids["tianhua"],
                "product_id": ids["products"][0],
                "quantity": 20,
                "stock_date": "2026-08-06",
                "idempotency_key": "phase2c9-forbidden-inbound",
                "confirmed": True,
            },
        )
        assert inbound.status_code == 403
        move = client.post(
            "/api/warehouse/twin-operations/pallets/999/move",
            json={
                "expected_version": 1,
                "to_location_id": ids["locations"][1],
                "expected_target_layout_version": 1,
                "idempotency_key": "phase2c9-forbidden-move",
                "confirmed": True,
            },
        )
        assert move.status_code == 403

    with factory() as db:
        from app.models.warehouse_inventory import InventoryLot

        assert db.scalar(select(func.count(InventoryLot.id))) == 0


def test_phase2c12_empty_location_selects_staging_product_without_adding_stock(
    floor3_app,
) -> None:
    from app.models.warehouse_inventory import InventoryLot, WarehouseLocation
    from app.services.warehouse_inventory import manual_finished_in

    app, ids, factory = floor3_app
    with factory() as db:
        staging = WarehouseLocation(
            location_code="F1-DISPATCH-01",
            location_name="一楼待送区",
            warehouse_type="finished",
            warehouse_floor=1,
            area_code="DISPATCH",
            storage_type="ground",
            placement_status="placed",
        )
        db.add(staging)
        db.flush()
        source = manual_finished_in(
            db,
            customer_id=ids["tianhua"],
            product_id=ids["products"][0],
            location_id=staging.id,
            quantity=80,
            stock_date=date(2026, 8, 6),
            source_type="production_completion",
            source_ref_type="production_completion",
            source_ref_id=88001,
            remarks="生产完工直接待送",
            operator_id=ids["admin"],
            idempotency_key="phase2c12-production-completion-001",
        )
        source_id = source.id
        db.commit()
    target_layout_version = _ensure_location_layout_version(
        factory, ids["rack_location"], left_pct=50
    )

    with TestClient(app) as client:
        _login(client, "floor3-admin")
        candidates = client.get(
            "/api/warehouse/twin-operations/location-product-candidates",
            params={"location_id": ids["rack_location"], "q": "21301011"},
        )
        assert candidates.status_code == 200, candidates.text
        assert candidates.json()["target_location"]["location_id"] == ids["rack_location"]
        assert candidates.json()["items"][0]["lot_id"] == source_id
        assert candidates.json()["items"][0]["total_quantity"] == 80

        unconfirmed = client.post(
            f"/api/warehouse/twin-operations/staging-lots/{source_id}/place",
            json={
                "location_id": ids["rack_location"],
                "expected_layout_version": target_layout_version,
                "expected_version": 1,
                "quantity": 30,
                "idempotency_key": "phase2c12-place-staging-001",
                "confirmed": False,
            },
        )
        assert unconfirmed.status_code == 422
        placed = client.post(
            f"/api/warehouse/twin-operations/staging-lots/{source_id}/place",
            json={
                "location_id": ids["rack_location"],
                "expected_layout_version": target_layout_version,
                "expected_version": 1,
                "quantity": 30,
                "idempotency_key": "phase2c12-place-staging-001",
                "confirmed": True,
            },
        )
        assert placed.status_code == 200, placed.text
        assert placed.json()["idempotent_replay"] is False
        assert placed.json()["pallet"]["location_id"] == ids["rack_location"]

        replay = client.post(
            f"/api/warehouse/twin-operations/staging-lots/{source_id}/place",
            json={
                "location_id": ids["rack_location"],
                "expected_layout_version": target_layout_version,
                "expected_version": 1,
                "quantity": 30,
                "idempotency_key": "phase2c12-place-staging-001",
                "confirmed": True,
            },
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True

    with factory() as db:
        lots = db.scalars(select(InventoryLot)).all()
        assert sum(
            int(row.quantity_available or 0) + int(row.quantity_reserved or 0)
            for row in lots
        ) == 80
        source = db.get(InventoryLot, source_id)
        assert source is not None
        assert source.quantity_available == 50
        target = next(row for row in lots if row.id != source_id)
        assert target.quantity_available == 30
        assert target.warehouse_location_id == ids["rack_location"]


def test_phase2c12_admin_can_atomically_create_temporary_product_and_stock(
    floor3_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.product import Product
    from app.models.warehouse_inventory import InventoryLot, InventoryMovement

    app, ids, factory = floor3_app
    target_layout_version = _ensure_location_layout_version(
        factory, ids["rack_location"], left_pct=50
    )
    payload = {
        "location_id": ids["rack_location"],
        "expected_layout_version": target_layout_version,
        "customer_id": ids["tianhua"],
        "inventory_code": "TEMP-WH-001",
        "product_name": "历史未建档五层纸箱",
        "quantity": 45,
        "stock_date": "2026-08-06",
        "reason": "首次盘点发现现场实物但ERP尚无产品档案",
        "idempotency_key": "phase2c12-temporary-inbound-001",
        "confirmed": True,
    }
    with TestClient(app) as client:
        _login(client, "floor3-scoped")
        forbidden = client.post(
            "/api/warehouse/twin-operations/temporary-finished-inbound",
            json=payload,
        )
        assert forbidden.status_code == 403

        _login(client, "floor3-admin")
        unconfirmed = client.post(
            "/api/warehouse/twin-operations/temporary-finished-inbound",
            json={**payload, "confirmed": False},
        )
        assert unconfirmed.status_code == 422
        created = client.post(
            "/api/warehouse/twin-operations/temporary-finished-inbound",
            json=payload,
        )
        assert created.status_code == 201, created.text
        assert created.json()["idempotent_replay"] is False
        product_id = created.json()["temporary_product_id"]
        assert created.json()["pallet"]["location_id"] == ids["rack_location"]

        replay = client.post(
            "/api/warehouse/twin-operations/temporary-finished-inbound",
            json=payload,
        )
        assert replay.status_code == 201, replay.text
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["temporary_product_id"] == product_id

    with factory() as db:
        product = db.get(Product, product_id)
        assert product is not None
        assert product.product_code == "TEMP-WH-001"
        assert product.customer_material_code == "TEMP-WH-001"
        assert product.manual_modified is True
        assert product.remark.startswith("[仓库临时建档]")
        lots = db.scalars(select(InventoryLot)).all()
        assert len(lots) == 1
        assert lots[0].quantity_available == 45
        assert lots[0].finished_detail.product_id == product_id
        assert db.scalar(
            select(func.count(InventoryMovement.id)).where(
                InventoryMovement.idempotency_key == payload["idempotency_key"]
            )
        ) == 1
        assert db.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.entity_type == "product",
                OperationLog.entity_id == product_id,
            )
        ) >= 1


def test_phase2c9_scoped_locator_finds_mold_plate_areas_without_cross_customer_leak(
    floor3_app,
) -> None:
    app, ids, factory = floor3_app
    with factory() as db:
        from app.models.mold_tool import MoldTool
        from app.models.printing_plate import PrintingPlate
        from app.models.product import Product

        visible_mold = MoldTool(
            mold_code="MOLD-TH-001",
            mold_name="天华开槽模具",
            rack_location="3F-M-R01-L1-D1-P1",
            is_active=True,
        )
        hidden_mold = MoldTool(
            mold_code="MOLD-OTHER-001",
            mold_name="其他客户模具",
            rack_location="3F-M-R02-L1-D1-P1",
            is_active=True,
        )
        db.add_all([visible_mold, hidden_mold])
        db.flush()
        visible_product = db.get(Product, ids["products"][0])
        hidden_product = db.get(Product, ids["other_product"])
        assert visible_product is not None and hidden_product is not None
        visible_product.mold_tool_id = visible_mold.id
        hidden_product.mold_tool_id = hidden_mold.id
        db.add_all(
            [
                PrintingPlate(
                    plate_code="PL000001",
                    customer_id=ids["tianhua"],
                    plate_name="天华红色挂板",
                    color_name="红色",
                    rack_location="1F-PL-R01-L1-P01",
                ),
                PrintingPlate(
                    plate_code="PL000002",
                    customer_id=ids["other"],
                    plate_name="其他客户蓝色挂板",
                    color_name="蓝色",
                    rack_location="1F-PL-R01-L1-P02",
                ),
            ]
        )
        db.commit()

    with TestClient(app) as client:
        _login(client, "floor3-scoped")
        mold = client.get(
            "/api/warehouse/twin-operations/locate",
            params={"keyword": "模具"},
        )
        assert mold.status_code == 200, mold.text
        mold_resources = mold.json()["resources"]
        assert any(row["kind"] == "mold_area" for row in mold_resources)
        assert any(row["primary_code"] == "MOLD-TH-001" for row in mold_resources)
        assert all(row.get("primary_code") != "MOLD-OTHER-001" for row in mold_resources)

        plate = client.get(
            "/api/warehouse/twin-operations/locate",
            params={"keyword": "PL000"},
        )
        assert plate.status_code == 200, plate.text
        plate_resources = [
            row for row in plate.json()["resources"]
            if row["kind"] == "printing_plate"
        ]
        assert len(plate_resources) == 1
        assert plate_resources[0]["primary_code"] == "PL000001"
        assert plate_resources[0]["feature_codes"] == ["ZONE-1F-PLATE-002"]


def test_phase2c14_typed_search_keeps_customer_scope_and_separates_resources(
    floor3_app,
) -> None:
    app, ids, factory = floor3_app
    target_layout_version = _ensure_location_layout_version(
        factory, ids["rack_location"], left_pct=50
    )
    with TestClient(app) as client:
        _login(client, "floor3-admin")
        inbound = client.post(
            "/api/warehouse/twin-operations/finished-inbound",
            json={
                "location_id": ids["rack_location"],
                "expected_layout_version": target_layout_version,
                "customer_id": ids["tianhua"],
                "product_id": ids["products"][0],
                "quantity": 18,
                "stock_date": "2026-08-06",
                "idempotency_key": "phase2c14-search-finished-001",
                "confirmed": True,
                "remarks": "分类型查找测试",
            },
        )
        assert inbound.status_code == 201, inbound.text

        _login(client, "floor3-scoped")
        finished = client.get(
            "/api/warehouse/twin-operations/locate",
            params={
                "search_type": "finished",
                "customer_id": ids["tianhua"],
                "keyword": "",
            },
        )
        assert finished.status_code == 200, finished.text
        assert finished.json()["search_type"] == "finished"
        assert finished.json()["resources"] == []
        assert {row["customer_id"] for row in finished.json()["items"]} == {
            ids["tianhua"]
        }
        assert {row["inventory_code"] for row in finished.json()["items"]} == {
            "21301011"
        }

        denied = client.get(
            "/api/warehouse/twin-operations/locate",
            params={
                "search_type": "finished",
                "customer_id": ids["other"],
                "keyword": "",
            },
        )
        assert denied.status_code == 403

        mold_only = client.get(
            "/api/warehouse/twin-operations/locate",
            params={"search_type": "mold", "keyword": "模具"},
        )
        assert mold_only.status_code == 200, mold_only.text
        assert mold_only.json()["items"] == []
        assert all("mold" in row["kind"] for row in mold_only.json()["resources"])


def test_phase2c14_customer_selected_product_candidates_can_list_common_boxes(
    floor3_app,
) -> None:
    app, ids, _factory = floor3_app
    with TestClient(app) as client:
        _login(client, "floor3-scoped")
        response = client.get(
            "/api/warehouse/floor3/product-candidates",
            params={"customer_id": ids["tianhua"], "q": "", "limit": 50},
        )
        assert response.status_code == 200, response.text
        assert {row["product_id"] for row in response.json()["items"]} == set(
            ids["products"]
        )
        assert response.json()["auto_bind_allowed"] is False


def test_phase2c14_map_semi_finished_inbound_is_admin_only_idempotent_and_compatible(
    floor3_app,
) -> None:
    from app.models.product import Product
    from app.models.warehouse_inventory import (
        InventoryLot,
        SemiFinishedLotAllowedProduct,
        WarehouseLocation,
    )

    app, ids, factory = floor3_app
    with factory() as db:
        location = WarehouseLocation(
            location_code="1F-SEMI-MAP-01",
            location_name="一楼半成品地图位",
            warehouse_type="semi_finished",
            warehouse_floor=1,
            area_code="SEMI",
            storage_type="ground",
            sort_order=100,
        )
        db.add(location)
        for product_id in ids["products"][:2]:
            product = db.get(Product, product_id)
            assert product is not None
            product.default_material_code = "K=A"
            product.layer_count = 3
            product.flute_type = "B"
            product.report_length_mm = 800
            product.report_width_mm = 600
            db.commit()
            location_id = location.id
    target_layout_version = _ensure_location_layout_version(
        factory, location_id, left_pct=20
    )

    payload = {
        "location_id": location_id,
        "expected_layout_version": target_layout_version,
        "customer_id": ids["tianhua"],
        "product_id": ids["products"][0],
        "quantity": 36,
        "stock_date": "2026-08-06",
        "idempotency_key": "phase2c14-semi-map-inbound-001",
        "confirmed": True,
        "remarks": "一楼地图半成品差异补录",
    }
    with TestClient(app) as client:
        _login(client, "floor3-scoped")
        forbidden = client.post(
            "/api/warehouse/twin-operations/semi-finished-inbound", json=payload
        )
        assert forbidden.status_code == 403

        _login(client, "floor3-admin")
        created = client.post(
            "/api/warehouse/twin-operations/semi-finished-inbound", json=payload
        )
        assert created.status_code == 201, created.text
        assert created.json()["idempotent_replay"] is False
        replay = client.post(
            "/api/warehouse/twin-operations/semi-finished-inbound", json=payload
        )
        assert replay.status_code == 201, replay.text
        assert replay.json()["idempotent_replay"] is True

        incompatible = client.post(
            "/api/warehouse/twin-operations/semi-finished-inbound",
            json={
                **payload,
                "product_id": ids["products"][1],
                "idempotency_key": "phase2c14-semi-map-inbound-002",
            },
        )
        assert incompatible.status_code == 409
        assert "不同半成品款号" in incompatible.json()["detail"]

    with factory() as db:
        lots = db.scalars(
            select(InventoryLot).where(
                InventoryLot.warehouse_location_id == location_id
            )
        ).all()
        assert len(lots) == 1
        assert lots[0].quantity_available == 36
        assert db.scalar(
            select(func.count(SemiFinishedLotAllowedProduct.inventory_lot_id)).where(
                SemiFinishedLotAllowedProduct.inventory_lot_id == lots[0].id,
                SemiFinishedLotAllowedProduct.product_id == ids["products"][0],
            )
        ) == 1


def test_p1_34b1_location_labels_are_mapped_read_only_and_fail_closed(
    floor3_app,
    monkeypatch,
) -> None:
    from app.api import warehouse as warehouse_api
    from app.models.warehouse_inventory import (
        Floor3LocationLayout,
        WarehouseArea,
        WarehouseFloor,
        WarehouseGroundLayoutPlan,
        WarehouseGroundLayoutSlot,
        WarehouseLocation,
    )
    from app.services import location_candidates

    app, ids, factory = floor3_app
    mapped_id = ids["locations"][0]
    temporary_id = ids["locations"][2]
    unmapped_id = ids["locations"][1]
    with factory() as db:
        floor = WarehouseFloor(
            floor_code="3F",
            floor_name="三楼成品仓",
            floor_number=3,
            construction_status="enabled",
        )
        db.add(floor)
        db.flush()
        area_a1 = WarehouseArea(
            floor_id=floor.id,
            area_code="A1",
            area_name="A1成品存放区",
            construction_status="enabled",
        )
        area_f12 = WarehouseArea(
            floor_id=floor.id,
            area_code="F12",
            area_name="F12过道临放区",
            construction_status="enabled",
        )
        db.add_all([area_a1, area_f12])
        db.flush()
        db.get(WarehouseLocation, mapped_id).placement_status = "placed"
        db.get(WarehouseLocation, temporary_id).placement_status = "placed"
        db.get(WarehouseLocation, unmapped_id).placement_status = "unplaced"
        db.add(
            Floor3LocationLayout(
                location_id=mapped_id,
                left_pct=Decimal("10"),
                top_pct=Decimal("20"),
                width_pct=Decimal("4"),
                height_pct=Decimal("5"),
                version=3,
                source_type="manual",
            )
        )
        ground_plan = WarehouseGroundLayoutPlan(
            area_id=area_a1.id,
            status="published",
            target_slot_count=1,
            numbering_origin="south",
            row_direction="from_aisle_inward",
            slot_direction="left_to_right",
            row_start_no=1,
            slot_start_no=1,
            draft_map_revision="p1-34b1-map-v1",
            published_map_revision="p1-34b1-map-v1",
            preview_fingerprint="a" * 64,
            version=1,
            publish_idempotency_key="p1-34b1-ground-publish",
            publish_request_hash="b" * 64,
            updated_by=ids["admin"],
            published_by=ids["admin"],
            published_at=datetime.now(),
        )
        db.add(ground_plan)
        db.flush()
        db.add(
            WarehouseGroundLayoutSlot(
                plan_id=ground_plan.id,
                location_id=mapped_id,
                route_sequence=1,
                row_no=1,
                slot_no=1,
                x_mm=Decimal("1000"),
                y_mm=Decimal("1000"),
                width_mm=1200,
                depth_mm=1000,
            )
        )
        db.commit()
    monkeypatch.setattr(
        location_candidates,
        "load_warehouse_twin_published_floor_identity",
        lambda _floor_number: {
            "revision": "p1-34b1-map-v1",
            "zones_by_id": {
                "zone-a1": "A1",
                "zone-f12": "F12",
            },
            "zone_ids_by_area": {
                "A1": ("zone-a1",),
                "F12": ("zone-f12",),
            },
        },
    )
    monkeypatch.setattr(warehouse_api, "_lan_ip", lambda: "192.168.3.80")

    with TestClient(app) as client:
        _login(client, "floor3-admin")
        single = client.get(f"/api/warehouse/locations/{mapped_id}/label")
        assert single.status_code == 200, single.text
        assert single.json()["location_code"] == "A1-L01"
        assert single.json()["display_path"] == "三楼 右区A1·成品存放区·A1-1"
        assert single.json()["area_text"] == "右区A1·成品存放区"
        assert single.json()["area_master_name"] == "A1成品存放区"
        assert single.json()["position_status"] == "mapped"
        assert single.json()["layout_version"] == 3
        assert single.json()["lookup_url"].endswith(
            f"/warehouse.html?tab=locations&location_id={mapped_id}"
        )
        assert single.json()["qr_data_url"].startswith("data:image/png;base64,")

        batch = client.get(
            "/api/warehouse/locations/labels",
            params={"location_ids": f"{mapped_id},{mapped_id}"},
        )
        assert batch.status_code == 200, batch.text
        assert batch.json()["count"] == 1
        assert [row["id"] for row in batch.json()["items"]] == [mapped_id]

        for blocked_id in (temporary_id, unmapped_id):
            blocked = client.get(
                f"/api/warehouse/locations/{blocked_id}/label"
            )
            assert blocked.status_code == 409, blocked.text
            assert "已发布到当前实测地图" in blocked.json()["detail"]

    with factory() as db:
        area = db.scalar(select(WarehouseArea).where(WarehouseArea.area_code == "A1"))
        assert area is not None
        area.area_name = "A1 区"
        db.commit()

    with TestClient(app) as client:
        _login(client, "floor3-admin")
        default_name = client.get(f"/api/warehouse/locations/{mapped_id}/label")
        assert default_name.status_code == 200, default_name.text
        assert default_name.json()["area_text"] == "右区A1"
        assert default_name.json()["area_master_name"] == "A1 区"
        assert default_name.json()["display_path"] == "三楼 右区A1·A1-1"

    with factory() as db:
        layout = db.scalar(
            select(Floor3LocationLayout).where(
                Floor3LocationLayout.location_id == mapped_id
            )
        )
        assert layout is not None
        assert layout.version == 3


def test_p1_34b1_location_label_frontend_is_batchable_and_read_only() -> None:
    root = Path(__file__).resolve().parents[1]
    page = (root / "static" / "location-label.html").read_text(encoding="utf-8")
    warehouse = (root / "static" / "warehouse.html").read_text(encoding="utf-8")

    assert "80 × 40 mm" in page
    assert "/api/warehouse/locations/labels?location_ids=" in page
    assert "/api/warehouse/locations/${id}/label" in page
    assert "扫码后仍须登录" in page
    assert "客户、库存数量或价格" in page
    assert "window.print()" in page
    assert 'id="locationLabelSelectAll"' in warehouse
    assert 'id="locationBatchPrint"' in warehouse
    assert "locationLabelEligible" in warehouse
    assert "/location-label.html?location_id=" in warehouse
    assert 'params.get("label_print")==="1"' in warehouse
