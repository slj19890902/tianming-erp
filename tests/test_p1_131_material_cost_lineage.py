from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.test_p1_81_receipt_purpose_flow import (
    _create_frozen_sources,
    _freeze_receipt_fact,
    _receive,
    _seed_material_and_staging,
    _use_p181_published_map_identity,
)
from tests.test_phase11_requisition import _login, requisition_app


@pytest.mark.parametrize(
    ("currency", "expected_ready"),
    [("CNY", True), ("USD", False)],
)
def test_actual_purchase_receipt_cost_freezes_at_delivery_and_reports_coverage(
    requisition_app,
    monkeypatch: pytest.MonkeyPatch,
    currency: str,
    expected_ready: bool,
) -> None:
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.material_cost import FinanceDeliveryMaterialCostFact
    from app.models.order import Order, OrderItem
    from app.models.user import User
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation
    from app.services.material_cost_lineage import (
        freeze_delivery_inventory_material_cost,
        material_cost_coverage_report,
    )
    from app.services.warehouse_inventory import consume_finished_reservation
    from app.api.cost_accounting import router as cost_router

    app, session_factory = requisition_app
    app.include_router(cost_router, prefix="/api/finance")
    _use_p181_published_map_identity(monkeypatch)
    _seed_material_and_staging(session_factory)

    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=10,
            purchase_total=10,
            order_purpose=10,
            stock_purpose=0,
        )[0]
        frozen = _freeze_receipt_fact(
            client,
            source,
            idempotency_key=f"p1131-a1-price-{currency.lower()}",
            unit_price="2.5000",
            currency=currency,
        )
        assert frozen.status_code == 200, frozen.text
        received = _receive(
            client,
            source,
            frozen.json(),
            quantity=10,
            idempotency_key=f"p1131-a1-receive-{currency.lower()}",
        )
        if currency != "CNY":
            assert received.status_code == 422, received.text
            assert "人民币" in received.text
            return
        assert received.status_code == 200, received.text

    with session_factory() as db:
        order_item = db.get(OrderItem, 1)
        order = db.get(Order, order_item.order_id if order_item else None)
        admin = db.scalar(select(User).where(User.username == "admin"))
        reservation = db.scalar(
            select(InventoryReservation)
            .where(
                InventoryReservation.order_item_id == 1,
                InventoryReservation.reservation_type == "finished_order",
                InventoryReservation.status != "cancelled",
            )
            .order_by(InventoryReservation.id)
        )
        assert order_item is not None and order is not None and admin is not None
        assert reservation is not None
        lot = db.get(InventoryLot, reservation.inventory_lot_id)
        assert lot is not None
        assert lot.cost_snapshot_source == "purchase_receipt_actual"

        delivery = Delivery(
            delivery_number="DL-P1131-A1-001",
            customer_id=order.customer_id,
            delivery_date=date(2026, 9, 1),
            source_mode="order",
            status="dispatched",
            total_quantity=5,
            created_by=admin.id,
            dispatched_by=admin.id,
        )
        db.add(delivery)
        db.flush()
        delivery_item = DeliveryItem(
            delivery_id=delivery.id,
            source_type="order",
            order_item_id=order_item.id,
            delivered_quantity=5,
        )
        db.add(delivery_item)
        db.flush()

        mutation = consume_finished_reservation(
            db,
            reservation_id=reservation.id,
            stock_quantity=5,
            expected_version=lot.version,
            operator_id=admin.id,
            idempotency_key="p1131-a1-dispatch",
            delivery_item_id=delivery_item.id,
        )
        assert mutation.allocation is not None
        facts = list(db.scalars(select(FinanceDeliveryMaterialCostFact)).all())
        assert len(facts) == 1
        fact = facts[0]
        assert fact.delivery_inventory_allocation_id == mutation.allocation.id
        assert fact.inventory_lot_id == lot.id
        assert fact.purchase_receipt_fact_id == frozen.json()["receipt_fact_id"]
        assert fact.consumed_quantity == 5
        assert fact.unit_material_cost == lot.estimated_unit_cost_snapshot
        assert fact.total_material_cost == (
            Decimal(str(fact.unit_material_cost)) * Decimal("5")
        ).quantize(Decimal("0.000001"))

        replay = freeze_delivery_inventory_material_cost(
            db,
            allocation=mutation.allocation,
            lot=lot,
            operator_id=admin.id,
        )
        assert replay is not None and replay.id == fact.id
        assert db.scalar(
            select(func.count()).select_from(FinanceDeliveryMaterialCostFact)
        ) == 1

        report = material_cost_coverage_report(db, month="2026-09")
        # OPT001: the overview uses exactly the monthly dispatch-cost read model,
        # not the product/statement cost fields, and an empty customer scope is empty.
        from app.services.material_cost_lineage import material_cost_overview
        overview = material_cost_overview(
            db, months=["2026-08", "2026-09", "2026-09"], can_view_costs=True,
            visible_customer_ids={order.customer_id},
        )
        assert overview["total_lines"] == report["total_delivery_lines"]
        assert overview["covered_lines"] == report["management_covered_lines"]
        assert overview["material_cost_amount"] == report["management_material_cost"]
        assert overview["months"] == ["2026-08", "2026-09"]
        empty_scope = material_cost_overview(
            db, months=["2026-09"], can_view_costs=True, visible_customer_ids=set(),
        )
        assert empty_scope["total_lines"] == 0
        assert empty_scope["material_cost_amount"] == Decimal("0.00")
        assert report["lineage_ready"] is expected_ready
        assert report["total_delivery_lines"] == 1
        assert report["frozen_source_count"] == 1
        assert report["currency_totals"] == [
            {
                "currency": currency,
                "amount": Decimal(str(fact.total_material_cost)).quantize(
                    Decimal("0.01")
                ),
            }
        ]
        if currency == "CNY":
            assert report["covered_delivery_lines"] == 1
            assert report["uncovered_delivery_lines"] == 0
            assert report["foreign_currency_source_count"] == 0
            assert report["actual_material_cost"] == Decimal(
                str(fact.total_material_cost)
            ).quantize(Decimal("0.01"))
            assert report["missing_details"] == []
        else:
            assert report["covered_delivery_lines"] == 0
            assert report["uncovered_delivery_lines"] == 1
            assert report["foreign_currency_source_count"] == 1
            assert report["actual_material_cost"] == Decimal("0.00")
            assert report["missing_details"][0]["reason_codes"] == [
                "foreign_currency_rate_missing"
            ]
        db.commit()

    with TestClient(app) as client:
        _login(client, "admin")
        response = client.get(
            "/api/finance/material-cost/coverage",
            params={"month": "2026-09"},
        )
        assert response.status_code == 200, response.text
        api_report = response.json()
        assert api_report["lineage_ready"] is expected_ready
        assert api_report["frozen_source_count"] == 1
        assert api_report["currency_totals"][0]["currency"] == currency


def test_estimated_inventory_is_reported_but_never_promoted_to_actual_cost(
    requisition_app,
) -> None:
    from app.models.customer import Customer
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.material_cost import FinanceDeliveryMaterialCostFact
    from app.models.product import Product
    from app.models.user import User
    from app.models.warehouse_inventory import (
        FinishedGoodsInventoryDetail,
        InventoryLot,
        UnorderedFinishedDeliveryAllocation,
        WarehouseLocation,
    )
    from app.services.material_cost_lineage import (
        freeze_unordered_delivery_material_cost,
        material_cost_coverage_report,
    )

    _app, session_factory = requisition_app
    with session_factory() as db:
        customer = db.scalar(select(Customer).order_by(Customer.id))
        product = db.scalar(select(Product).order_by(Product.id))
        admin = db.scalar(select(User).where(User.username == "admin"))
        assert customer is not None and product is not None and admin is not None
        location = WarehouseLocation(
            location_code="P1131-EST-01",
            location_name="成本缺口测试位",
            warehouse_type="finished",
            is_active=True,
            warehouse_floor=1,
            area_code="TEST",
            storage_type="ground",
            placement_status="placed",
            source_version="P1-131-A1",
        )
        db.add(location)
        db.flush()
        lot = InventoryLot(
            lot_number="FG-P1131-EST-001",
            inventory_type="finished",
            warehouse_location_id=location.id,
            quantity_available=0,
            quantity_consumed=5,
            unit="boxes",
            status="active",
            source_type="manual",
            stock_date=date(2026, 9, 1),
            last_movement_at=datetime(2026, 9, 1),
            estimated_unit_cost_snapshot=Decimal("1.2500"),
            cost_snapshot_source="material_quote_area",
            created_by=admin.id,
        )
        db.add(lot)
        db.flush()
        db.add(
            FinishedGoodsInventoryDetail(
                inventory_lot_id=lot.id,
                owner_customer_id=customer.id,
                owner_customer_name_snapshot=customer.name,
                is_general=False,
                product_id=product.id,
                inventory_code_snapshot=product.product_code,
                product_name_snapshot=product.product_name,
            )
        )
        delivery = Delivery(
            delivery_number="DL-P1131-EST-001",
            customer_id=customer.id,
            delivery_date=date(2026, 9, 2),
            source_mode="unordered_finished",
            status="dispatched",
            total_quantity=5,
            created_by=admin.id,
            dispatched_by=admin.id,
        )
        db.add(delivery)
        db.flush()
        item = DeliveryItem(
            delivery_id=delivery.id,
            source_type="unordered_finished",
            product_id=product.id,
            product_code_snapshot=product.product_code,
            product_name_snapshot=product.product_name,
            unit_snapshot="boxes",
            price_source="pending",
            delivered_quantity=5,
        )
        db.add(item)
        db.flush()
        allocation = UnorderedFinishedDeliveryAllocation(
            delivery_item_id=item.id,
            inventory_lot_id=lot.id,
            planned_quantity=5,
            consumed_quantity=5,
            restored_quantity=0,
            status="dispatched",
            created_by=admin.id,
        )
        db.add(allocation)
        db.flush()

        assert freeze_unordered_delivery_material_cost(
            db,
            allocation=allocation,
            lot=lot,
            operator_id=admin.id,
        ) is None
        assert db.scalar(
            select(func.count()).select_from(FinanceDeliveryMaterialCostFact)
        ) == 0
        report = material_cost_coverage_report(db, month="2026-09")
        assert report["lineage_ready"] is False
        assert report["covered_delivery_lines"] == 0
        assert report["uncovered_delivery_lines"] == 1
        assert report["estimate_only_source_count"] == 1
        assert report["actual_material_cost"] == Decimal("0.00")
        assert report["missing_details"][0]["reason_codes"] == ["estimate_only"]
