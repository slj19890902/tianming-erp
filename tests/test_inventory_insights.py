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
from app.models.warehouse_inventory import InventoryMovement, WarehouseLocation
from app.services.inventory_insights import _movement_stagnant_days, build_inventory_insights
from app.services.warehouse_inventory import (
    manual_finished_in,
    manual_semi_finished_in,
    replace_semi_finished_lot_allowed_products,
    reserve_finished_inventory,
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
        source_type="stocktake",
        remarks="只读看板测试",
        operator_id=None,
        idempotency_key="inventory-insights-lot",
    )
    return product, lot


def add_open_order(db: Session, product: Product, *, as_of: date) -> OrderItem:
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
    item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=30,
            delivered_quantity=0,
            unit_price=Decimal("1.0000"),
            subtotal=Decimal("30.00"),
            snapshot_product_name=product.product_name,
            snapshot_product_code=product.product_code,
        )
    db.add(item)
    db.flush()
    return item


def add_inventory_movement(
    db: Session,
    *,
    lot,
    movement_type: str,
    created_at: datetime,
    sequence: int,
    order_item_id: int | None = None,
) -> InventoryMovement:
    movement = InventoryMovement(
        movement_number=f"INSIGHT-MOVE-{sequence:03d}",
        inventory_lot_id=lot.id,
        movement_type=movement_type,
        quantity=1,
        unit=lot.unit,
        before_available=lot.quantity_available,
        after_available=lot.quantity_available,
        before_reserved=lot.quantity_reserved,
        after_reserved=lot.quantity_reserved,
        before_consumed=lot.quantity_consumed,
        after_consumed=lot.quantity_consumed,
        before_damaged=lot.quantity_damaged,
        after_damaged=lot.quantity_damaged,
        before_scrapped=lot.quantity_scrapped,
        after_scrapped=lot.quantity_scrapped,
        related_order_item_id=order_item_id,
        idempotency_key=f"inventory-insight-move-{sequence:03d}",
        created_at=created_at,
    )
    db.add(movement)
    db.flush()
    return movement


def test_empty_insights_are_explicit_and_never_fake_cash_value(db: Session) -> None:
    result = build_inventory_insights(db, as_of=date(2026, 7, 12))

    assert result["summary"]["recorded_lots"] == 0
    assert result["summary"]["actual_inventory_value"] is None
    assert result["summary"]["confirmed_material_inventory_value"] is None
    assert result["summary"]["estimated_inventory_value"] is None
    assert result["data_quality"]["actual_cost_supported"] is False
    assert result["data_quality"]["cost_coverage_percent"] is None
    assert result["recommendation_readiness"]["status"] == "no_inventory_data"
    assert result["recommendation_readiness"]["rule_based_ready"] is False
    assert result["recommendation_readiness"]["ai_analysis_ready"] is False
    assert result["recommendation_readiness"]["movement_count"] == 0
    assert result["action_items"] == []
    assert "只统计已录入 ERP" in result["scope_notice"]


def test_finished_stock_links_its_own_demand_and_flags_missing_cost(db: Session) -> None:
    as_of = date(2026, 7, 12)
    product, lot = seed_finished_lot(db)
    lot.stock_date = as_of - timedelta(days=200)
    lot.last_movement_at = datetime.combine(as_of, datetime.min.time())
    movement = db.scalar(
        select(InventoryMovement).where(
            InventoryMovement.inventory_lot_id == lot.id
        )
    )
    assert movement is not None
    movement.created_at = datetime.combine(as_of, datetime.min.time())
    add_open_order(db, product, as_of=as_of)
    db.flush()

    result = build_inventory_insights(db, as_of=as_of)
    action = result["action_items"][0]
    codes = {row["code"] for row in action["reasons"]}

    assert result["summary"]["finished_available"] == 50
    assert result["summary"]["actual_inventory_value"] is None
    assert result["data_quality"]["missing_cost_lots"] == 1
    readiness = result["recommendation_readiness"]
    assert readiness["status"] == "blocked_actual_cost"
    assert readiness["rule_based_ready"] is True
    assert readiness["ai_analysis_ready"] is False
    assert readiness["ai_procurement_ready"] is False
    assert readiness["movement_count"] >= 1
    assert readiness["movement_history_days"] >= 1
    assert any("纸板材料成本快照" in row for row in readiness["reasons"])
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
    assert action["coverage_basis"] == "finished_free_stock_plus_active_reservations"
    assert action["active_reserved_demand_quantity"] == 0
    assert action["net_unreserved_demand_quantity"] == 30
    assert action["free_covered_demand_quantity"] == 30
    assert "finished_stock_exceeds_open_demand" in {
        row["code"] for row in action["reasons"]
    }


def test_finished_coverage_counts_reservations_once_across_multiple_lots(
    db: Session,
) -> None:
    as_of = date(2026, 7, 12)
    product, primary_lot = seed_finished_lot(db, quantity=20)
    item = add_open_order(db, product, as_of=as_of)
    second_location = WarehouseLocation(
        location_code="INSIGHT-FG-02",
        location_name="洞察测试第二库位",
        warehouse_type="finished",
    )
    db.add(second_location)
    db.flush()
    secondary_lot = manual_finished_in(
        db,
        customer_id=product.customer_id,
        product_id=product.id,
        location_id=second_location.id,
        quantity=15,
        stock_date=date(2026, 1, 2),
        source_type="stocktake",
        remarks="同款第二批次",
        operator_id=None,
        idempotency_key="inventory-insights-lot-2",
    )
    reserve_finished_inventory(
        db,
        order_item_id=item.id,
        inventory_lot_id=primary_lot.id,
        quantity=10,
        expected_version=primary_lot.version,
        operator_id=None,
        idempotency_key="inventory-insights-reservation",
        warning_acknowledged_codes=[],
    )
    db.flush()

    result = build_inventory_insights(db, as_of=as_of)
    by_lot = {row["lot_id"]: row for row in result["action_items"]}
    primary = by_lot[primary_lot.id]
    secondary = by_lot[secondary_lot.id]

    assert primary["demand"]["open_demand"] == 30
    assert primary["demand"]["active_reserved_demand"] == 10
    assert primary["demand"]["net_unreserved_demand"] == 20
    assert primary["free_available_quantity"] == 25
    assert primary["free_covered_demand_quantity"] == 20
    assert primary["covered_demand_quantity"] == 30
    assert primary["uncovered_demand_quantity"] == 0
    assert primary["coverage_percent"] == 100.0
    assert primary["coverage_is_primary"] is True
    assert secondary["coverage_is_primary"] is False
    assert secondary["coverage_primary_lot_number"] == primary_lot.lot_number
    assert secondary["covered_demand_quantity"] is None
    assert not any(
        reason["code"] == "finished_stock_can_cover_order"
        for reason in secondary["reasons"]
    )


def test_consumed_reservation_no_longer_covers_open_demand(db: Session) -> None:
    as_of = date(2026, 7, 12)
    product, lot = seed_finished_lot(db, quantity=20)
    item = add_open_order(db, product, as_of=as_of)
    reservation = reserve_finished_inventory(
        db,
        order_item_id=item.id,
        inventory_lot_id=lot.id,
        quantity=10,
        expected_version=lot.version,
        operator_id=None,
        idempotency_key="inventory-insights-partial-consume",
        warning_acknowledged_codes=[],
    )
    reservation.consumed_stock_quantity = 5
    reservation.consumed_requirement_quantity = 5
    reservation.status = "partial"
    lot.quantity_reserved = 5
    lot.quantity_consumed = 5
    item.delivered_quantity = 5
    db.flush()

    action = build_inventory_insights(db, as_of=as_of)["action_items"][0]

    assert action["demand"]["open_demand"] == 25
    assert action["active_reserved_demand_quantity"] == 5
    assert action["net_unreserved_demand_quantity"] == 20
    assert action["free_available_quantity"] == 10
    assert action["covered_demand_quantity"] == 15
    assert action["uncovered_demand_quantity"] == 10


def test_frozen_finished_inventory_is_not_counted_as_free_coverage(
    db: Session,
) -> None:
    as_of = date(2026, 7, 12)
    product, lot = seed_finished_lot(db, quantity=20)
    add_open_order(db, product, as_of=as_of)
    lot.status = "frozen"
    db.flush()

    result = build_inventory_insights(db, as_of=as_of)
    action = result["action_items"][0]

    assert result["summary"]["finished_available"] == 0
    assert result["summary"]["usable_lots"] == 0
    assert result["recommendation_readiness"]["status"] == "no_usable_inventory"
    assert action["coverage_basis"] == "finished_inventory_inactive"
    assert action["free_available_quantity"] == 0
    assert action["covered_demand_quantity"] == 0
    assert action["uncovered_demand_quantity"] == 30
    assert "inventory_lot_not_active" in {
        reason["code"] for reason in action["reasons"]
    }


def test_damage_scrap_and_freeze_do_not_satisfy_ai_consumption_history(
    db: Session,
) -> None:
    as_of = date(2026, 7, 20)
    _product, lot = seed_finished_lot(db, quantity=20)
    add_inventory_movement(
        db,
        lot=lot,
        movement_type="damage",
        created_at=datetime(2026, 1, 1, 8),
        sequence=1,
    )
    add_inventory_movement(
        db,
        lot=lot,
        movement_type="scrap",
        created_at=datetime(2026, 4, 1, 8),
        sequence=2,
    )
    add_inventory_movement(
        db,
        lot=lot,
        movement_type="freeze",
        created_at=datetime(2026, 7, 19, 8),
        sequence=3,
    )

    readiness = build_inventory_insights(db, as_of=as_of)[
        "recommendation_readiness"
    ]

    assert readiness["consumption_movement_count"] == 0
    assert readiness["recent_90_day_movement_count"] == 0
    assert readiness["movement_data_ready"] is False
    assert readiness["ai_analysis_ready"] is False


def test_beijing_day_boundary_excludes_next_day_order_consumption(
    db: Session,
) -> None:
    as_of = date(2026, 7, 20)
    product, lot = seed_finished_lot(db, quantity=20)
    item = add_open_order(db, product, as_of=as_of)
    add_inventory_movement(
        db,
        lot=lot,
        movement_type="consume",
        created_at=datetime(2026, 7, 20, 15, 59, 59),
        sequence=1,
        order_item_id=item.id,
    )
    add_inventory_movement(
        db,
        lot=lot,
        movement_type="consume",
        created_at=datetime(2026, 7, 20, 16, 0, 0),
        sequence=2,
        order_item_id=item.id,
    )

    readiness = build_inventory_insights(db, as_of=as_of)[
        "recommendation_readiness"
    ]

    assert readiness["consumption_movement_count"] == 1
    assert readiness["recent_90_day_movement_count"] == 1
    assert readiness["movement_data_ready"] is False


def test_consumption_months_follow_beijing_business_date(db: Session) -> None:
    as_of = date(2026, 7, 20)
    product, lot = seed_finished_lot(db, quantity=20)
    item = add_open_order(db, product, as_of=as_of)
    for sequence, created_at in enumerate(
        (
            datetime(2026, 3, 31, 16, 30),
            datetime(2026, 4, 30, 16, 30),
            datetime(2026, 5, 31, 16, 30),
        ),
        1,
    ):
        add_inventory_movement(
            db,
            lot=lot,
            movement_type="consume",
            created_at=created_at,
            sequence=sequence,
            order_item_id=item.id,
        )

    readiness = build_inventory_insights(db, as_of=as_of)[
        "recommendation_readiness"
    ]

    assert readiness["consumption_movement_count"] == 3
    assert readiness["consumption_month_count"] == 3


def test_ai_movement_gate_requires_repeated_order_consumption_across_months(
    db: Session,
) -> None:
    as_of = date(2026, 7, 20)
    product, lot = seed_finished_lot(db, quantity=20)
    item = add_open_order(db, product, as_of=as_of)
    dates = [
        datetime(2026, 3, 1 + index, 8) for index in range(3)
    ] + [
        datetime(2026, 4, 1 + index, 8) for index in range(3)
    ] + [
        datetime(2026, 5, 1 + index, 8) for index in range(3)
    ] + [
        datetime(2026, 7, 1 + index, 8) for index in range(3)
    ]
    for sequence, created_at in enumerate(dates, 1):
        add_inventory_movement(
            db,
            lot=lot,
            movement_type="consume",
            created_at=created_at,
            sequence=sequence,
            order_item_id=item.id,
        )

    readiness = build_inventory_insights(db, as_of=as_of)[
        "recommendation_readiness"
    ]

    assert readiness["consumption_movement_count"] == 12
    assert readiness["consumption_month_count"] == 4
    assert readiness["consumption_history_days"] >= 90
    assert readiness["recent_90_day_movement_count"] > 0
    assert readiness["movement_data_ready"] is True
    assert readiness["ai_analysis_ready"] is False


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
        source_type="stocktake",
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
    assert "只属于估算" in result["data_quality"]["actual_cost_message"]


def test_stock_in_board_cost_snapshot_remains_an_estimate(
    db: Session,
) -> None:
    _product, lot = seed_finished_lot(db, quantity=40)
    lot.estimated_unit_cost_snapshot = Decimal("2.5000")
    lot.estimated_square_price_snapshot = Decimal("1.2500")
    lot.estimated_cost_area_m2_snapshot = Decimal("2.000000")
    lot.cost_snapshot_source = "material_quote_area"
    lot.cost_snapshot_detail_json = (
        '{"formula":"length_mm * width_mm / 1,000,000 * square_price"}'
    )
    lot.cost_snapshot_at = datetime(2026, 1, 1, 8, 0, 0)
    db.flush()

    result = build_inventory_insights(db, as_of=date(2026, 7, 12))
    action = result["action_items"][0]

    assert result["summary"]["confirmed_material_inventory_value"] is None
    assert result["summary"]["actual_inventory_value"] is None
    assert result["summary"]["estimated_inventory_value"] == "100.00"
    assert result["data_quality"]["actual_cost_supported"] is False
    assert result["data_quality"]["confirmed_material_cost_lots"] == 0
    assert result["data_quality"]["actual_material_cost_coverage_percent"] is None
    assert result["data_quality"]["material_snapshot_coverage"] == 100.0
    assert action["cost_status"] == "estimated_snapshot"
    assert "只属于估算" in result["data_quality"]["actual_cost_message"]
