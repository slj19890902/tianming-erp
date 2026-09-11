from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.warehouse_inventory import WarehouseLocation
from app.services.inventory_insights import _movement_stagnant_days, build_inventory_insights
from app.services.warehouse_inventory import (
    manual_finished_in,
    manual_semi_finished_in,
    replace_semi_finished_lot_allowed_products,
)


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
        source_type="manual",  # Historical fixtures may lack a frozen cost; new stocktake now requires one.
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
    lot.stock_date = as_of - timedelta(days=200)
    lot.last_movement_at = datetime.combine(as_of, datetime.min.time())
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


def test_finished_coverage_keeps_age_on_stock_date_and_separates_stagnation(db: Session) -> None:
    as_of = date(2026, 7, 12)
    product, lot = seed_finished_lot(db, quantity=50)
    lot.stock_date = as_of - timedelta(days=200)
    lot.last_movement_at = datetime.combine(as_of, datetime.min.time())
    add_open_order(db, product, as_of=as_of)
    db.flush()

    result = build_inventory_insights(db, as_of=as_of)
    action = result["action_items"][0]

    assert action["age_days"] == 200
    assert action["age_basis"] == "stock_date"
    assert action["last_movement_at"] is not None
    assert action["movement_stagnant_days"] == 0
    assert action["covered_demand_quantity"] == 30
    assert action["uncovered_demand_quantity"] == 0
    assert action["coverage_percent"] == 100.0
    assert action["coverage_basis"] == "finished_available_vs_open_order_demand"
    assert "finished_stock_exceeds_open_demand" in {row["code"] for row in action["reasons"]}


def test_partial_and_zero_demand_coverage_are_explicit(db: Session) -> None:
    as_of = date(2026, 7, 12)
    product, lot = seed_finished_lot(db, quantity=20)
    lot.stock_date = as_of - timedelta(days=200)
    add_open_order(db, product, as_of=as_of)
    db.flush()

    partial = build_inventory_insights(db, as_of=as_of)["action_items"][0]
    assert (partial["covered_demand_quantity"], partial["uncovered_demand_quantity"]) == (20, 10)
    assert partial["coverage_percent"] == 66.7

    partial_order = db.scalar(select(Order).where(Order.order_number == "TM-INSIGHT-001"))
    assert partial_order is not None
    partial_order.status = "cancelled"
    db.flush()
    zero = build_inventory_insights(db, as_of=as_of)["action_items"][0]
    assert (zero["covered_demand_quantity"], zero["uncovered_demand_quantity"]) == (0, 0)
    assert zero["coverage_percent"] is None
    assert "no_demand_180" in {row["code"] for row in zero["reasons"]}


def test_missing_last_movement_time_is_explicitly_not_used_for_age() -> None:
    assert _movement_stagnant_days(None, date(2026, 7, 12)) is None


def test_unknown_stock_date_is_separate_from_precise_age_buckets(
    db: Session,
) -> None:
    _product, lot = seed_finished_lot(db)
    lot.stock_date_accuracy = "unknown"
    lot.stock_date_original_text = None
    db.flush()

    result = build_inventory_insights(db, as_of=date(2026, 7, 12))
    action = result["action_items"][0]
    unknown_bucket = next(
        row for row in result["age_buckets"] if row["key"] == "unknown"
    )

    assert action["age_days"] is None
    assert action["age_basis"] == "stock_date_unknown"
    assert "stock_date_unknown" in {
        row["code"] for row in action["reasons"]
    }
    assert unknown_bucket["lots"] == 1
    assert result["data_quality"]["unknown_stock_date_lots"] == 1
    assert result["data_quality"]["exact_stock_date_lots"] == 0


def test_semi_finished_candidate_relationship_is_read_only(db: Session) -> None:
    as_of = date(2026, 7, 12)
    product, _finished_lot = seed_finished_lot(db)
    add_open_order(db, product, as_of=as_of)
    semi_location = WarehouseLocation(
        location_code="INSIGHT-SF-01",
        location_name="洞察测试半成品库位",
        warehouse_type="semi_finished",
    )
    db.add(semi_location)
    db.flush()
    semi_lot = manual_semi_finished_in(
        db,
        location_id=semi_location.id,
        quantity=12,
        stock_date=as_of - timedelta(days=10),
        source_type="manual",  # Historical fixtures may lack a frozen cost; new stocktake now requires one.
        material_code="C3C",
        layer_count=3,
        flute_type="B",
        board_length_mm=400,
        board_width_mm=300,
        sheet_type="net_sheet",
        supplier_name="洞察测试供应商",
        customer_id=product.customer_id,
        crease_type=None,
        crease_left_mm=None,
        crease_middle_mm=None,
        crease_right_mm=None,
        cutting_note=None,
        remarks="只读候选测试",
        operator_id=None,
        idempotency_key="inventory-insights-semi-lot",
    )
    replace_semi_finished_lot_allowed_products(
        db,
        inventory_lot_id=semi_lot.id,
        product_ids=[product.id],
        expected_version=semi_lot.version,
        operator_id=None,
    )
    db.flush()
    before = (semi_lot.quantity_available, semi_lot.quantity_reserved, semi_lot.quantity_consumed)

    result = build_inventory_insights(db, as_of=as_of)
    action = next(row for row in result["action_items"] if row["lot_id"] == semi_lot.id)

    assert action["detail"]["candidate_relationship_read_only"] is True
    assert action["detail"]["assigned_products"] == [
        {"product_id": product.id, "inventory_code": product.product_code, "name": product.product_name}
    ]
    assert action["coverage_basis"] == "semi_finished_candidate_relationship_read_only"
    assert action["covered_demand_quantity"] is None
    assert action["coverage_percent"] is None
    assert (semi_lot.quantity_available, semi_lot.quantity_reserved, semi_lot.quantity_consumed) == before


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
    assert result["data_quality"]["product_reference_coverage"] == 100.0
    assert result["data_quality"]["snapshot_estimate_coverage"] == 0.0
    assert result["data_quality"]["current_quote_coverage"] == 0.0
    assert "估算来源" in result["data_quality"]["actual_cost_message"]


def test_frozen_only_assistant_does_not_adopt_current_product_reference(db: Session) -> None:
    _product, _lot = seed_finished_lot(db, quantity=40, cost=Decimal("2.5000"))
    db.flush()
    before=(_lot.estimated_unit_cost_snapshot,_lot.version)
    result=build_inventory_insights(db,as_of=date(2026,7,12),frozen_cost_only=True,action_limit=None)
    assert result["data_quality"]["cost_ready_lots"]==0
    assert result["summary"]["estimated_inventory_value"] is None
    assert result["action_items"][0]["cost_status"]=="pending"
    assert (_lot.estimated_unit_cost_snapshot,_lot.version)==before
