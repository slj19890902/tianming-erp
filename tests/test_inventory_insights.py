from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.warehouse_inventory import WarehouseLocation
from app.services.inventory_insights import build_inventory_insights
from app.services.warehouse_inventory import manual_finished_in


@pytest.fixture()
def db(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "inventory-insights.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session


def seed_finished_lot(
    db: Session,
    *,
    quantity: int = 50,
    cost: Decimal | None = None,
) -> tuple[Product, object]:
    customer = Customer(
        customer_number=9712,
        customer_code="INSIGHT-CUSTOMER",
        name="库存洞察测试客户",
        payment_term_days=0,
        credit_limit=0,
    )
    db.add(customer)
    db.flush()
    product = Product(
        customer_id=customer.id,
        product_code="INSIGHT-P001",
        customer_material_code="INSIGHT-M001",
        product_name="库存洞察成品箱",
        box_category="normal",
        box_style="A1",
        cost_unit_price=cost,
    )
    location = WarehouseLocation(
        location_code="INSIGHT-FG-01",
        location_name="洞察测试库位",
        warehouse_type="finished",
    )
    db.add_all([product, location])
    db.flush()
    lot = manual_finished_in(
        db,
        customer_id=customer.id,
        product_id=product.id,
        location_id=location.id,
        quantity=quantity,
        stock_date=date(2026, 1, 1),
        source_type="stocktake",
        remarks="只读看板测试",
        operator_id=None,
        idempotency_key="inventory-insights-lot",
    )
    return product, lot


def add_open_order(db: Session, product: Product, *, as_of: date) -> None:
    order = Order(
        order_number="TM-INSIGHT-001",
        customer_id=product.customer_id,
        order_date=as_of,
        delivery_date=as_of,
        status="pending_delivery",
        payment_status="unpaid",
        total_amount=Decimal("30.00"),
    )
    db.add(order)
    db.flush()
    db.add(
        OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=30,
            delivered_quantity=0,
            unit_price=Decimal("1.0000"),
            subtotal=Decimal("30.00"),
            snapshot_product_name=product.product_name,
            snapshot_product_code=product.product_code,
        )
    )
    db.flush()


def test_empty_insights_are_explicit_and_never_fake_cash_value(db: Session) -> None:
    result = build_inventory_insights(db, as_of=date(2026, 7, 12))

    assert result["summary"]["recorded_lots"] == 0
    assert result["summary"]["actual_inventory_value"] is None
    assert result["summary"]["estimated_inventory_value"] is None
    assert result["data_quality"]["actual_cost_supported"] is False
    assert result["data_quality"]["cost_coverage_percent"] is None
    assert result["action_items"] == []
    assert "只统计已录入 ERP" in result["scope_notice"]


def test_finished_stock_links_its_own_demand_and_flags_missing_cost(db: Session) -> None:
    as_of = date(2026, 7, 12)
    product, lot = seed_finished_lot(db)
    lot.last_movement_at = datetime.combine(as_of - timedelta(days=200), datetime.min.time())
    add_open_order(db, product, as_of=as_of)
    db.flush()

    result = build_inventory_insights(db, as_of=as_of)
    action = result["action_items"][0]
    codes = {row["code"] for row in action["reasons"]}

    assert result["summary"]["finished_available"] == 50
    assert result["summary"]["actual_inventory_value"] is None
    assert result["data_quality"]["missing_cost_lots"] == 1
    assert action["detail"]["inventory_code"] == "INSIGHT-P001"
    assert action["demand"]["open_demand"] == 30
    assert action["demand"]["demand_30"] == 30
    assert action["age_days"] == 200
    assert {"finished_stock_can_cover_order", "age_slow", "cost_pending"} <= codes


def test_product_cost_is_only_estimated_and_kept_separate_from_actual_value(db: Session) -> None:
    _product, _lot = seed_finished_lot(db, quantity=40, cost=Decimal("2.5000"))
    db.flush()

    result = build_inventory_insights(db, as_of=date(2026, 7, 12))
    action = result["action_items"][0]

    assert result["summary"]["actual_inventory_value"] is None
    assert result["summary"]["estimated_inventory_value"] == "100.00"
    assert result["data_quality"]["cost_ready_lots"] == 1
    assert result["data_quality"]["missing_cost_lots"] == 0
    assert action["cost_status"] == "estimated_product_cost"
    assert action["estimated_unit_cost"] == "2.50"
    assert action["estimated_value"] == "100.00"
