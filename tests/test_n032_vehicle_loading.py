from __future__ import annotations

import importlib.util
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def loading_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deliveries import order_actions_router, router as deliveries_router
    from app.api.deps import get_db
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "n032.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(username="admin", password_hash=hash_password("RolePass123!"), role="admin", real_name="admin", display_name="admin", must_change_password=False)
        sales = User(username="sales", password_hash=hash_password("RolePass123!"), role="sales", real_name="sales", display_name="sales", must_change_password=False, customer_access_mode="selected")
        finance = User(username="finance", password_hash=hash_password("RolePass123!"), role="finance", real_name="finance", display_name="finance", must_change_password=False)
        customer = Customer(customer_number=1, customer_code="C1", name="测试客户", payment_term_days=30, credit_limit=Decimal("1000"))
        other_customer = Customer(customer_number=2, customer_code="C2", name="Other customer", payment_term_days=30, credit_limit=Decimal("1000"))
        db.add_all([admin, sales, finance, customer, other_customer])
        db.flush()
        db.add(UserCustomerScope(user_id=sales.id, customer_id=customer.id))
        db.add_all(
            [
                UserPermissionOverride(
                    user_id=sales.id,
                    permission_code=permission_code,
                    is_allowed=True,
                    granted_by=admin.id,
                )
                for permission_code in ("deliveries.view", "deliveries.execute")
            ]
        )
        product = Product(customer_id=customer.id, product_code="BOX-1", customer_material_code="BOX-1", product_name="测试箱", length_mm=Decimal("1000"), width_mm=Decimal("1000"), height_mm=Decimal("1000"), box_category="normal")
        pending_product = Product(customer_id=customer.id, product_code="BOX-2", customer_material_code="BOX-2", product_name="缺数据箱", box_category="normal")
        other_product = Product(customer_id=other_customer.id, product_code="BOX-X", customer_material_code="BOX-X", product_name="Other carton", length_mm=Decimal("500"), width_mm=Decimal("500"), height_mm=Decimal("500"), box_category="normal")
        db.add_all([product, pending_product, other_product])
        db.flush()
        order = Order(order_number="N032-001", customer_id=customer.id, order_date=date(2026, 7, 17), delivery_date=date(2026, 7, 18), status="pending_delivery", payment_status="unpaid", total_amount=Decimal("10"))
        db.add(order)
        db.flush()
        other_order = Order(order_number="N032-002", customer_id=other_customer.id, order_date=date(2026, 7, 17), delivery_date=date(2026, 7, 18), status="pending_delivery", payment_status="unpaid", total_amount=Decimal("10"))
        db.add(other_order)
        db.flush()
        db.add_all([
            OrderItem(order_id=order.id, product_id=product.id, quantity=20, unit_price=Decimal("1"), subtotal=Decimal("20"), material_status="received", snapshot_product_name="测试箱", snapshot_spec="1000x1000x1000", snapshot_material="A"),
            OrderItem(order_id=order.id, product_id=pending_product.id, quantity=20, unit_price=Decimal("1"), subtotal=Decimal("20"), material_status="received", snapshot_product_name="缺数据箱", snapshot_spec="-", snapshot_material="A"),
        ])
        db.add(OrderItem(order_id=other_order.id, product_id=other_product.id, quantity=20, unit_price=Decimal("1"), subtotal=Decimal("20"), material_status="received", snapshot_product_name="Other carton", snapshot_spec="500x500x500", snapshot_material="A"))
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(deliveries_router, prefix="/api/deliveries")
    app.include_router(order_actions_router, prefix="/api/orders")

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    return app, factory


def login(client: TestClient, role: str) -> None:
    response = client.post("/api/auth/login", json={"username": role, "password": "RolePass123!"})
    assert response.status_code == 200, response.text


def vehicle_payload(**overrides) -> dict:
    payload = {
        "name": "一号车",
        "vehicle_type": "厢式",
        "plate_number": "苏E-N032",
        "cargo_length_mm": 1000,
        "cargo_width_mm": 1000,
        "cargo_height_mm": 1000,
        "safety_load_factor": 1,
        "yellow_threshold_pct": 80,
        "red_threshold_pct": 100,
        "is_active": True,
    }
    payload.update(overrides)
    return payload


def profile_payload(**overrides) -> dict:
    payload = {"mode": "manual_unit", "manual_unit_m3": 0.5, "source_note": "称量折算", "confirmed": True}
    payload.update(overrides)
    return payload


def create_vehicle(client: TestClient, **overrides) -> int:
    response = client.post("/api/deliveries/vehicles", json=vehicle_payload(**overrides))
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_loading_modes_and_threshold_boundaries(loading_app) -> None:
    from app.models.delivery_loading import DeliveryVehicle, ProductLoadingProfile
    from app.services.delivery_loading import calculate_loading

    _app, factory = loading_app
    with factory() as db:
        vehicle = DeliveryVehicle(**vehicle_payload())
        product_id = 1
        db.add(vehicle)
        profile = ProductLoadingProfile(product_id=product_id, mode="manual_unit", manual_unit_m3=Decimal("0.7999"))
        db.add(profile)
        db.commit()
        for mode in ("manual_unit", "package", "theoretical_box"):
            profile.mode = mode
            profile.manual_unit_m3 = Decimal("0.7999") if mode == "manual_unit" else None
            profile.package_piece_count = 10 if mode == "package" else None
            profile.package_length_mm = Decimal("1000") if mode == "package" else None
            profile.package_width_mm = Decimal("1000") if mode == "package" else None
            profile.package_height_mm = Decimal("1000") if mode == "package" else None
            db.commit()
            unconfirmed = calculate_loading(db, vehicle=vehicle, lines=[(1, 1)])
            assert unconfirmed["status"] == "data_pending"
            assert unconfirmed["items"][0]["confirmed"] is False
            assert unconfirmed["items"][0]["confirmed_at"] is None
            assert "pending_confirmation" in unconfirmed["items"][0]["source"]
        profile.mode = "manual_unit"
        profile.manual_unit_m3 = Decimal("0.7999")
        profile.confirmed_by = 1
        profile.confirmed_at = datetime(2026, 7, 17, 8, 0, 0)
        db.commit()
        assert calculate_loading(db, vehicle=vehicle, lines=[(1, 1)])["status"] == "normal"
        profile.manual_unit_m3 = Decimal("0.8")
        db.commit()
        assert calculate_loading(db, vehicle=vehicle, lines=[(1, 1)])["status"] == "warning"
        profile.manual_unit_m3 = Decimal("1")
        db.commit()
        assert calculate_loading(db, vehicle=vehicle, lines=[(1, 1)])["status"] == "warning"
        profile.manual_unit_m3 = Decimal("1.0001")
        db.commit()
        assert calculate_loading(db, vehicle=vehicle, lines=[(1, 1)])["status"] == "over_capacity"
        profile.mode = "package"
        profile.manual_unit_m3 = None
        profile.package_piece_count = 10
        profile.package_length_mm = Decimal("1000")
        profile.package_width_mm = Decimal("1000")
        profile.package_height_mm = Decimal("1000")
        db.commit()
        package = calculate_loading(db, vehicle=vehicle, lines=[(1, 1)])
        assert package["items"][0]["unit_volume_m3"] == Decimal("0.10000000")
        profile.mode = "theoretical_box"
        profile.package_piece_count = None
        profile.package_length_mm = profile.package_width_mm = profile.package_height_mm = None
        db.commit()
        assert calculate_loading(db, vehicle=vehicle, lines=[(1, 1)])["items"][0]["unit_volume_m3"] == Decimal("1.00000000")


def test_data_pending_and_snapshot_no_drift(loading_app) -> None:
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.delivery_loading import DeliveryVehicle, ProductLoadingProfile
    from app.services.delivery_loading import calculate_loading, recalculate_delivery_loading, stored_delivery_loading

    _app, factory = loading_app
    with factory() as db:
        vehicle = DeliveryVehicle(**vehicle_payload())
        db.add(vehicle)
        db.flush()
        theoretical_pending = calculate_loading(db, vehicle=vehicle, lines=[(1, 1)])
        assert theoretical_pending["status"] == "data_pending"
        assert theoretical_pending["items"][0]["unit_volume_m3"] == Decimal("1.00000000")
        assert theoretical_pending["items"][0]["confirmed"] is False
        dimensions_pending = calculate_loading(db, vehicle=vehicle, lines=[(2, 1)])
        assert dimensions_pending["status"] == "data_pending"
        assert dimensions_pending["items"][0]["unit_volume_m3"] is None
        profile = ProductLoadingProfile(
            product_id=1,
            mode="manual_unit",
            manual_unit_m3=Decimal("0.25"),
            source_note="initial measurement",
            confirmed_by=1,
            confirmed_at=datetime(2026, 7, 17, 8, 0, 0),
        )
        db.add(profile)
        db.flush()
        delivery = Delivery(delivery_number="N032-SNAPSHOT", customer_id=1, delivery_date=date(2026, 7, 17), vehicle_id=vehicle.id, vehicle_number=vehicle.plate_number, status="pending", total_quantity=1)
        db.add(delivery)
        db.flush()
        line = DeliveryItem(delivery_id=delivery.id, order_item_id=1, delivered_quantity=1)
        db.add(line)
        db.flush()
        original = recalculate_delivery_loading(db, delivery, vehicle)
        db.commit()
        assert original["status"] == "normal"
        saved = stored_delivery_loading(delivery)
        saved_vehicle_snapshot = delivery.vehicle_capacity_snapshot_json
        saved_item_volume = line.estimated_volume_m3
        saved_item_snapshot = line.loading_snapshot_json
        profile.manual_unit_m3 = Decimal("0.75")
        profile.source_note = "later measurement"
        vehicle.cargo_length_mm = Decimal("2000")
        db.commit()
        db.expire_all()
        persisted_delivery = db.get(Delivery, delivery.id)
        persisted_line = db.get(DeliveryItem, line.id)
        assert stored_delivery_loading(persisted_delivery) == saved
        assert persisted_delivery.vehicle_capacity_snapshot_json == saved_vehicle_snapshot
        assert persisted_line.estimated_volume_m3 == saved_item_volume
        assert persisted_line.loading_snapshot_json == saved_item_snapshot


def test_profile_search_scope_confirmation_and_source_gate(loading_app) -> None:
    from app.models.customer import Customer
    from app.models.product import Product

    app, factory = loading_app
    with factory() as db:
        customer_ids = {
            row.customer_code: row.id
            for row in db.scalars(select(Customer)).all()
        }
        product_ids = {
            row.product_code: row.id
            for row in db.scalars(select(Product)).all()
        }
    outer_keys = {
        "product_id",
        "customer_id",
        "customer_name",
        "product_code",
        "product_name",
        "profile",
    }
    with TestClient(app) as client:
        login(client, "admin")
        no_source = client.put(
            f"/api/deliveries/loading-profiles/{product_ids['BOX-1']}",
            json={"mode": "manual_unit", "manual_unit_m3": 0.5, "confirmed": True},
        )
        assert no_source.status_code == 422
        assert "来源说明" in no_source.json()["detail"]

        draft = client.put(
            f"/api/deliveries/loading-profiles/{product_ids['BOX-1']}",
            json={"mode": "manual_unit", "manual_unit_m3": 0.5},
        )
        assert draft.status_code == 200, draft.text
        assert set(draft.json()) == outer_keys
        assert draft.json()["profile"]["confirmed"] is False
        assert draft.json()["profile"]["source_note"] is None
        vehicle_id = create_vehicle(client)
        pending_preview = client.post(
            "/api/deliveries/loading-preview",
            json={
                "customer_id": customer_ids["C1"],
                "vehicle_id": vehicle_id,
                "items": [{"order_item_id": 1, "delivered_quantity": 1}],
            },
        )
        assert pending_preview.status_code == 200, pending_preview.text
        assert pending_preview.json()["status"] == "data_pending"
        assert pending_preview.json()["items"][0]["confirmed"] is False

        confirmed = client.put(
            f"/api/deliveries/loading-profiles/{product_ids['BOX-1']}",
            json=profile_payload(manual_unit_m3=0.5),
        )
        assert confirmed.status_code == 200, confirmed.text
        assert set(confirmed.json()) == outer_keys
        confirmed_by = confirmed.json()["profile"]["confirmed_by"]
        confirmed_at = confirmed.json()["profile"]["confirmed_at"]
        preserved = client.put(
            f"/api/deliveries/loading-profiles/{product_ids['BOX-1']}",
            json={"mode": "manual_unit", "manual_unit_m3": 0.5},
        )
        assert preserved.status_code == 200, preserved.text
        assert preserved.json()["profile"]["confirmed_by"] == confirmed_by
        assert preserved.json()["profile"]["confirmed_at"] == confirmed_at
        assert preserved.json()["profile"]["source_note"] == "称量折算"
        invalidated = client.put(
            f"/api/deliveries/loading-profiles/{product_ids['BOX-1']}",
            json={"mode": "manual_unit", "manual_unit_m3": 0.4},
        )
        assert invalidated.status_code == 200, invalidated.text
        assert invalidated.json()["profile"]["confirmed"] is False
        assert invalidated.json()["profile"]["confirmed_by"] is None
        assert invalidated.json()["profile"]["confirmed_at"] is None
        assert invalidated.json()["profile"]["source_note"] == "称量折算"

        search = client.get("/api/deliveries/loading-profiles/search", params={"keyword": "BOX"})
        assert search.status_code == 200, search.text
        rows = search.json()["items"]
        assert {row["product_code"] for row in rows} == {"BOX-1", "BOX-2", "BOX-X"}
        assert all(set(row) == outer_keys for row in rows)
        by_code = {row["product_code"]: row for row in rows}
        assert by_code["BOX-1"]["profile"]["mode"] == "manual_unit"
        assert by_code["BOX-2"]["profile"] is None
        assert by_code["BOX-X"]["profile"] is None
        detail = client.get(f"/api/deliveries/loading-profiles/{product_ids['BOX-1']}")
        assert detail.status_code == 200, detail.text
        assert set(detail.json()) == outer_keys
        assert detail.json()["profile"]["confirmed_at"] is None
        customer_filtered = client.get(
            "/api/deliveries/loading-profiles/search",
            params={"customer_id": customer_ids["C1"]},
        )
        assert customer_filtered.status_code == 200
        assert {
            row["customer_id"] for row in customer_filtered.json()["items"]
        } == {customer_ids["C1"]}

        login(client, "sales")
        scoped = client.get("/api/deliveries/loading-profiles/search")
        assert scoped.status_code == 200, scoped.text
        assert {row["customer_id"] for row in scoped.json()["items"]} == {
            customer_ids["C1"]
        }
        forbidden_filter = client.get(
            "/api/deliveries/loading-profiles/search",
            params={"customer_id": customer_ids["C2"]},
        )
        assert forbidden_filter.status_code == 403
        assert client.get(
            f"/api/deliveries/loading-profiles/{product_ids['BOX-X']}"
        ).status_code == 403

        login(client, "admin")
        cleared = client.put(
            f"/api/deliveries/loading-profiles/{product_ids['BOX-1']}",
            json={"mode": "manual_unit", "manual_unit_m3": 0.4, "confirmed": False},
        )
        assert cleared.status_code == 200, cleared.text
        assert set(cleared.json()) == outer_keys
        assert cleared.json()["profile"]["confirmed"] is False
        assert cleared.json()["profile"]["confirmed_by"] is None
        assert cleared.json()["profile"]["confirmed_at"] is None


def test_vehicle_profile_preview_create_update_dispatch_and_legacy(loading_app) -> None:
    from app.models.order import OrderItem
    from app.models.product import Product

    app, factory = loading_app
    with factory() as db:
        other_order_item_id = db.scalar(
            select(OrderItem.id)
            .join(Product, Product.id == OrderItem.product_id)
            .where(Product.product_code == "BOX-X")
        )
        assert other_order_item_id is not None
    with TestClient(app) as client:
        login(client, "finance")
        assert client.post("/api/deliveries/vehicles", json=vehicle_payload()).status_code == 403
        login(client, "admin")
        vehicle_id = create_vehicle(client)
        assert client.get("/api/deliveries/vehicles").json()["items"][0]["effective_volume_m3"] == "1.00000000"
        login(client, "finance")
        assert client.put(
            f"/api/deliveries/vehicles/{vehicle_id}",
            json=vehicle_payload(name="unauthorized"),
        ).status_code == 403
        login(client, "admin")
        profile = client.put("/api/deliveries/loading-profiles/1", json=profile_payload(manual_unit_m3=0.8))
        assert profile.status_code == 200, profile.text
        missing_customer = client.post(
            "/api/deliveries/loading-preview",
            json={
                "vehicle_id": vehicle_id,
                "items": [{"order_item_id": 1, "delivered_quantity": 1}],
            },
        )
        assert missing_customer.status_code == 422
        cross_customer = client.post(
            "/api/deliveries/loading-preview",
            json={
                "customer_id": 1,
                "vehicle_id": vehicle_id,
                "items": [
                    {
                        "order_item_id": other_order_item_id,
                        "delivered_quantity": 1,
                    }
                ],
            },
        )
        assert cross_customer.status_code == 400
        preview = client.post("/api/deliveries/loading-preview", json={"customer_id": 1, "vehicle_id": vehicle_id, "items": [{"order_item_id": 1, "delivered_quantity": 1}]})
        assert preview.status_code == 200, preview.text
        assert preview.json()["status"] == "warning"
        login(client, "admin")
        created = client.post("/api/deliveries", json={"customer_id": 1, "vehicle_id": vehicle_id, "items": [{"order_item_id": 1, "delivered_quantity": 1}]})
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["vehicle_number"] == "苏E-N032"
        assert body["loading"]["status"] == "warning"
        delivery_id = body["id"]
        updated = client.put(f"/api/deliveries/{delivery_id}", json={"vehicle_id": vehicle_id, "items": [{"order_item_id": 1, "delivered_quantity": 1, "remarks": "复核后装车"}]})
        assert updated.status_code == 200, updated.text
        assert updated.json()["loading"]["status"] == "warning"
        cleared_vehicle = client.put(
            f"/api/deliveries/{delivery_id}",
            json={
                "vehicle_id": None,
                "items": [{"order_item_id": 1, "delivered_quantity": 1}],
            },
        )
        assert cleared_vehicle.status_code == 200, cleared_vehicle.text
        assert cleared_vehicle.json()["vehicle_id"] is None
        assert cleared_vehicle.json()["vehicle_number"] is None
        assert cleared_vehicle.json()["loading"]["status"] == "not_evaluated"
        rebound = client.put(
            f"/api/deliveries/{delivery_id}",
            json={
                "vehicle_id": vehicle_id,
                "items": [{"order_item_id": 1, "delivered_quantity": 1}],
            },
        )
        assert rebound.status_code == 200, rebound.text
        body = rebound.json()
        confirmation_required = client.put(f"/api/deliveries/{delivery_id}/dispatch")
        assert confirmation_required.status_code == 409
        assert confirmation_required.json()["detail"]["code"] == "LOADING_CONFIRMATION_REQUIRED"
        stale = client.put(
            f"/api/deliveries/{delivery_id}/dispatch",
            json={"loading_confirmation": True, "expected_loading_hash": "0" * 64},
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "LOADING_HASH_EXPIRED"
        assert stale.json()["detail"]["loading"]["hash"] == body["loading"]["hash"]
        assert stale.json()["detail"]["expected_loading_hash"] == body["loading"]["hash"]
        dispatched = client.put(f"/api/deliveries/{delivery_id}/dispatch", json={"loading_confirmation": True, "expected_loading_hash": body["loading"]["hash"]})
        assert dispatched.status_code == 200, dispatched.text
        with factory() as db:
            delivery = db.get(__import__("app.models.delivery", fromlist=["Delivery"]).Delivery, delivery_id)
            assert delivery.loading_confirmed_by is not None
            assert delivery.loading_confirmed_hash == body["loading"]["hash"]
        legacy = client.post("/api/deliveries", json={"customer_id": 1, "vehicle_number": "临时车", "items": [{"order_item_id": 1, "delivered_quantity": 1}]})
        assert legacy.status_code == 201, legacy.text
        assert legacy.json()["loading"]["status"] == "not_evaluated"
        assert client.put(f"/api/deliveries/{legacy.json()['id']}/dispatch").status_code == 200


def _migration_module():
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "bd57v8x9z47_n032_vehicle_loading.py"
    spec = importlib.util.spec_from_file_location("n032_migration", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_connection():
    engine = create_engine("sqlite://")
    connection = engine.connect()
    connection.execute(text("PRAGMA foreign_keys=ON"))
    connection.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
    connection.execute(text("CREATE TABLE products (id INTEGER PRIMARY KEY)"))
    connection.execute(text("CREATE TABLE sales_deliveries (id INTEGER PRIMARY KEY)"))
    connection.execute(text("CREATE TABLE sales_delivery_items (id INTEGER PRIMARY KEY)"))
    return connection


def test_migration_upgrade_empty_downgrade_and_fact_guard() -> None:
    module = _migration_module()
    connection = _migration_connection()
    module.op = Operations(MigrationContext.configure(connection))
    module.upgrade()
    assert "delivery_vehicles" in inspect(connection).get_table_names()
    assert "vehicle_id" in {column["name"] for column in inspect(connection).get_columns("sales_deliveries")}
    assert "ix_sales_deliveries_vehicle_id" in {
        index["name"] for index in inspect(connection).get_indexes("sales_deliveries")
    }
    with pytest.raises(IntegrityError):
        connection.execute(text("INSERT INTO product_loading_profiles (product_id, mode) VALUES (999, 'theoretical_box')"))
    module.downgrade()
    assert "delivery_vehicles" not in inspect(connection).get_table_names()
    assert "ix_sales_deliveries_vehicle_id" not in {
        index["name"] for index in inspect(connection).get_indexes("sales_deliveries")
    }
    connection.close()

    connection = _migration_connection()
    module.op = Operations(MigrationContext.configure(connection))
    module.upgrade()
    connection.execute(text("INSERT INTO delivery_vehicles (name, plate_number, cargo_length_mm, cargo_width_mm, cargo_height_mm) VALUES ('车', '苏E-GUARD', 1, 1, 1)"))
    with pytest.raises(RuntimeError, match="恢复迁移前备份"):
        module.downgrade()
    connection.close()


@pytest.mark.parametrize(
    ("column", "value_sql"),
    [
        ("vehicle_id", "999"),
        ("vehicle_capacity_snapshot_json", "'{}'"),
        ("loading_total_snapshot_json", "'{}'"),
        ("estimated_total_volume_m3", "'0.25'"),
        ("load_rate_pct", "'25.00'"),
        ("loading_status", "'warning'"),
        ("loading_calculation_hash", "'hash'"),
        ("loading_confirmed_by", "999"),
        ("loading_confirmed_at", "'2026-07-17 08:00:00'"),
        ("loading_confirmed_hash", "'confirmed-hash'"),
    ],
)
def test_migration_downgrade_rejects_each_delivery_fact(
    column: str,
    value_sql: str,
) -> None:
    module = _migration_module()
    connection = _migration_connection()
    module.op = Operations(MigrationContext.configure(connection))
    module.upgrade()
    connection.commit()
    connection.execute(text("PRAGMA foreign_keys=OFF"))
    connection.execute(
        text(f"INSERT INTO sales_deliveries ({column}) VALUES ({value_sql})")
    )
    with pytest.raises(RuntimeError, match="恢复迁移前备份"):
        module.downgrade()
    connection.close()
