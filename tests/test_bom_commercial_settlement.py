from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import pytest

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_n029_production_integration import _login, n029_delivery_app


def _create_component_priced_order(factory, ids, client):
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.product_bom import ProductBomComponent
    from app.models.warehouse_inventory import (
        FinishedGoodsInventoryDetail,
        InventoryLot,
        InventoryReservation,
    )

    with factory() as db:
        customer = db.get(Customer, ids["customer"])
        customer.statement_cycle_start_day = 1
        parent = Product(
            customer_id=customer.id,
            product_code="SETTLE-PARENT-3-4",
            customer_material_code="SETTLE-PARENT-3-4",
            product_name="三长四短组合",
            box_category="normal",
            is_composite=True,
            combination_mode="component_priced",
        )
        long_piece = Product(
            customer_id=customer.id,
            product_code="SETTLE-LONG",
            customer_material_code="SETTLE-LONG",
            product_name="长片",
            box_category="normal",
            is_internal_component=True,
        )
        short_piece = Product(
            customer_id=customer.id,
            product_code="SETTLE-SHORT",
            customer_material_code="SETTLE-SHORT",
            product_name="短片",
            box_category="normal",
            is_internal_component=True,
        )
        db.add_all([parent, long_piece, short_piece])
        db.flush()
        db.add_all(
            [
                ProductBomComponent(
                    parent_product_id=parent.id,
                    component_product_id=long_piece.id,
                    quantity_per_set=3,
                    display_order=1,
                    internal_component_code="SETTLE-LONG",
                ),
                ProductBomComponent(
                    parent_product_id=parent.id,
                    component_product_id=short_piece.id,
                    quantity_per_set=4,
                    display_order=2,
                    internal_component_code="SETTLE-SHORT",
                ),
            ]
        )
        db.commit()
        specifications = [(long_piece, 300, 3, "1.25"), (short_piece, 400, 4, "0.80")]
        group_key = "SETTLE-COMPONENT-100-GROUP"
        created = client.post("/api/orders", json={
            "customer_id": customer.id,
            "order_date": date.today().isoformat(),
            "items": [dict(
                product_id=product.id, quantity=quantity, unit_price=price,
                combination_mode_snapshot="component_priced",
                combination_role="priced_component",
                combination_group_key=group_key,
                combination_parent_product_id=parent.id,
                combination_parent_name_snapshot=parent.product_name,
                combination_set_quantity_snapshot=100,
                combination_quantity_per_set_snapshot=per_set,
            ) for product, quantity, per_set, price in specifications],
        })
        assert created.status_code == 201, created.text
        assert Decimal(str(created.json()["total_amount"])) == Decimal("695.00")
        order = db.get(Order, created.json()["id"])
        saved_items = {row["product_id"]: row["id"] for row in created.json()["items"]}
        items = []
        for sequence, (product, quantity, per_set, price) in enumerate(
            (
                (long_piece, 300, 3, Decimal("1.25")),
                (short_piece, 400, 4, Decimal("0.80")),
            ),
            start=1,
        ):
            item = db.get(OrderItem, saved_items[product.id])
            assert item.quantity == quantity
            assert item.unit_price == price
            assert item.combination_quantity_per_set_snapshot == per_set
            lot = InventoryLot(
                lot_number=f"SETTLE-LOT-{sequence}",
                inventory_type="finished",
                warehouse_location_id=ids["temporary_location"],
                quantity_available=quantity,
                quantity_reserved=0,
                quantity_consumed=0,
                quantity_damaged=0,
                quantity_scrapped=0,
                unit="boxes",
                status="active",
                source_type="manual",
                stock_date=date.today(),
                last_movement_at=datetime.utcnow(),
                version=1,
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
            db.flush()
            from app.services.warehouse_inventory import reserve_finished_inventory
            reserve_finished_inventory(db, order_item_id=item.id,
                inventory_lot_id=lot.id, quantity=quantity, expected_version=lot.version,
                operator_id=ids["user"], idempotency_key=f"SETTLE-RS-{sequence}",
                warning_acknowledged_codes=[])
            items.append(item)
        db.commit()
        return order.id, items[0].id, items[1].id


def _create_parent_priced_order(factory, ids):
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.warehouse_inventory import (
        FinishedGoodsInventoryDetail,
        InventoryLot,
        InventoryReservation,
    )

    with factory() as db:
        customer = db.get(Customer, ids["customer"])
        customer.statement_cycle_start_day = 1
        parent = Product(
            customer_id=customer.id,
            product_code="SETTLE-ASSEMBLED-PARENT",
            customer_material_code="SETTLE-ASSEMBLED-PARENT",
            product_name="工厂组装成套产品",
            box_category="normal",
            is_composite=True,
            combination_mode="parent_priced_set",
        )
        db.add(parent)
        db.flush()
        order = Order(
            order_number="SETTLE-PARENT-100",
            customer_id=customer.id,
            order_date=date.today(),
            delivery_date=date.today(),
            status="pending_delivery",
            payment_status="unpaid",
            total_amount=Decimal("850.00"),
        )
        db.add(order)
        db.flush()
        item = OrderItem(
            order_id=order.id,
            product_id=parent.id,
            item_sequence=1,
            item_order_number=f"{order.order_number}-001",
            quantity=100,
            delivered_quantity=0,
            unit_price=Decimal("8.50"),
            subtotal=Decimal("850.00"),
            material_status="received",
            requisition_status="已入库",
            snapshot_product_code=parent.product_code,
            snapshot_product_name=parent.product_name,
            combination_mode_snapshot="parent_priced_set",
            combination_role="set_parent",
        )
        db.add(item)
        db.flush()
        lot = InventoryLot(
            lot_number="SETTLE-ASSEMBLED-LOT",
            inventory_type="finished",
            warehouse_location_id=ids["temporary_location"],
            quantity_available=0,
            quantity_reserved=100,
            quantity_consumed=0,
            quantity_damaged=0,
            quantity_scrapped=0,
            unit="boxes",
            status="active",
            source_type="manual",
            stock_date=date.today(),
            last_movement_at=datetime.utcnow(),
            version=1,
        )
        db.add(lot)
        db.flush()
        db.add(
            FinishedGoodsInventoryDetail(
                inventory_lot_id=lot.id,
                owner_customer_id=customer.id,
                owner_customer_name_snapshot=customer.name,
                is_general=False,
                product_id=parent.id,
                inventory_code_snapshot=parent.product_code,
                product_name_snapshot=parent.product_name,
            )
        )
        db.add(
            InventoryReservation(
                reservation_number="SETTLE-ASSEMBLED-RS",
                inventory_lot_id=lot.id,
                reservation_type="finished_order",
                order_id=order.id,
                order_item_id=item.id,
                reserved_stock_quantity=100,
                credited_requirement_quantity=100,
                yield_factor=1,
                status="active",
                idempotency_key="SETTLE-ASSEMBLED-RS",
            )
        )
        db.commit()
        return order.id, item.id


def _dispatch(client: TestClient, customer_id: int, quantities: list[tuple[int, int]]):
    created = client.post(
        "/api/deliveries",
        json={
            "customer_id": customer_id,
            "items": [
                {"order_item_id": item_id, "delivered_quantity": quantity}
                for item_id, quantity in quantities
            ],
        },
    )
    assert created.status_code == 201, created.text
    dispatched = client.put(f"/api/deliveries/{created.json()['id']}/dispatch")
    assert dispatched.status_code == 200, dispatched.text
    return dispatched.json()


def _confirm_receipt(client: TestClient, delivery: dict, *, short_received: int = 0,
                     return_location_id=None, return_layout_versions=None) -> dict:
    response = client.post(
        "/api/finance/return_receipts",
        json={
            "delivery_id": delivery["id"],
            "actual_received_date": date.today().isoformat(),
            "items": [
                {
                    "delivery_item_id": line["id"],
                    "actual_received_quantity": line["delivered_quantity"] - short_received,
                    **({"resolution_action": "continue_delivery", "difference_reason": "隔离验收：客户短收，继续待送",
                        "return_location_id": (return_location_id[line["order_item_id"]]
                            if isinstance(return_location_id, dict) else return_location_id),
                        "expected_return_layout_version": (return_layout_versions or {}).get(line["order_item_id"])}
                       if short_received else {}),
                }
                for line in delivery["items"]
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize("short_received", [0, 20])
def test_component_priced_delivery_settles_real_child_quantities_not_pairable_sets(
    n029_delivery_app, short_received,
) -> None:
    from app.models.delivery import DeliveryItem
    from app.models.finance import ReturnReceiptItem, Statement, StatementItem
    from app.models.order import Order, OrderItem

    app, factory, ids = n029_delivery_app
    with TestClient(app) as client:
        _login(client)
        order_id, long_item_id, short_item_id = _create_component_priced_order(factory, ids, client)
        return_locations = None
        if short_received:
            from app.models.warehouse_inventory import WarehouseLocation
            with factory() as db:
                location = WarehouseLocation(location_code="SETTLE-SHORT-RETURN",
                    location_name="隔离短片退回区", warehouse_type="finished", is_active=True)
                db.add(location)
                db.commit()
                return_locations = {long_item_id: ids["return_location"], short_item_id: location.id}
        first = _dispatch(
            client,
            ids["customer"],
            [(long_item_id, 120), (short_item_id, 100)],
        )
        with factory() as db:
            assert db.get(OrderItem, long_item_id).delivered_quantity == 120
            assert db.get(OrderItem, short_item_id).delivered_quantity == 100
            assert db.get(Order, order_id).status != "delivered"

        second = _dispatch(
            client,
            ids["customer"],
            [(long_item_id, 180), (short_item_id, 300)],
        )
        cancelled = client.put(f"/api/deliveries/{second['id']}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        with factory() as db:
            assert db.get(OrderItem, long_item_id).delivered_quantity == 120
            assert db.get(OrderItem, short_item_id).delivered_quantity == 100
            assert db.get(Order, order_id).status != "delivered"

        replacement = _dispatch(
            client,
            ids["customer"],
            [(long_item_id, 180), (short_item_id, 300)],
        )
        _confirm_receipt(client, first, short_received=short_received, return_location_id=return_locations)
        _confirm_receipt(client, replacement)
        statement = client.post(
            "/api/finance/statements",
            json={
                "customer_id": ids["customer"],
                "statement_month": date.today().strftime("%Y-%m"),
                "delivery_ids": [first["id"], replacement["id"]],
            },
        )
        assert statement.status_code == 201, statement.text

    with factory() as db:
        stored_statement = db.get(Statement, statement.json()["id"])
        assert stored_statement.total_receivable == Decimal("695.00") - short_received * Decimal("2.05")
        rows = db.execute(
            select(
                DeliveryItem.order_item_id,
                StatementItem.actual_received_quantity,
                StatementItem.unit_price_snapshot,
                StatementItem.receivable_amount,
            )
            .join(
                ReturnReceiptItem,
                ReturnReceiptItem.id == StatementItem.return_receipt_item_id,
            )
            .join(DeliveryItem, DeliveryItem.id == ReturnReceiptItem.delivery_item_id)
            .where(StatementItem.statement_id == stored_statement.id)
            .order_by(DeliveryItem.order_item_id, StatementItem.id)
        ).all()
        assert rows == [
            (long_item_id, 120-short_received, Decimal("1.2500"), Decimal("150.00")-short_received*Decimal("1.25")),
            (long_item_id, 180, Decimal("1.2500"), Decimal("225.00")),
            (short_item_id, 100-short_received, Decimal("0.8000"), Decimal("80.00")-short_received*Decimal("0.80")),
            (short_item_id, 300, Decimal("0.8000"), Decimal("240.00")),
        ]
        assert (db.get(Order, order_id).status == "delivered") is (short_received == 0)
        assert db.get(OrderItem, long_item_id).delivered_quantity == 300-short_received
        assert db.get(OrderItem, short_item_id).delivered_quantity == 400-short_received


@pytest.mark.parametrize("short_received", [0, 5])
def test_parent_priced_assembled_order_settles_actual_received_sets(
    n029_delivery_app, short_received,
) -> None:
    from app.models.finance import Statement, StatementItem

    app, factory, ids = n029_delivery_app
    _order_id, item_id = _create_parent_priced_order(factory, ids)
    with TestClient(app) as client:
        _login(client)
        delivery = _dispatch(client, ids["customer"], [(item_id, 25)])
        _confirm_receipt(client, delivery, short_received=short_received, return_location_id=ids["return_location"])
        statement = client.post(
            "/api/finance/statements",
            json={
                "customer_id": ids["customer"],
                "statement_month": date.today().strftime("%Y-%m"),
                "delivery_ids": [delivery["id"]],
            },
        )
        assert statement.status_code == 201, statement.text

    with factory() as db:
        stored = db.get(Statement, statement.json()["id"])
        assert stored.total_receivable == Decimal("212.50") - short_received * Decimal("8.50")
        line = db.scalar(
            select(StatementItem).where(StatementItem.statement_id == stored.id)
        )
        assert line.actual_received_quantity == 25-short_received
        assert line.unit_price_snapshot == Decimal("8.5000")
        assert line.receivable_amount == Decimal("212.50") - short_received * Decimal("8.50")
