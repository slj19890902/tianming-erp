from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def order_trace_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.orders import router as orders_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.finance import (
        Invoice,
        ReturnReceipt,
        ReturnReceiptItem,
        SettlementRecord,
        Statement,
        StatementItem,
    )
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.requisition import Requisition, RequisitionItem
    from app.models.user import User
    from app.models.warehouse_inventory import (
        FinishedGoodsInventoryDetail,
        InventoryLot,
        WarehouseLocation,
    )

    engine = create_sqlite_engine(tmp_path / "p1-04-order-trace.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    base_time = datetime(2026, 7, 29, 1, 0, 0)
    with factory() as db:
        admin = User(
            username="trace-admin",
            password_hash=hash_password("RolePass123!"),
            role="admin",
            real_name="Trace Admin",
            must_change_password=False,
        )
        sales = User(
            username="trace-sales",
            password_hash=hash_password("RolePass123!"),
            role="sales",
            real_name="Trace Sales",
            must_change_password=False,
            customer_access_mode="selected",
        )
        customer_a = Customer(
            customer_number=401,
            customer_code="TRA",
            name="追溯客户甲",
        )
        customer_b = Customer(
            customer_number=402,
            customer_code="TRB",
            name="追溯客户乙",
        )
        db.add_all([admin, sales, customer_a, customer_b])
        db.flush()
        product_a = Product(
            customer_id=customer_a.id,
            product_code="TRACE-A",
            customer_material_code="TRACE-A",
            product_name="追溯纸箱甲",
        )
        product_b = Product(
            customer_id=customer_b.id,
            product_code="TRACE-B",
            customer_material_code="TRACE-B",
            product_name="追溯纸箱乙",
        )
        db.add_all([product_a, product_b])
        db.flush()
        unrelated_location = WarehouseLocation(
            location_code="E1-L99",
            location_name="同款但无单据关联的测试库位",
            warehouse_type="finished",
        )
        db.add(unrelated_location)
        db.flush()
        unrelated_lot = InventoryLot(
            lot_number="LOT-UNRELATED-SAME-PRODUCT",
            inventory_type="finished",
            warehouse_location_id=unrelated_location.id,
            quantity_available=99,
            quantity_reserved=0,
            quantity_consumed=0,
            quantity_damaged=0,
            quantity_scrapped=0,
            unit="boxes",
            status="active",
            source_type="manual",
            stock_date=date(2026, 7, 29),
            stock_date_accuracy="exact",
            last_movement_at=base_time,
        )
        db.add(unrelated_lot)
        db.flush()
        db.add(
            FinishedGoodsInventoryDetail(
                inventory_lot_id=unrelated_lot.id,
                owner_customer_id=customer_a.id,
                owner_customer_name_snapshot=customer_a.name,
                is_general=False,
                product_id=product_a.id,
                inventory_code_snapshot="TRACE-A",
                product_name_snapshot="追溯纸箱甲",
            )
        )
        order_a = Order(
            order_number="TMTRACE-A1",
            customer_id=customer_a.id,
            customer_po="SAME-PO",
            order_date=date(2026, 7, 29),
            status="pending_reconciliation",
            payment_status="unpaid",
            total_amount=Decimal("30"),
            created_at=base_time,
        )
        order_a_same_po = Order(
            order_number="TMTRACE-A2",
            customer_id=customer_a.id,
            customer_po="SAME-PO",
            order_date=date(2026, 7, 29),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("20"),
            created_at=base_time + timedelta(minutes=1),
        )
        order_b = Order(
            order_number="TMTRACE-B1",
            customer_id=customer_b.id,
            customer_po="SAME-PO",
            order_date=date(2026, 7, 29),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("10"),
            created_at=base_time + timedelta(minutes=2),
        )
        order_a_without_po = Order(
            order_number="TMTRACE-A-NO-PO-1",
            customer_id=customer_a.id,
            customer_po=None,
            order_date=date(2026, 7, 29),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("5"),
            created_at=base_time + timedelta(minutes=3),
        )
        order_a_without_po_other = Order(
            order_number="TMTRACE-A-NO-PO-2",
            customer_id=customer_a.id,
            customer_po="",
            order_date=date(2026, 7, 29),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("6"),
            created_at=base_time + timedelta(minutes=4),
        )
        db.add_all(
            [
                order_a,
                order_a_same_po,
                order_b,
                order_a_without_po,
                order_a_without_po_other,
            ]
        )
        db.flush()
        item_a = OrderItem(
            order_id=order_a.id,
            product_id=product_a.id,
            item_order_number="TMTRACE-A1-001",
            item_sequence=1,
            quantity=30,
            delivered_quantity=30,
            unit_price=Decimal("1"),
            subtotal=Decimal("30"),
            material_status="received",
            requisition_status="已入库",
            snapshot_product_code="TRACE-A",
            snapshot_product_name="追溯纸箱甲",
            snapshot_spec="300×200×100",
        )
        item_a_same_po = OrderItem(
            order_id=order_a_same_po.id,
            product_id=product_a.id,
            item_order_number="TMTRACE-A2-001",
            item_sequence=1,
            quantity=20,
            unit_price=Decimal("1"),
            subtotal=Decimal("20"),
            material_status="pending",
            requisition_status="未报料",
            snapshot_product_code="TRACE-A",
            snapshot_product_name="同客户同单号另一订单",
        )
        item_b = OrderItem(
            order_id=order_b.id,
            product_id=product_b.id,
            item_order_number="TMTRACE-B1-001",
            item_sequence=1,
            quantity=10,
            unit_price=Decimal("1"),
            subtotal=Decimal("10"),
            material_status="pending",
            requisition_status="未报料",
            snapshot_product_code="TRACE-B",
            snapshot_product_name="追溯纸箱乙",
        )
        item_a_without_po = OrderItem(
            order_id=order_a_without_po.id,
            product_id=product_a.id,
            item_order_number="TMTRACE-A-NO-PO-1-001",
            item_sequence=1,
            quantity=5,
            unit_price=Decimal("1"),
            subtotal=Decimal("5"),
            material_status="pending",
            requisition_status="未报料",
            snapshot_product_code="TRACE-NO-PO-1",
            snapshot_product_name="无客户单号订单一",
        )
        item_a_without_po_other = OrderItem(
            order_id=order_a_without_po_other.id,
            product_id=product_a.id,
            item_order_number="TMTRACE-A-NO-PO-2-001",
            item_sequence=1,
            quantity=6,
            unit_price=Decimal("1"),
            subtotal=Decimal("6"),
            material_status="pending",
            requisition_status="未报料",
            snapshot_product_code="TRACE-NO-PO-2",
            snapshot_product_name="无客户单号订单二",
        )
        db.add_all(
            [
                item_a,
                item_a_same_po,
                item_b,
                item_a_without_po,
                item_a_without_po_other,
            ]
        )
        db.flush()
        requisition = Requisition(
            requisition_number="REQ-TRACE-A",
            requisition_date=date(2026, 7, 29),
            supplier_name="鸣朋",
            status="已报料",
            created_at=base_time + timedelta(hours=1),
        )
        db.add(requisition)
        db.flush()
        db.add(
            RequisitionItem(
                requisition_id=requisition.id,
                order_item_id=item_a.id,
                requisition_qty=30,
                cardboard_len=Decimal("400"),
                cardboard_width=Decimal("300"),
                product_code_snapshot="TRACE-A",
                product_name_snapshot="追溯纸箱甲",
                specification_snapshot="300×200×100",
                status="有效",
            )
        )
        delivery = Delivery(
            delivery_number="TH000001",
            customer_id=customer_a.id,
            delivery_date=date(2026, 7, 29),
            status="dispatched",
            total_quantity=30,
            created_at=base_time + timedelta(hours=2),
            dispatched_at=base_time + timedelta(hours=3),
            ever_dispatched_at=base_time + timedelta(hours=3),
        )
        db.add(delivery)
        db.flush()
        delivery_item = DeliveryItem(
            delivery_id=delivery.id,
            order_item_id=item_a.id,
            delivered_quantity=30,
            ordered_quantity_snapshot=30,
            order_remaining_snapshot=30,
            created_at=base_time + timedelta(hours=2),
        )
        db.add(delivery_item)
        db.flush()
        receipt = ReturnReceipt(
            delivery_id=delivery.id,
            actual_received_date=date(2026, 7, 30),
            status="confirmed",
            created_at=base_time + timedelta(days=1),
        )
        db.add(receipt)
        db.flush()
        receipt_item = ReturnReceiptItem(
            return_receipt_id=receipt.id,
            delivery_item_id=delivery_item.id,
            actual_received_quantity=30,
            created_at=base_time + timedelta(days=1),
        )
        db.add(receipt_item)
        db.flush()
        statement = Statement(
            statement_number="ST-TRACE-001",
            customer_id=customer_a.id,
            statement_month="2026-07",
            total_receivable=Decimal("30"),
            status="settled",
            created_at=base_time + timedelta(days=2),
        )
        db.add(statement)
        db.flush()
        db.add(
            StatementItem(
                statement_id=statement.id,
                return_receipt_item_id=receipt_item.id,
                actual_received_quantity=30,
                unit_price_snapshot=Decimal("1"),
                unit_cost_snapshot=Decimal("0.5"),
                receivable_amount=Decimal("30"),
                gross_profit_amount=Decimal("15"),
                created_at=base_time + timedelta(days=2),
            )
        )
        db.add_all(
            [
                Invoice(
                    statement_id=statement.id,
                    invoice_number="INV-TRACE-001",
                    invoice_date=date(2026, 8, 1),
                    invoice_amount=Decimal("30"),
                    created_at=base_time + timedelta(days=3),
                ),
                SettlementRecord(
                    statement_id=statement.id,
                    settled_amount=Decimal("30"),
                    settlement_date=date(2026, 8, 2),
                    account="测试账户",
                    created_at=base_time + timedelta(days=4),
                ),
                UserCustomerScope(user_id=sales.id, customer_id=customer_a.id),
            ]
        )
        db.commit()
        ids = {
            "customer_a": customer_a.id,
            "customer_b": customer_b.id,
            "order_a": order_a.id,
            "item_a": item_a.id,
            "order_a_same_po": order_a_same_po.id,
            "item_a_same_po": item_a_same_po.id,
            "order_b": order_b.id,
            "item_b": item_b.id,
            "order_a_without_po": order_a_without_po.id,
            "item_a_without_po": item_a_without_po.id,
            "order_a_without_po_other": order_a_without_po_other.id,
            "item_a_without_po_other": item_a_without_po_other.id,
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(orders_router, prefix="/api/orders")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    app.state.trace_session_factory = factory
    return app, ids


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "RolePass123!"},
    )
    assert response.status_code == 200, response.text


def test_exact_item_trace_does_not_mix_same_customer_po(order_trace_app) -> None:
    app, ids = order_trace_app
    with TestClient(app) as client:
        _login(client, "trace-admin")
        response = client.get(
            f"/api/orders/{ids['order_a']}/items/{ids['item_a']}/documents"
        )
        empty_response = client.get(
            f"/api/orders/{ids['order_a_same_po']}/items/{ids['item_a_same_po']}/documents"
        )

    assert response.status_code == 200, response.text
    body = response.json()
    document_numbers = {event["document_number"] for event in body["events"]}
    assert "REQ-TRACE-A" in document_numbers
    assert "TH000001" in document_numbers
    assert "ST-TRACE-001" in document_numbers
    assert "INV-TRACE-001" in document_numbers
    assert body["item"]["id"] == ids["item_a"]
    assert body["current_inventory"] == []
    assert body["current_event_key"] in {
        event["key"] for event in body["events"] if event["is_effective"]
    }
    assert all(event["target"]["source_id"] == event["source_id"] for event in body["events"])
    assert not any("unit_cost" in str(event) for event in body["events"])

    assert empty_response.status_code == 200, empty_response.text
    empty_body = empty_response.json()
    assert len(empty_body["events"]) == 1
    assert empty_body["events"][0]["stage"] == "order"
    assert "REQ-TRACE-A" not in {
        event["document_number"] for event in empty_body["events"]
    }


def test_trace_maps_only_current_exact_completion_lots_and_split_descendants(
    order_trace_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib.parse import parse_qs, urlsplit

    from sqlalchemy import func, select

    import app.services.order_document_trace as trace_service
    from app.models.customer import Customer
    from app.models.order import OrderItem
    from app.models.production import (
        ProductionCompletion,
        ProductionCompletionBatch,
        ProductionTask,
    )
    from app.models.user import User
    from app.models.warehouse_inventory import (
        FinishedGoodsInventoryDetail,
        InventoryLot,
        InventoryLotTransfer,
        InventoryPallet,
        InventoryPalletItem,
        WarehouseArea,
        WarehouseFloor,
        WarehouseLocation,
    )

    app, ids = order_trace_app
    factory = app.state.trace_session_factory
    completed_at = datetime(2026, 7, 29, 8, 0, 0)
    with factory() as db:
        item = db.get(OrderItem, ids["item_a"])
        assert item is not None
        customer = db.get(Customer, ids["customer_a"])
        assert customer is not None
        other_customer = db.get(Customer, ids["customer_b"])
        assert other_customer is not None
        other_item = db.get(OrderItem, ids["item_b"])
        assert other_item is not None
        admin = db.scalar(select(User).where(User.username == "trace-admin"))
        assert admin is not None

        floor = WarehouseFloor(
            floor_code="3F",
            floor_name="三楼成品仓库",
            floor_number=3,
            construction_status="enabled",
        )
        db.add(floor)
        db.flush()
        area = WarehouseArea(
            floor_id=floor.id,
            area_code="C",
            area_name="三楼成品区",
            construction_status="enabled",
        )
        db.add(area)
        db.flush()

        locations: list[WarehouseLocation] = []
        for index, active in enumerate(
            (True, True, True, True, True, True, True),
            start=1,
        ):
            location = WarehouseLocation(
                location_code=f"C3-L{index:02d}",
                location_name=f"三楼成品区第{index}位",
                warehouse_type="finished",
                warehouse_floor=3,
                area_code="C",
                address_area_id=area.id,
                storage_type="ground",
                placement_status="placed",
                is_active=active,
                sort_order=index,
            )
            locations.append(location)
            db.add(location)
        locations[4].is_active = False
        db.flush()

        task = ProductionTask(
            order_item_id=item.id,
            task_role="order_main",
            status="completed",
            planned_quantity=39,
            ordered_quantity_snapshot=39,
            material_received_quantity=39,
            material_input_quantity=39,
            finished_coverage_snapshot=39,
            ready_at=completed_at - timedelta(hours=1),
        )
        batch = ProductionCompletionBatch(
            idempotency_key="p1-146-trace-batch",
            request_hash="1" * 64,
            item_count=1,
            completed_by=admin.id,
            completed_at=completed_at,
        )
        db.add_all([task, batch])
        db.flush()
        completion = ProductionCompletion(
            batch_id=batch.id,
            task_id=task.id,
            order_item_id=item.id,
            expected_version=task.version,
            quantity=39,
            material_input_quantity=39,
            planned_output_quantity=39,
            actual_output_quantity=39,
            defective_quantity=0,
            order_reserved_quantity=30,
            direct_delivery_quantity=0,
            stock_quantity=39,
            surplus_finished_quantity=9,
            initial_disposition="stock",
            warehouse_location_id=locations[0].id,
            status="posted",
            completed_by=admin.id,
            completed_at=completed_at,
        )
        db.add(completion)
        db.flush()

        lot_specs = (
            ("LOT-TRACE-ROOT", locations[0], 9, 0, "production_completion"),
            ("LOT-TRACE-SPLIT", locations[1], 8, 0, "transfer"),
            ("LOT-TRACE-MISMATCH", locations[2], 12, 0, "transfer"),
            ("LOT-TRACE-DRAINED", locations[3], 0, 1, "transfer"),
            ("LOT-TRACE-DISABLED", locations[4], 2, 0, "transfer"),
            ("LOT-TRACE-UNMAPPED", locations[5], 3, 0, "transfer"),
            ("LOT-TRACE-CROSS-CUSTOMER", locations[6], 4, 0, "transfer"),
        )
        lots: list[InventoryLot] = []
        for lot_number, location, available, consumed, source_type in lot_specs:
            lot = InventoryLot(
                lot_number=lot_number,
                inventory_type="finished",
                warehouse_location_id=location.id,
                quantity_available=available,
                quantity_reserved=0,
                quantity_consumed=consumed,
                quantity_damaged=0,
                quantity_scrapped=0,
                unit="boxes",
                status="active",
                source_type=source_type,
                source_ref_type=(
                    "production_completion" if source_type == "production_completion" else None
                ),
                source_ref_id=(completion.id if source_type == "production_completion" else None),
                stock_date=date(2026, 7, 29),
                stock_date_accuracy="exact",
                last_movement_at=completed_at,
                created_by=admin.id,
            )
            lots.append(lot)
            db.add(lot)
        db.flush()
        completion.inventory_lot_id = lots[0].id
        for lot in lots:
            is_cross_customer = lot.lot_number == "LOT-TRACE-CROSS-CUSTOMER"
            db.add(
                FinishedGoodsInventoryDetail(
                    inventory_lot_id=lot.id,
                    owner_customer_id=(other_customer.id if is_cross_customer else customer.id),
                    owner_customer_name_snapshot=(
                        other_customer.name if is_cross_customer else customer.name
                    ),
                    is_general=False,
                    product_id=(other_item.product_id if is_cross_customer else item.product_id),
                    inventory_code_snapshot=("TRACE-B" if is_cross_customer else "TRACE-A"),
                    product_name_snapshot=("追溯纸箱乙" if is_cross_customer else "追溯纸箱甲"),
                )
            )

        for index, (target_lot, quantity) in enumerate(
            zip(lots[1:], (8, 12, 1, 2, 3, 4), strict=True),
            start=1,
        ):
            db.add(
                InventoryLotTransfer(
                    source_lot_id=lots[0].id,
                    target_lot_id=target_lot.id,
                    source_location_id=locations[0].id,
                    target_location_id=target_lot.warehouse_location_id,
                    quantity=quantity,
                    available_quantity=quantity,
                    reserved_quantity=0,
                    source_version_before=index,
                    source_version_after=index + 1,
                    idempotency_key=f"p1-146-transfer-{index}",
                    request_hash=str(index) * 64,
                    transferred_by=admin.id,
                    transferred_at=completed_at + timedelta(minutes=index),
                )
            )

        pallet_locations = (
            locations[0],
            locations[1],
            locations[3],  # LOT-TRACE-MISMATCH is deliberately bound elsewhere.
            locations[4],
            locations[5],
            locations[6],
        )
        pallet_lots = (lots[0], lots[1], lots[2], lots[4], lots[5], lots[6])
        for index, (pallet_location, lot) in enumerate(
            zip(pallet_locations, pallet_lots, strict=True),
            start=1,
        ):
            pallet = InventoryPallet(
                pallet_code=f"PLT-P1-146-{index}",
                location_id=pallet_location.id,
                status="active",
                is_current=True,
                created_by=admin.id,
            )
            db.add(pallet)
            db.flush()
            db.add(
                InventoryPalletItem(
                    pallet_id=pallet.id,
                    inventory_lot_id=lot.id,
                    customer_id=(
                        other_customer.id
                        if lot.lot_number == "LOT-TRACE-CROSS-CUSTOMER"
                        else customer.id
                    ),
                    product_id=(
                        other_item.product_id
                        if lot.lot_number == "LOT-TRACE-CROSS-CUSTOMER"
                        else item.product_id
                    ),
                    inventory_code=(
                        "TRACE-B"
                        if lot.lot_number == "LOT-TRACE-CROSS-CUSTOMER"
                        else "TRACE-A"
                    ),
                    order_no="TMTRACE-A1",
                    customer_name_snapshot=(
                        other_customer.name
                        if lot.lot_number == "LOT-TRACE-CROSS-CUSTOMER"
                        else customer.name
                    ),
                    product_name=(
                        "追溯纸箱乙"
                        if lot.lot_number == "LOT-TRACE-CROSS-CUSTOMER"
                        else "追溯纸箱甲"
                    ),
                    item_type="finished",
                    quantity=max(lot.quantity_available, 1),
                    unit="boxes",
                    match_status="matched",
                    created_by=admin.id,
                )
            )
        db.commit()
        related_ids = [lot.id for lot in lots]
        before_lots = {
            lot.id: (
                lot.quantity_available,
                lot.quantity_reserved,
                lot.quantity_consumed,
                lot.warehouse_location_id,
                lot.version,
            )
            for lot in lots
        }
        before_transfer_count = db.scalar(select(func.count(InventoryLotTransfer.id)))
        unmapped_location_id = locations[5].id

    monkeypatch.setattr(
        trace_service,
        "warehouse_location_projection",
        lambda location, **context: (
            {
                "position_status": "unplaced",
                "map_issue": "该库位尚未发布到当前实测地图",
            }
            if location.id == unmapped_location_id
            else {"position_status": "mapped", "map_issue": None}
        ),
    )

    with TestClient(app) as client:
        _login(client, "trace-admin")
        response = client.get(
            f"/api/orders/{ids['order_a']}/items/{ids['item_a']}/documents"
        )
        repeated_response = client.get(
            f"/api/orders/{ids['order_a']}/items/{ids['item_a']}/documents"
        )
        _login(client, "trace-sales")
        restricted_response = client.get(
            f"/api/orders/{ids['order_a']}/items/{ids['item_a']}/documents"
        )

    assert response.status_code == 200, response.text
    assert repeated_response.status_code == 200, repeated_response.text
    assert repeated_response.json()["current_inventory"] == response.json()[
        "current_inventory"
    ]
    inventory_rows = {
        row["lot_number"]: row for row in response.json()["current_inventory"]
    }
    assert set(inventory_rows) == {
        lot_number
        for lot_number, *_ in lot_specs
        if lot_number != "LOT-TRACE-CROSS-CUSTOMER"
    }
    assert "LOT-UNRELATED-SAME-PRODUCT" not in inventory_rows
    assert "LOT-TRACE-CROSS-CUSTOMER" not in inventory_rows

    for lot_number, expected_location in (
        ("LOT-TRACE-ROOT", locations[0].id),
        ("LOT-TRACE-SPLIT", locations[1].id),
    ):
        row = inventory_rows[lot_number]
        assert row["map_position_status"] == "mapped"
        assert row["map_deep_link"] is not None
        parsed = urlsplit(row["map_deep_link"])
        params = parse_qs(parsed.query)
        assert parsed.path == "/warehouse.html"
        assert params == {
            "floor": ["3F"],
            "view": ["2d"],
            "mode": ["lookup"],
            "readonly": ["1"],
            "source": ["order_trace"],
            "area_code": ["C"],
            "location_id": [str(expected_location)],
            "lot_id": [str(row["lot_id"])],
        }

    assert inventory_rows["LOT-TRACE-DRAINED"]["map_deep_link"] is None
    assert "已清零" in inventory_rows["LOT-TRACE-DRAINED"]["location_issue"]
    assert inventory_rows["LOT-TRACE-MISMATCH"]["map_deep_link"] is None
    assert "不一致" in inventory_rows["LOT-TRACE-MISMATCH"]["location_issue"]
    assert inventory_rows["LOT-TRACE-DISABLED"]["map_deep_link"] is None
    assert "已停用" in inventory_rows["LOT-TRACE-DISABLED"]["location_issue"]
    assert inventory_rows["LOT-TRACE-UNMAPPED"]["map_deep_link"] is None
    assert "尚未发布" in inventory_rows["LOT-TRACE-UNMAPPED"]["location_issue"]

    assert restricted_response.status_code == 200, restricted_response.text
    assert restricted_response.json()["current_inventory"] == []
    assert "inventory" in {
        row["stage"] for row in restricted_response.json()["restricted_stages"]
    }

    with factory() as db:
        after_lots = {
            lot.id: (
                lot.quantity_available,
                lot.quantity_reserved,
                lot.quantity_consumed,
                lot.warehouse_location_id,
                lot.version,
            )
            for lot in db.scalars(
                select(InventoryLot).where(InventoryLot.id.in_(related_ids))
            ).all()
        }
        after_transfer_count = db.scalar(select(func.count(InventoryLotTransfer.id)))
    assert after_lots == before_lots
    assert after_transfer_count == before_transfer_count


def test_scope_and_stage_permissions_are_enforced(order_trace_app) -> None:
    app, ids = order_trace_app
    with TestClient(app) as client:
        _login(client, "trace-sales")
        allowed = client.get(
            f"/api/orders/{ids['order_a']}/items/{ids['item_a']}/documents"
        )
        denied = client.get(
            f"/api/orders/{ids['order_b']}/items/{ids['item_b']}/documents"
        )

    assert allowed.status_code == 200, allowed.text
    body = allowed.json()
    assert {row["stage"] for row in body["restricted_stages"]} >= {
        "requisition",
        "incoming",
        "inventory",
        "delivery",
        "statement",
        "invoice",
        "settlement",
    }
    assert {event["stage"] for event in body["events"]} == {"order"}
    assert denied.status_code == 403


def test_exact_stage_detail_revalidates_order_item_and_source(order_trace_app) -> None:
    app, ids = order_trace_app
    with TestClient(app) as client:
        _login(client, "trace-admin")
        trace_response = client.get(
            f"/api/orders/{ids['order_a']}/items/{ids['item_a']}/documents"
        )
        assert trace_response.status_code == 200, trace_response.text
        trace = trace_response.json()
        requisition_event = next(
            event
            for event in trace["events"]
            if event["source_type"] == "material_requisition"
        )

        exact = client.get(
            f"/api/orders/{ids['order_a']}/items/{ids['item_a']}"
            f"/documents/{requisition_event['source_type']}"
            f"/{requisition_event['source_id']}"
        )
        wrong_sibling = client.get(
            f"/api/orders/{ids['order_a_same_po']}"
            f"/items/{ids['item_a_same_po']}"
            f"/documents/{requisition_event['source_type']}"
            f"/{requisition_event['source_id']}"
        )

    assert exact.status_code == 200, exact.text
    body = exact.json()
    assert body["navigation"] == {
        "order_id": ids["order_a"],
        "item_id": ids["item_a"],
        "source_type": requisition_event["source_type"],
        "source_id": requisition_event["source_id"],
    }
    assert body["event"]["document_number"] == "REQ-TRACE-A"
    assert body["target"]["module"] == "requisition"
    assert wrong_sibling.status_code == 404


def test_exact_stage_detail_cannot_bypass_stage_permissions(order_trace_app) -> None:
    app, ids = order_trace_app
    with TestClient(app) as client:
        _login(client, "trace-admin")
        admin_trace = client.get(
            f"/api/orders/{ids['order_a']}/items/{ids['item_a']}/documents"
        ).json()
        delivery_event = next(
            event
            for event in admin_trace["events"]
            if event["source_type"] == "delivery_dispatch"
        )

        client.post("/api/auth/logout")
        _login(client, "trace-sales")
        response = client.get(
            f"/api/orders/{ids['order_a']}/items/{ids['item_a']}"
            f"/documents/{delivery_event['source_type']}"
            f"/{delivery_event['source_id']}"
        )

    assert response.status_code == 404


def test_trace_targets_production_completion_to_completion_history() -> None:
    from app.services.order_document_trace import trace_event_target

    assert trace_event_target(
        stage="production",
        source_type="production_completion",
        source_id=57,
    ) == {
        "module": "production",
        "module_label": "生产确认",
        "section": "history",
        "section_label": "完工历史",
        "source_type": "production_completion",
        "source_id": 57,
    }


def test_item_must_belong_to_order(order_trace_app) -> None:
    app, ids = order_trace_app
    with TestClient(app) as client:
        _login(client, "trace-admin")
        response = client.get(
            f"/api/orders/{ids['order_a']}/items/{ids['item_b']}/documents"
        )
    assert response.status_code == 404


def test_group_detail_returns_all_same_customer_po_orders_across_pages(
    order_trace_app,
) -> None:
    app, ids = order_trace_app
    with TestClient(app) as client:
        _login(client, "trace-admin")
        response = client.get(
            "/api/orders/group-detail",
            params={
                "customer_id": ids["customer_a"],
                "customer_po": "SAME-PO",
                "anchor_order_id": ids["order_a"],
                "scope": "all",
            },
        )
        first_page = client.get(
            "/api/orders",
            params={
                "customer_id": ids["customer_a"],
                "customer_po": "SAME-PO",
                "scope": "all",
                "page": 1,
                "page_size": 1,
            },
        )
        second_page = client.get(
            "/api/orders",
            params={
                "customer_id": ids["customer_a"],
                "customer_po": "SAME-PO",
                "scope": "all",
                "page": 2,
                "page_size": 1,
            },
        )

    assert response.status_code == 200, response.text
    assert first_page.status_code == 200, first_page.text
    assert second_page.status_code == 200, second_page.text
    assert first_page.json()["total"] == second_page.json()["total"] == 2
    assert {
        first_page.json()["items"][0]["id"],
        second_page.json()["items"][0]["id"],
    } == {ids["order_a"], ids["order_a_same_po"]}
    body = response.json()
    assert {row["id"] for row in body["orders"]} == {
        ids["order_a"],
        ids["order_a_same_po"],
    }
    assert {
        item["snapshot_product_code"]
        for row in body["orders"]
        for item in row["items"]
    } == {"TRACE-A"}
    assert ids["order_b"] not in {row["id"] for row in body["orders"]}


def test_group_detail_without_customer_po_is_exactly_one_anchor_order(
    order_trace_app,
) -> None:
    app, ids = order_trace_app
    with TestClient(app) as client:
        _login(client, "trace-admin")
        response = client.get(
            "/api/orders/group-detail",
            params={
                "customer_id": ids["customer_a"],
                "anchor_order_id": ids["order_a_without_po"],
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [row["id"] for row in body["orders"]] == [ids["order_a_without_po"]]
    assert body["orders"][0]["items"][0]["snapshot_product_code"] == "TRACE-NO-PO-1"


def test_group_detail_respects_current_business_scope(order_trace_app) -> None:
    app, ids = order_trace_app
    with TestClient(app) as client:
        _login(client, "trace-admin")
        active = client.get(
            "/api/orders/group-detail",
            params={
                "customer_id": ids["customer_a"],
                "customer_po": "SAME-PO",
                "anchor_order_id": ids["order_a_same_po"],
                "scope": "active",
            },
        )
        stale_anchor = client.get(
            "/api/orders/group-detail",
            params={
                "customer_id": ids["customer_a"],
                "customer_po": "SAME-PO",
                "anchor_order_id": ids["order_a"],
                "scope": "active",
            },
        )

    assert active.status_code == 200, active.text
    assert [row["id"] for row in active.json()["orders"]] == [
        ids["order_a_same_po"]
    ]
    assert stale_anchor.status_code == 409


def test_group_detail_rechecks_customer_scope(order_trace_app) -> None:
    app, ids = order_trace_app
    with TestClient(app) as client:
        _login(client, "trace-sales")
        allowed = client.get(
            "/api/orders/group-detail",
            params={
                "customer_id": ids["customer_a"],
                "customer_po": "SAME-PO",
                "anchor_order_id": ids["order_a"],
                "scope": "all",
            },
        )
        denied = client.get(
            "/api/orders/group-detail",
            params={
                "customer_id": ids["customer_b"],
                "customer_po": "SAME-PO",
                "anchor_order_id": ids["order_b"],
            },
        )

    assert allowed.status_code == 200, allowed.text
    assert denied.status_code == 403
    assert "estimated_cost" not in str(allowed.json())
    assert "total_estimated_cost" not in str(allowed.json())


def test_group_detail_rejects_mismatched_anchor_identity(order_trace_app) -> None:
    app, ids = order_trace_app
    with TestClient(app) as client:
        _login(client, "trace-admin")
        wrong_customer = client.get(
            "/api/orders/group-detail",
            params={
                "customer_id": ids["customer_a"],
                "customer_po": "SAME-PO",
                "anchor_order_id": ids["order_b"],
                "scope": "all",
            },
        )
        wrong_po = client.get(
            "/api/orders/group-detail",
            params={
                "customer_id": ids["customer_a"],
                "customer_po": "OTHER-PO",
                "anchor_order_id": ids["order_a"],
                "scope": "all",
            },
        )

    assert wrong_customer.status_code == 404
    assert wrong_po.status_code == 409


@pytest.mark.parametrize(
    ("stage", "source_type", "status", "expected"),
    [
        ("order", "sales_order", "pending_production", "待生产"),
        ("order", "sales_order", "waiting_material", "待收料"),
        ("order", "sales_order", "production", "生产中"),
        ("order", "sales_order", "delivered", "已送完"),
        ("order", "sales_order", "pending_material", "待报料"),
        ("order", "sales_order", "pending_incoming", "待收料"),
        ("order", "sales_order", "pending_delivery", "待送货"),
        ("order", "sales_order", "partially_delivered", "部分送完"),
        ("order", "sales_order", "waiting_receipt", "待回单"),
        ("order", "sales_order", "pending_reconciliation", "待对账"),
        ("order", "sales_order", "pending_invoice", "待开票"),
        ("order", "sales_order", "pending_payment", "待结款"),
        ("order", "sales_order", "completed", "订单完成"),
        ("production", "production_task", "waiting_material", "待收料"),
        ("production", "production_task", "pending", "待生产"),
        ("production", "production_task", "completed", "已完成"),
        ("production", "production_task", "not_required", "无需生产"),
        ("inventory", "inventory_movement", "reserve", "预占库存"),
        ("inventory", "inventory_movement", "release_reserve", "释放预占"),
        ("inventory", "inventory_movement", "manual_in", "手工入库"),
        ("inventory", "inventory_movement", "adjust", "库存调整"),
        ("inventory", "inventory_movement", "freeze", "冻结"),
        ("inventory", "inventory_movement", "unfreeze", "解冻"),
        ("inventory", "inventory_movement", "damage", "报损"),
        ("inventory", "inventory_movement", "scrap", "报废"),
        ("inventory", "inventory_movement", "consume", "出库扣减"),
        ("inventory", "inventory_movement", "reverse_consume", "撤销出库"),
        ("inventory", "inventory_reservation", "active", "预占中"),
        ("inventory", "inventory_lot", "active", "正常在库"),
        ("inventory", "inventory_lot", "frozen", "已冻结"),
        ("delivery", "delivery_dispatch", "closed", "已关闭"),
        ("inventory", "inventory_lot", "future_status", "状态待确认"),
    ],
)
def test_trace_status_labels_are_contextual_chinese(
    stage: str,
    source_type: str,
    status: str,
    expected: str,
) -> None:
    from app.services.order_document_trace import _status_label

    assert (
        _status_label(status, stage=stage, source_type=source_type)
        == expected
    )
