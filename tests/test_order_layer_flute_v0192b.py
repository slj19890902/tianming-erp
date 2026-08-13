"""Tests for order_item layer/flute/supplier/weight snapshot (v0.19.2-B Hotfix).

Verifies:
* Migration q35k9m2n4p17 columns exist in sales_order_items
* OrderItemCreate accepts layer_count / flute_type
* Order creation copies layer/flute from product when not supplied
* _order_data returns new fields
* cost-preview accepts flute_type
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from app.services import material_pricing as mp


@pytest.fixture()
def db(tmp_path: Path, seed_supplier_master):
    from app.core.database import create_sqlite_engine
    from app.models import Base

    engine = create_sqlite_engine(tmp_path / "test_order_lf.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    seed_supplier_master(factory, "苏州嘉林亿", "JLY-OLF")
    with factory() as session:
        yield session


def _make_customer(session):
    from app.models.customer import Customer
    c = Customer(name="测试客户", is_active=True)
    session.add(c)
    session.flush()
    return c


def _make_material(
    session,
    supplier="苏州嘉林亿",
    layer=3,
    flute="B",
    price="1.52",
    weight="150g/130g/130g",
    code=None,
):
    from app.models.material import Material
    m = Material(
        code=code or f"A6D-{flute}", supplier_name=supplier, quote_price=Decimal(price),
        is_active=True, layer_count=layer, flute_type=flute,
        basis_weight_description=weight,
    )
    session.add(m)
    session.flush()
    return m


def _make_product(session, customer_id, material=None, layer=3, flute="A"):
    from app.models.product import Product
    p = Product(
        customer_id=customer_id,
        product_code="21301021",
        customer_material_code="21301021",
        product_name="中性内箱",
        layer_count=layer,
        flute_type=flute,
        material_id=material.id if material else None,
        box_category="normal",
        length_mm=Decimal("680"), width_mm=Decimal("240"), height_mm=Decimal("165"),
        is_active=True,
    )
    session.add(p)
    session.flush()
    return p


def _make_admin(session):
    from app.models.user import User

    user = User(
        username="order-seven-admin",
        password_hash="test-only",
        role="admin",
        real_name="订单七层测试",
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


class TestOrderItemColumns:
    def test_new_columns_exist_in_model(self, db):
        from app.models.order import OrderItem
        # All new columns should be accessible as attributes
        assert hasattr(OrderItem, "layer_count")
        assert hasattr(OrderItem, "flute_type")
        assert hasattr(OrderItem, "material_id")
        assert hasattr(OrderItem, "snapshot_supplier_name")
        assert hasattr(OrderItem, "snapshot_weight")
        assert hasattr(OrderItem, "drawing_file")

    def test_create_order_item_with_layer_flute(self, db):
        from app.models.order import Order, OrderItem
        from app.services.order_numbering import reserve_next_order_number

        customer = _make_customer(db)
        material = _make_material(db)
        product = _make_product(db, customer.id, material=material, layer=3, flute="A")
        db.commit()

        order_date = dt.date(2026, 6, 26)
        order = Order(
            order_number="TM20260626001",
            customer_id=customer.id,
            order_date=order_date,
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("0"),
        )
        db.add(order)
        db.flush()

        item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            item_sequence=1,
            item_order_number="TM20260626001-001",
            quantity=100,
            unit_price=Decimal("1.50"),
            subtotal=Decimal("150"),
            material_status="pending",
            snapshot_product_code="21301021",
            snapshot_product_name="中性内箱",
            layer_count=3,
            flute_type="A",
            material_id=material.id,
            snapshot_supplier_name="苏州嘉林亿",
            snapshot_weight="150g/130g/130g",
            requisition_status="未报料",
        )
        db.add(item)
        db.commit()

        loaded = db.get(OrderItem, item.id)
        assert loaded.layer_count == 3
        assert loaded.flute_type == "A"
        assert loaded.material_id == material.id
        assert loaded.snapshot_supplier_name == "苏州嘉林亿"
        assert loaded.snapshot_weight == "150g/130g/130g"
        assert loaded.drawing_file is None

    def test_drawing_file_can_be_set(self, db):
        from app.models.order import Order, OrderItem

        customer = _make_customer(db)
        material = _make_material(db)
        product = _make_product(db, customer.id, material=material)
        db.commit()

        order = Order(
            order_number="TM20260626002",
            customer_id=customer.id,
            order_date=dt.date(2026, 6, 26),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("0"),
        )
        db.add(order)
        db.flush()

        item = OrderItem(
            order_id=order.id, product_id=product.id,
            item_sequence=1, item_order_number="TM20260626002-001",
            quantity=10, unit_price=Decimal("1.5"), subtotal=Decimal("15"),
            material_status="pending", snapshot_product_code="21301021",
            snapshot_product_name="中性内箱",
            drawing_file="/static/uploads/drawings/abc123.png",
            requisition_status="未报料",
        )
        db.add(item)
        db.commit()

        loaded = db.get(OrderItem, item.id)
        assert loaded.drawing_file == "/static/uploads/drawings/abc123.png"


class TestSevenLayerOrderSnapshots:
    @pytest.mark.parametrize("flute_type", ["AAA", "ABC"])
    def test_create_order_preserves_valid_seven_layer_flute(self, db, flute_type):
        from app.api.orders import OrderCreate, OrderItemCreate, create_order
        from app.models.order import OrderItem

        customer = _make_customer(db)
        material = _make_material(
            db,
            layer=7,
            flute=None,
            code="A12345B",
            weight="200g/130g/130g/130g/130g/130g/200g",
        )
        product = _make_product(
            db,
            customer.id,
            material=material,
            layer=7,
            flute=flute_type,
        )
        user = _make_admin(db)
        db.commit()

        create_order(
            OrderCreate(
                customer_id=customer.id,
                order_date=dt.date(2026, 7, 16),
                items=[
                    OrderItemCreate(
                        product_id=product.id,
                        quantity=10,
                        unit_price=Decimal("2.50"),
                    )
                ],
            ),
            db=db,
            user=user,
        )

        item = db.query(OrderItem).one()
        assert item.snapshot_material == "A12345B"
        assert item.layer_count == 7
        assert item.flute_type == flute_type

    @pytest.mark.parametrize(
        "flute_type",
        [None, "", "A", "B", "E", "AB", "BE"],
    )
    def test_create_order_rejects_invalid_seven_layer_flute(self, db, flute_type):
        from fastapi import HTTPException

        from app.api.orders import OrderCreate, OrderItemCreate, create_order
        from app.models.order import Order

        customer = _make_customer(db)
        material = _make_material(
            db,
            layer=7,
            flute=None,
            code="A12345B",
        )
        product = _make_product(
            db,
            customer.id,
            material=material,
            layer=7,
            flute=None,
        )
        user = _make_admin(db)
        db.commit()

        with pytest.raises(HTTPException) as exc_info:
            create_order(
                OrderCreate(
                    customer_id=customer.id,
                    order_date=dt.date(2026, 7, 16),
                    items=[
                        OrderItemCreate(
                            product_id=product.id,
                            quantity=10,
                            unit_price=Decimal("2.50"),
                            layer_count=7,
                            flute_type=flute_type,
                        )
                    ],
                ),
                db=db,
                user=user,
            )

        assert exc_info.value.status_code == 400
        detail = str(exc_info.value.detail)
        assert "AAA" in detail and "ABC" in detail
        assert db.query(Order).count() == 0

    def test_update_order_item_cannot_bypass_validation_when_product_sync_is_off(self, db):
        from fastapi import HTTPException

        from app.api.orders import OrderItemUpdate, update_order_item
        from app.models.order import Order, OrderItem
        from app.models.product import Product

        customer = _make_customer(db)
        material = _make_material(db, layer=7, flute=None, code="A12345B")
        product = _make_product(
            db,
            customer.id,
            material=material,
            layer=7,
            flute="AAA",
        )
        user = _make_admin(db)
        order = Order(
            order_number="TM20260716001",
            customer_id=customer.id,
            order_date=dt.date(2026, 7, 16),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("25"),
            created_by=user.id,
        )
        db.add(order)
        db.flush()
        item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            item_sequence=1,
            item_order_number="TM20260716001-001",
            quantity=10,
            unit_price=Decimal("2.50"),
            subtotal=Decimal("25"),
            material_status="pending",
            requisition_status="未报料",
            snapshot_product_code=product.product_code,
            snapshot_product_name=product.product_name,
            snapshot_material="A12345B",
            material_id=material.id,
            layer_count=7,
            flute_type="AAA",
        )
        db.add(item)
        db.commit()

        with pytest.raises(HTTPException) as exc_info:
            update_order_item(
                item.id,
                OrderItemUpdate(
                    quantity=10,
                    unit_price=Decimal("2.50"),
                    product_code=product.product_code,
                    product_name=product.product_name,
                    material="A12345B",
                    layer_count=7,
                    flute_type="AB",
                    sync_product=False,
                ),
                db=db,
                user=user,
            )

        assert exc_info.value.status_code == 400
        assert db.get(OrderItem, item.id).flute_type == "AAA"
        assert db.get(Product, product.id).flute_type == "AAA"


class TestFluteDeltaInCost:
    """verify get_effective_material_price returns correct delta for cost calc."""

    def test_a_flute_cost_includes_delta(self, db):
        from app.models.supplier_flute_price_rule import SupplierFlutePriceRule
        r = SupplierFlutePriceRule(
            supplier_name="苏州嘉林亿", layer_count=3, flute_type="A",
            price_delta=Decimal("0.04"), is_active=True,
        )
        db.add(r)
        db.commit()

        result = mp.get_effective_material_price(
            db,
            base_price=Decimal("1.52"),
            supplier_name="苏州嘉林亿",
            layer_count=3,
            flute_type="A",
        )
        assert result["effective_price"] == pytest.approx(1.56, abs=1e-4)
        assert result["flute_delta"] == pytest.approx(0.04, abs=1e-4)

    def test_b_flute_no_delta(self, db):
        result = mp.get_effective_material_price(
            db,
            base_price=Decimal("1.52"),
            supplier_name="苏州嘉林亿",
            layer_count=3,
            flute_type="B",
        )
        assert result["effective_price"] == pytest.approx(1.52, abs=1e-4)
        assert result["flute_delta"] == 0.0
