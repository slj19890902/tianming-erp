from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.user import User
from app.models.warehouse_inventory import (
    InventoryReservation,
    SemiFinishedLotAllowedProduct,
    WarehouseLocation,
)
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    edit_semi_finished_lot_customer,
    manual_semi_finished_in,
    replace_semi_finished_lot_allowed_products,
    semi_finished_lot_allowed_product_ids,
    void_semi_finished_lot,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "au48v8x9y0q38"
TARGET_REVISION = "av49v8x9y0r39"


def _alembic_config(monkeypatch: pytest.MonkeyPatch, database_path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database_path.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "semi-lot-binding-test")
    return Config(str(PROJECT_ROOT / "alembic.ini"))


def test_migration_creates_constraints_and_refuses_downgrade_with_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "semi-lot-binding.sqlite3"
    config = _alembic_config(monkeypatch, database_path)
    command.upgrade(config, PREVIOUS_REVISION)
    command.upgrade(config, TARGET_REVISION)

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(semi_finished_lot_allowed_products)"
            )
        }
        assert columns == {
            "id", "inventory_lot_id", "product_id", "confirmed_by", "confirmed_at"
        }
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(semi_finished_lot_allowed_products)"
        ).fetchall()
        assert {
            (row[2], row[3], row[6]) for row in foreign_keys
        } >= {
            ("inventory_lots", "inventory_lot_id", "CASCADE"),
            ("products", "product_id", "RESTRICT"),
            ("users", "confirmed_by", "SET NULL"),
        }
        assert any(
            row[1] == "ix_semi_finished_lot_allowed_products_product_lot"
            and [column[2] for column in connection.execute(
                "PRAGMA index_info(ix_semi_finished_lot_allowed_products_product_lot)"
            )] == ["product_id", "inventory_lot_id"]
            for row in connection.execute(
                "PRAGMA index_list(semi_finished_lot_allowed_products)"
            )
        )
        assert any(
            row[2] == 1
            and [column[2] for column in connection.execute(
                f"PRAGMA index_info({row[1]})"
            )] == ["inventory_lot_id", "product_id"]
            for row in connection.execute(
                "PRAGMA index_list(semi_finished_lot_allowed_products)"
            )
        )
        connection.execute(
            "INSERT INTO semi_finished_lot_allowed_products "
            "(inventory_lot_id, product_id, confirmed_at) VALUES (1, 1, CURRENT_TIMESTAMP)"
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="禁止降级删除历史关联"):
        command.downgrade(config, PREVIOUS_REVISION)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            TARGET_REVISION,
        )


@pytest.fixture()
def lot_db(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "semi-lot-bindings.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="lot-binding-admin", password_hash="test", role="admin",
            real_name="批次绑定管理员", must_change_password=False,
        )
        customer = Customer(
            customer_number=9201, customer_code="LOT-A", name="批次客户A",
            payment_term_days=0, credit_limit=0,
        )
        other_customer = Customer(
            customer_number=9202, customer_code="LOT-B", name="批次客户B",
            payment_term_days=0, credit_limit=0,
        )
        db.add_all([admin, customer, other_customer])
        db.flush()
        products = [
            Product(
                customer_id=customer.id, product_code=f"LOT-P{index}",
                customer_material_code=f"LOT-M{index}", product_name=f"批次款号{index}",
                box_category="normal", default_material_code="A416D", flute_type="B",
                report_length_mm=800, report_width_mm=600, is_active=True,
            )
            for index in range(1, 4)
        ]
        other_product = Product(
            customer_id=other_customer.id, product_code="LOT-OTHER",
            customer_material_code="LOT-OTHER", product_name="跨客户款号",
            box_category="normal", is_active=True,
        )
        location = WarehouseLocation(
            location_code="LOT-SF-01", location_name="批次半成品库位",
            warehouse_type="semi_finished",
        )
        db.add_all([*products, other_product, location])
        db.flush()
        lot = manual_semi_finished_in(
            db, location_id=location.id, quantity=10, stock_date=date.today(),
            source_type="manual", material_code="A416D", layer_count=3,
            flute_type="B", board_length_mm=800, board_width_mm=600,
            sheet_type="net_sheet", component_type="whole", pieces_per_box=1,
            stock_yield_per_sheet=1, supplier_name=None, customer_id=customer.id,
            crease_type=None, crease_left_mm=None, crease_middle_mm=None,
            crease_right_mm=None, cutting_note=None, remarks=None,
            operator_id=admin.id, idempotency_key="lot-product-binding",
        )
        db.commit()
        yield db, {
            "admin": admin, "customer": customer, "products": products,
            "other_product": other_product, "lot": lot,
        }


def test_lot_binding_supports_multiple_products_and_versioning(lot_db) -> None:
    db, data = lot_db
    lot = replace_semi_finished_lot_allowed_products(
        db, inventory_lot_id=data["lot"].id,
        product_ids=[data["products"][0].id, data["products"][1].id, data["products"][0].id],
        expected_version=data["lot"].version, operator_id=data["admin"].id,
    )
    assert semi_finished_lot_allowed_product_ids(db, lot.id) == tuple(
        sorted([data["products"][0].id, data["products"][1].id])
    )
    assert lot.version == 2
    with pytest.raises(WarehouseInventoryError, match="刷新后重试"):
        replace_semi_finished_lot_allowed_products(
            db, inventory_lot_id=lot.id, product_ids=[], expected_version=1,
            operator_id=data["admin"].id,
        )
    cleared = replace_semi_finished_lot_allowed_products(
        db, inventory_lot_id=lot.id, product_ids=[], expected_version=lot.version,
        operator_id=data["admin"].id,
    )
    assert semi_finished_lot_allowed_product_ids(db, cleared.id) == ()
    assert cleared.version == 3


def test_lot_binding_rejects_other_customer_product(lot_db) -> None:
    db, data = lot_db
    with pytest.raises(WarehouseInventoryError, match="同一客户"):
        replace_semi_finished_lot_allowed_products(
            db, inventory_lot_id=data["lot"].id,
            product_ids=[data["other_product"].id],
            expected_version=data["lot"].version, operator_id=data["admin"].id,
        )
    assert semi_finished_lot_allowed_product_ids(db, data["lot"].id) == ()


def test_lot_binding_api_returns_lot_scope_and_rejects_stale_version(lot_db) -> None:
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.warehouse import router as warehouse_router
    from app.core.security import hash_password

    db, data = lot_db
    password = "LotBindingApi123!"
    data["admin"].password_hash = hash_password(password)
    db.commit()
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(warehouse_router, prefix="/api/warehouse")

    def override_get_db():
        with factory() as api_db:
            yield api_db

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            assert client.post("/api/auth/login", json={
                "username": data["admin"].username, "password": password,
            }).status_code == 200
            saved = client.put(
                f"/api/warehouse/lots/{data['lot'].id}/product-assignments",
                json={
                    "expected_version": data["lot"].version,
                    "product_ids": [data["products"][0].id, data["products"][1].id],
                },
            )
            assert saved.status_code == 200, saved.text
            body = saved.json()
            assert body["binding_scope"] == "lot"
            assert body["allowed_product_ids"] == sorted([
                data["products"][0].id, data["products"][1].id,
            ])
            assert body["version"] == data["lot"].version + 1
            stale = client.put(
                f"/api/warehouse/lots/{data['lot'].id}/product-assignments",
                json={"expected_version": data["lot"].version, "product_ids": []},
            )
            assert stale.status_code == 409
    finally:
        app.dependency_overrides.clear()


def test_cancel_customer_clears_lot_bindings_but_safe_void_keeps_history(lot_db) -> None:
    db, data = lot_db
    bound = replace_semi_finished_lot_allowed_products(
        db, inventory_lot_id=data["lot"].id, product_ids=[data["products"][0].id],
        expected_version=data["lot"].version, operator_id=data["admin"].id,
    )
    unassigned = edit_semi_finished_lot_customer(
        db, lot_id=bound.id, customer_id=None, expected_version=bound.version,
        operator_id=data["admin"].id,
    )
    assert semi_finished_lot_allowed_product_ids(db, unassigned.id) == ()

    reassigned = edit_semi_finished_lot_customer(
        db, lot_id=unassigned.id, customer_id=data["customer"].id,
        expected_version=unassigned.version, operator_id=data["admin"].id,
    )
    rebound = replace_semi_finished_lot_allowed_products(
        db, inventory_lot_id=reassigned.id, product_ids=[data["products"][0].id],
        expected_version=reassigned.version, operator_id=data["admin"].id,
    )
    voided = void_semi_finished_lot(
        db, lot_id=rebound.id, expected_version=rebound.version,
        reason="误录", operator_id=data["admin"].id,
    )
    assert voided.status == "closed"
    assert semi_finished_lot_allowed_product_ids(db, voided.id) == (data["products"][0].id,)


def test_active_reservation_blocks_removing_its_bound_product(lot_db) -> None:
    db, data = lot_db
    bound = replace_semi_finished_lot_allowed_products(
        db, inventory_lot_id=data["lot"].id, product_ids=[data["products"][0].id],
        expected_version=data["lot"].version, operator_id=data["admin"].id,
    )
    order = Order(
        order_number="LOT-BINDING-ORDER", customer_id=data["customer"].id,
        order_date=date.today(), status="pending_production", payment_status="unpaid",
        total_amount=Decimal("1"),
    )
    db.add(order)
    db.flush()
    item = OrderItem(
        order_id=order.id, product_id=data["products"][0].id, quantity=1,
        unit_price=Decimal("1"), subtotal=Decimal("1"), material_status="pending",
        requisition_status="未报料", snapshot_product_code="LOT-P1",
        snapshot_product_name="批次款号1", snapshot_spec="", snapshot_material="A416D",
    )
    db.add(item)
    db.flush()
    db.add(InventoryReservation(
        reservation_number="LOT-BINDING-RESERVATION", inventory_lot_id=bound.id,
        reservation_type="semi_order", order_id=order.id, order_item_id=item.id,
        reserved_stock_quantity=1, consumed_stock_quantity=0,
        released_stock_quantity=0, status="active",
    ))
    db.flush()
    with pytest.raises(WarehouseInventoryError, match="活跃预占或净消耗"):
        replace_semi_finished_lot_allowed_products(
            db, inventory_lot_id=bound.id, product_ids=[],
            expected_version=bound.version, operator_id=data["admin"].id,
        )
    with pytest.raises(WarehouseInventoryError, match="预占或消耗"):
        edit_semi_finished_lot_customer(
            db, lot_id=bound.id, customer_id=None,
            expected_version=bound.version, operator_id=data["admin"].id,
        )
    assert db.scalars(select(SemiFinishedLotAllowedProduct)).all()


def test_active_reservation_blocks_assigning_customer_to_general_lot(lot_db) -> None:
    db, data = lot_db
    general_lot = edit_semi_finished_lot_customer(
        db, lot_id=data["lot"].id, customer_id=None,
        expected_version=data["lot"].version, operator_id=data["admin"].id,
    )
    order = Order(
        order_number="LOT-GENERAL-ASSIGN-ORDER", customer_id=data["customer"].id,
        order_date=date.today(), status="pending_production", payment_status="unpaid",
        total_amount=Decimal("1"),
    )
    db.add(order)
    db.flush()
    item = OrderItem(
        order_id=order.id, product_id=data["products"][0].id, quantity=1,
        unit_price=Decimal("1"), subtotal=Decimal("1"), material_status="pending",
        requisition_status="未报料", snapshot_product_code="LOT-P1",
        snapshot_product_name="批次款号1", snapshot_spec="", snapshot_material="A416D",
    )
    db.add(item)
    db.flush()
    reservation = InventoryReservation(
        reservation_number="LOT-GENERAL-ASSIGN-RESERVATION",
        inventory_lot_id=general_lot.id, reservation_type="semi_order",
        order_id=order.id, order_item_id=item.id, reserved_stock_quantity=1,
        consumed_stock_quantity=0, released_stock_quantity=0, status="active",
    )
    db.add(reservation)
    db.flush()

    with pytest.raises(WarehouseInventoryError, match="预占或消耗"):
        edit_semi_finished_lot_customer(
            db, lot_id=general_lot.id, customer_id=data["customer"].id,
            expected_version=general_lot.version, operator_id=data["admin"].id,
        )


def test_released_reservation_allows_assigning_customer_to_general_lot(lot_db) -> None:
    db, data = lot_db
    general_lot = edit_semi_finished_lot_customer(
        db, lot_id=data["lot"].id, customer_id=None,
        expected_version=data["lot"].version, operator_id=data["admin"].id,
    )
    order = Order(
        order_number="LOT-GENERAL-RELEASE-ORDER", customer_id=data["customer"].id,
        order_date=date.today(), status="pending_production", payment_status="unpaid",
        total_amount=Decimal("1"),
    )
    db.add(order)
    db.flush()
    item = OrderItem(
        order_id=order.id, product_id=data["products"][0].id, quantity=1,
        unit_price=Decimal("1"), subtotal=Decimal("1"), material_status="pending",
        requisition_status="未报料", snapshot_product_code="LOT-P1",
        snapshot_product_name="批次款号1", snapshot_spec="", snapshot_material="A416D",
    )
    db.add(item)
    db.flush()
    db.add(InventoryReservation(
        reservation_number="LOT-GENERAL-RELEASE-RESERVATION",
        inventory_lot_id=general_lot.id, reservation_type="semi_order",
        order_id=order.id, order_item_id=item.id, reserved_stock_quantity=1,
        consumed_stock_quantity=0, released_stock_quantity=1, status="released",
    ))
    db.flush()

    assigned = edit_semi_finished_lot_customer(
        db, lot_id=general_lot.id, customer_id=data["customer"].id,
        expected_version=general_lot.version, operator_id=data["admin"].id,
    )
    assert assigned.semi_finished_detail.owner_customer_id == data["customer"].id
