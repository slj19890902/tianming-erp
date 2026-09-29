from __future__ import annotations

import json
import hashlib
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryItem
from app.models.finance import ReturnReceipt, ReturnReceiptItem
from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
from app.models.material import Material
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.product_bom import SalesOrderItemBomComponent
from app.models.production import (
    ProductionCompletion,
    ProductionCompletionBatch,
    ProductionTask,
)
from app.models.purchase_receipt import (
    IncomingReceiptPurposeAllocation,
    IncomingReceiptPurposeReversal,
    PurchaseReceiptFact,
)
from app.models.requisition import Requisition, RequisitionItem
from app.models.supplier_requisition_order import (
    PurchasePurposeSourceSnapshot,
    SupplierRequisitionOrder,
    SupplierRequisitionOrderItem,
)
from app.models.user import User
from app.models.warehouse_inventory import (
    DeliveryInventoryAllocation,
    FinishedGoodsInventoryDetail,
    InventoryLot,
    InventoryLotTransfer,
    InventoryMovement,
    InventoryReservation,
    WarehouseLocation,
)
from app.services.incomplete_order_chain_audit import audit_incomplete_order_chains


REVISION = "vv30v8x9z19"
ANONYMIZATION_KEY = b"p0-15-anonymous-sqlite-contract-key"
NOW = datetime(2026, 8, 20, 9, 30, 0)
TODAY = date(2026, 8, 20)


def build_p0_15_database(path: Path):
    engine = create_sqlite_engine(path)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE alembic_version "
                "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
            )
        )
        connection.execute(
            text("INSERT INTO alembic_version(version_num) VALUES (:revision)"),
            {"revision": REVISION},
        )
    return engine


def add_order(
    db: Session,
    token: str,
    *,
    status: str = "pending_production",
    material_status: str = "pending",
    delivered_quantity: int = 0,
    quantity: int = 10,
) -> tuple[Customer, Product, Order, OrderItem]:
    customer = Customer(
        name=f"匿名客户-{token}",
        chinese_short_name=f"客{token}",
    )
    db.add(customer)
    db.flush()
    product = Product(
        customer_id=customer.id,
        product_code=f"ANON-PRODUCT-{token}",
        customer_material_code=f"ANON-MATERIAL-{token}",
        product_name=f"匿名纸箱-{token}",
        pieces_per_box=1,
    )
    db.add(product)
    db.flush()
    order = Order(
        order_number=f"ANON-ORDER-{token}",
        customer_id=customer.id,
        customer_po=f"ANON-PO-{token}",
        order_date=TODAY,
        status=status,
    )
    db.add(order)
    db.flush()
    item = OrderItem(
        order_id=order.id,
        product_id=product.id,
        item_order_number=f"ANON-ITEM-{token}",
        quantity=quantity,
        delivered_quantity=delivered_quantity,
        unit_price=Decimal("1.0000"),
        subtotal=Decimal(quantity),
        material_status=material_status,
        snapshot_product_name=product.product_name,
        snapshot_product_code=product.product_code,
    )
    db.add(item)
    db.flush()
    return customer, product, order, item


def add_task(db: Session, item: OrderItem, *, status: str = "pending") -> ProductionTask:
    task = ProductionTask(
        order_item_id=item.id,
        status=status,
        planned_quantity=item.quantity,
        ordered_quantity_snapshot=item.quantity,
        material_received_quantity=item.quantity,
    )
    db.add(task)
    db.flush()
    return task


def add_traced_receipt(
    db: Session,
    token: str,
    order: Order,
    item: OrderItem,
    *,
    quantity: int | None = None,
) -> IncomingReceiptItem:
    received = int(quantity or item.quantity)
    requisition = Requisition(
        requisition_number=f"ANON-REQ-{token}",
        requisition_date=TODAY,
    )
    db.add(requisition)
    db.flush()
    requisition_item = RequisitionItem(
        requisition_id=requisition.id,
        order_item_id=item.id,
        requisition_qty=item.quantity,
        cardboard_len=Decimal("800"),
        cardboard_width=Decimal("600"),
        product_name_snapshot=item.snapshot_product_name,
    )
    db.add(requisition_item)
    supplier_order = SupplierRequisitionOrder(
        order_number=f"ANON-SUPPLIER-ORDER-{token}",
        total_quantity=item.quantity,
        requisition_qty=item.quantity,
    )
    db.add(supplier_order)
    db.flush()
    supplier_item = SupplierRequisitionOrderItem(
        supplier_order_id=supplier_order.id,
        order_item_id=item.id,
        product_id=item.product_id,
        quantity=item.quantity,
        requisition_qty=item.quantity,
    )
    db.add(supplier_item)
    db.flush()
    receipt = IncomingReceipt(
        receipt_number=f"ANON-RECEIPT-{token}",
        received_at=NOW,
        idempotency_key=f"anon-receipt-{token}",
    )
    db.add(receipt)
    db.flush()
    receipt_item = IncomingReceiptItem(
        receipt_id=receipt.id,
        order_id=order.id,
        order_item_id=item.id,
        requisition_id=requisition.id,
        requisition_item_id=requisition_item.id,
        supplier_order_id=supplier_order.id,
        supplier_order_item_id=supplier_item.id,
        planned_quantity=item.quantity,
        received_quantity=received,
        cumulative_received_quantity=received,
        variance_quantity=received - item.quantity,
        variance_type=(
            "matched" if received == item.quantity else "over" if received > item.quantity else "short"
        ),
        resolution_status="not_required" if received == item.quantity else "resolved",
        resolution_action=None if received == item.quantity else "all_to_production",
    )
    db.add(receipt_item)
    item.material_status = "received"
    db.flush()
    return receipt_item


def add_location(db: Session, token: str) -> WarehouseLocation:
    location = WarehouseLocation(
        location_code=f"ANON-LOC-{token}",
        location_name=f"匿名暂存位-{token}",
        warehouse_type="finished",
    )
    db.add(location)
    db.flush()
    return location


def add_lot(
    db: Session,
    token: str,
    customer: Customer,
    product: Product,
    *,
    source_ref_id: int | None = None,
    quantity_available: int = 10,
    quantity_reserved: int = 0,
    detail_product: Product | None = None,
    location: WarehouseLocation | None = None,
) -> InventoryLot:
    location = location or add_location(db, token)
    lot = InventoryLot(
        lot_number=f"ANON-LOT-{token}",
        inventory_type="finished",
        warehouse_location_id=location.id,
        quantity_available=quantity_available,
        quantity_reserved=quantity_reserved,
        unit="boxes",
        source_type="production_completion" if source_ref_id is not None else "manual",
        source_ref_type="production_completion" if source_ref_id is not None else None,
        source_ref_id=source_ref_id,
        stock_date=TODAY,
        last_movement_at=NOW,
    )
    db.add(lot)
    db.flush()
    detail_product = detail_product or product
    db.add(
        FinishedGoodsInventoryDetail(
            inventory_lot_id=lot.id,
            owner_customer_id=customer.id,
            owner_customer_name_snapshot=customer.name,
            product_id=detail_product.id,
            inventory_code_snapshot=detail_product.product_code,
            product_name_snapshot=detail_product.product_name,
        )
    )
    db.flush()
    return lot


def add_movement(
    db: Session,
    token: str,
    lot: InventoryLot,
    movement_type: str,
    quantity: int,
    *,
    order: Order | None = None,
    item: OrderItem | None = None,
    delivery: Delivery | None = None,
    reservation: InventoryReservation | None = None,
) -> InventoryMovement:
    movement = InventoryMovement(
        movement_number=f"ANON-MOVE-{token}",
        inventory_lot_id=lot.id,
        movement_type=movement_type,
        quantity=quantity,
        unit="boxes",
        before_available=0,
        after_available=quantity,
        before_reserved=0,
        after_reserved=0,
        before_consumed=0,
        after_consumed=quantity if movement_type == "consume" else 0,
        before_damaged=0,
        after_damaged=0,
        before_scrapped=0,
        after_scrapped=0,
        reservation_id=reservation.id if reservation else None,
        related_order_id=order.id if order else None,
        related_order_item_id=item.id if item else None,
        related_delivery_id=delivery.id if delivery else None,
    )
    db.add(movement)
    db.flush()
    return movement


def add_location_transfer(
    db: Session,
    token: str,
    source: InventoryLot,
    target: InventoryLot,
    *,
    source_location: WarehouseLocation,
    target_location: WarehouseLocation,
    quantity: int,
    available_quantity: int,
    reserved_quantity: int,
) -> InventoryLotTransfer:
    """Build the ledger pair emitted by the finished-lot transfer service."""
    assert quantity == available_quantity + reserved_quantity
    key = f"anon-transfer-{token}"
    source_move = add_movement(db, f"{token}-SOURCE", source, "location_transfer", quantity)
    source_move.idempotency_key = f"location-transfer:{key}:source"
    source_move.before_available = (
        source.quantity_available
        if target.id == source.id
        else source.quantity_available + available_quantity
    )
    source_move.after_available = source.quantity_available
    source_move.before_reserved = (
        source.quantity_reserved
        if target.id == source.id
        else source.quantity_reserved + reserved_quantity
    )
    source_move.after_reserved = source.quantity_reserved
    if target.id != source.id:
        target_move = add_movement(db, f"{token}-TARGET", target, "location_transfer", quantity)
        target_move.idempotency_key = f"location-transfer:{key}:target"
        target_move.before_available = target.quantity_available - available_quantity
        target_move.after_available = target.quantity_available
        target_move.before_reserved = target.quantity_reserved - reserved_quantity
        target_move.after_reserved = target.quantity_reserved
    transfer = InventoryLotTransfer(
        source_lot_id=source.id,
        target_lot_id=target.id,
        source_location_id=source_location.id,
        target_location_id=target_location.id,
        quantity=quantity,
        available_quantity=available_quantity,
        reserved_quantity=reserved_quantity,
        source_version_before=1,
        source_version_after=2,
        idempotency_key=key,
        request_hash=(token.encode("utf-8").hex() + "0" * 64)[:64],
        transferred_at=NOW,
    )
    db.add(transfer)
    db.flush()
    return transfer


def add_completion(
    db: Session,
    token: str,
    task: ProductionTask,
    item: OrderItem,
    *,
    location: WarehouseLocation | None = None,
) -> ProductionCompletion:
    batch = ProductionCompletionBatch(
        idempotency_key=f"anon-completion-{token}",
        request_hash=(token.encode("utf-8").hex() + "0" * 64)[:64],
        item_count=1,
        completed_at=NOW,
    )
    db.add(batch)
    db.flush()
    completion = ProductionCompletion(
        batch_id=batch.id,
        task_id=task.id,
        order_item_id=item.id,
        expected_version=1,
        quantity=item.quantity,
        initial_disposition="stock" if location else "direct",
        warehouse_location_id=location.id if location else None,
        completed_at=NOW,
    )
    db.add(completion)
    task.status = "completed"
    db.flush()
    return completion


def add_frozen_purpose_chain(
    db: Session,
    token: str,
    customer: Customer,
    product: Product,
    order: Order,
    item: OrderItem,
    *,
    order_sheet_qty: int,
    reserve_sheet_qty: int,
    finished_output_qty: int,
    with_allocation: bool = True,
) -> dict:
    purchase_qty = order_sheet_qty + reserve_sheet_qty
    task = add_task(db, item)
    receipt_item = add_traced_receipt(
        db,
        token,
        order,
        item,
        quantity=purchase_qty,
    )
    supplier_item = db.get(
        SupplierRequisitionOrderItem,
        receipt_item.supplier_order_item_id,
    )
    assert supplier_item is not None
    supplier_item.purpose_contract_status = "frozen"
    supplier_item.requisition_qty = purchase_qty

    user = User(
        username=f"anon-purpose-{token}",
        password_hash="not-used-by-audit",
        role="admin",
        real_name=f"匿名审计员-{token}",
        must_change_password=False,
    )
    material = Material(
        code=f"ANON-BOARD-{token}",
        supplier_name=f"匿名供应商-{token}",
        layer_count=3,
        flute_type="B",
        quote_price=Decimal("1.0000"),
        price_unit="元/张",
    )
    db.add_all([user, material])
    db.flush()
    supplier_item.material_id = material.id
    supplier_item.material_code_snapshot = material.code
    supplier_item.report_length_mm = 800
    supplier_item.report_width_mm = 600

    digest = (token.encode("utf-8").hex() + "0" * 64)[:64]
    snapshot = PurchasePurposeSourceSnapshot(
        snapshot_key=f"anon-purpose-snapshot-{token}",
        allocation_group_key=f"anon-purpose-group-{token}",
        supplier_requisition_order_item_id=supplier_item.id,
        source_kind="order_item",
        source_key=f"order_item:{item.id}:whole",
        source_order_item_id=item.id,
        customer_id=customer.id,
        customer_name_snapshot=customer.name,
        component_type="whole",
        source_finished_qty_snapshot=item.quantity,
        pieces_per_finished_snapshot=1,
        source_required_piece_qty_snapshot=purchase_qty,
        source_semi_reserved_piece_qty_snapshot=0,
        source_effective_piece_qty_snapshot=purchase_qty,
        yield_per_sheet_snapshot=1,
        group_effective_piece_qty_snapshot=purchase_qty,
        group_authoritative_order_sheet_qty_snapshot=max(purchase_qty, 1),
        purchase_sheet_qty=purchase_qty,
        order_purpose_sheet_qty=order_sheet_qty,
        reserve_purpose_sheet_qty=reserve_sheet_qty,
        calculation_rule_version="p1-80-v1",
        snapshot_version=1,
        preview_fingerprint=digest,
        request_hash=digest,
        created_by=user.id,
    )
    db.add(snapshot)
    db.flush()
    if not with_allocation:
        db.flush()
        return {
            "task": task,
            "receipt_item": receipt_item,
            "supplier_item": supplier_item,
            "snapshot": snapshot,
        }

    receipt_fact = PurchaseReceiptFact(
        supplier_requisition_order_item_id=supplier_item.id,
        purchase_purpose_source_snapshot_id=snapshot.id,
        source_key=snapshot.source_key,
        receipt_fact_version=1,
        expected_source_version=supplier_item.version,
        purpose_snapshot_version=snapshot.snapshot_version,
        receipt_plan_fingerprint=digest,
        actual_material_id=material.id,
        actual_material_code_snapshot=material.code,
        actual_material_version=material.version,
        actual_material_layer_count_snapshot=material.layer_count,
        actual_material_flute_type_snapshot=material.flute_type,
        actual_material_is_active_snapshot=True,
        actual_material_fingerprint=digest,
        expected_material_id=material.id,
        expected_material_code_snapshot=material.code,
        material_change_confirmed=False,
        unit_price=Decimal("1.000000"),
        currency="CNY",
        price_unit="per_sheet",
        tax_included=True,
        tax_rate=Decimal("0.000000"),
        idempotency_key=f"anon-receipt-fact-{token}",
        request_hash=digest,
        created_by=user.id,
    )
    db.add(receipt_fact)
    db.flush()

    completion = None
    finished_lot = None
    if finished_output_qty > 0:
        finished_location = add_location(db, f"PURPOSE-FINISHED-{token}")
        completion = add_completion(
            db,
            f"PURPOSE-{token}",
            task,
            item,
            location=finished_location,
        )
        completion.origin = "receipt_auto"
        completion.quantity = finished_output_qty
        completion.material_input_quantity = max(order_sheet_qty, 1)
        completion.planned_output_quantity = finished_output_qty
        completion.actual_output_quantity = finished_output_qty
        completion.order_reserved_quantity = min(finished_output_qty, item.quantity)
        completion.direct_delivery_quantity = 0
        completion.stock_quantity = finished_output_qty
        completion.surplus_finished_quantity = max(
            finished_output_qty - item.quantity,
            0,
        )
        finished_lot = add_lot(
            db,
            f"PURPOSE-FINISHED-{token}",
            customer,
            product,
            source_ref_id=completion.id,
            quantity_available=finished_output_qty,
            location=finished_location,
        )
        completion.inventory_lot_id = finished_lot.id
        add_movement(
            db,
            f"PURPOSE-FINISHED-{token}",
            finished_lot,
            "manual_in",
            finished_output_qty,
            order=order,
            item=item,
        )

    reserve_lot = None
    reserve_movement = None
    if reserve_sheet_qty > 0:
        reserve_location = add_location(db, f"PURPOSE-RESERVE-{token}")
        reserve_location.warehouse_type = "semi_finished"
        reserve_lot = InventoryLot(
            lot_number=f"ANON-RESERVE-LOT-{token}",
            inventory_type="semi_finished",
            warehouse_location_id=reserve_location.id,
            quantity_available=reserve_sheet_qty,
            unit="sheets",
            status="active",
            source_type="purchase_reserve",
            source_ref_type="purchase_purpose_source_snapshot",
            source_ref_id=snapshot.id,
            stock_date=TODAY,
            last_movement_at=NOW,
        )
        db.add(reserve_lot)
        db.flush()
        reserve_movement = add_movement(
            db,
            f"PURPOSE-RESERVE-{token}",
            reserve_lot,
            "manual_in",
            reserve_sheet_qty,
            order=order,
            item=item,
        )

    allocation = IncomingReceiptPurposeAllocation(
        incoming_receipt_item_id=receipt_item.id,
        purpose_contract_status_snapshot="frozen",
        surplus_disposition=(
            "semi_finished_reserve"
            if reserve_sheet_qty > 0
            else "not_applicable"
        ),
        purchase_purpose_source_snapshot_id=snapshot.id,
        purchase_receipt_fact_id=receipt_fact.id,
        supplier_requisition_order_item_id=supplier_item.id,
        source_kind=snapshot.source_kind,
        source_key=snapshot.source_key,
        source_order_item_id=item.id,
        customer_id=customer.id,
        customer_name_snapshot=customer.name,
        component_type="whole",
        order_purpose_plan_sheet_qty_snapshot=order_sheet_qty,
        reserve_purpose_plan_sheet_qty_snapshot=reserve_sheet_qty,
        receipt_total_sheet_qty=purchase_qty,
        receipt_order_purpose_sheet_qty=order_sheet_qty,
        receipt_reserve_purpose_sheet_qty=reserve_sheet_qty,
        cumulative_total_sheet_qty_before=0,
        cumulative_total_sheet_qty_after=purchase_qty,
        cumulative_order_purpose_sheet_qty_before=0,
        cumulative_order_purpose_sheet_qty_after=order_sheet_qty,
        cumulative_reserve_purpose_sheet_qty_before=0,
        cumulative_reserve_purpose_sheet_qty_after=reserve_sheet_qty,
        finished_output_qty_before=0,
        finished_output_qty_after=finished_output_qty,
        finished_output_qty_delta=finished_output_qty,
        sheet_cost=Decimal("1.000000"),
        order_purpose_cost=Decimal(order_sheet_qty),
        reserve_purpose_cost=Decimal(reserve_sheet_qty),
        total_cost=Decimal(purchase_qty),
        capitalized_cost=(
            Decimal(order_sheet_qty) if finished_output_qty > 0 else Decimal(0)
        ),
        production_completion_id=completion.id if completion is not None else None,
        finished_inventory_lot_id=finished_lot.id if finished_lot is not None else None,
        semi_finished_inventory_lot_id=reserve_lot.id if reserve_lot is not None else None,
        initial_semi_inventory_movement_id=(
            reserve_movement.id if reserve_movement is not None else None
        ),
        status="posted",
        version=1,
        request_hash=digest,
        created_by=user.id,
    )
    db.add(allocation)
    db.flush()
    return {
        "task": task,
        "receipt_item": receipt_item,
        "supplier_item": supplier_item,
        "snapshot": snapshot,
        "receipt_fact": receipt_fact,
        "user": user,
        "allocation": allocation,
        "completion": completion,
        "finished_lot": finished_lot,
        "reserve_lot": reserve_lot,
    }


def run_audit(db: Session, *, focus_terms: tuple[str, ...] = ()) -> dict:
    return audit_incomplete_order_chains(
        db,
        anonymization_key=ANONYMIZATION_KEY,
        focus_terms=focus_terms,
        generated_at=NOW,
    )


def finding_codes(report: dict) -> list[str]:
    return [finding["code"] for finding in report["findings"]]


def test_valid_receipt_waiting_for_manual_production_is_not_stock_error(tmp_path: Path):
    engine = build_p0_15_database(tmp_path / "normal.sqlite3")
    with Session(engine) as db:
        _, _, order, item = add_order(db, "NORMAL", material_status="received")
        add_task(db, item)
        add_traced_receipt(db, "NORMAL", order, item)
        db.commit()

        report = run_audit(db)

    codes = finding_codes(report)
    assert codes.count("P015_RECEIVED_AWAITING_MANUAL_PRODUCTION") == 1
    assert "P015_INCOMING_WITHOUT_PRODUCTION_TASK" not in codes
    assert "P015_COMPLETION_WITHOUT_ACTIVE_FINISHED_LOT" not in codes
    assert "P015_COMPLETION_WITHOUT_INVENTORY_MOVEMENT" not in codes
    assert "P015_FINISHED_STOCK_NOT_DELIVERABLE" not in codes


def test_missing_task_and_legacy_delivery_compatibility_are_distinct_contracts(
    tmp_path: Path,
):
    engine = build_p0_15_database(tmp_path / "input-facts.sqlite3")
    with Session(engine) as db:
        _, _, receipt_order, receipt_item = add_order(db, "MISSING-TASK")
        add_traced_receipt(db, "MISSING-TASK", receipt_order, receipt_item)
        add_order(db, "LEGACY-ONLY", material_status="received")
        db.commit()

        report = run_audit(db)

    codes = finding_codes(report)
    assert codes.count("P015_INCOMING_WITHOUT_PRODUCTION_TASK") == 1
    assert codes.count("P015_LEGACY_RECEIVED_STATUS_TRACE_GAP") == 1
    assert "P015_PENDING_PRODUCTION_WITHOUT_INPUT_FACT" not in codes
    assert "P015_RECEIVED_AWAITING_MANUAL_PRODUCTION" not in codes


def test_completion_requires_one_finished_lot_and_one_matching_initial_movement(tmp_path: Path):
    engine = build_p0_15_database(tmp_path / "completion.sqlite3")
    with Session(engine) as db:
        customer, product, order, item = add_order(db, "NO-LOT", status="pending_delivery")
        task = add_task(db, item)
        add_traced_receipt(db, "NO-LOT", order, item)
        add_completion(db, "NO-LOT", task, item)

        customer2, product2, order2, item2 = add_order(db, "NO-MOVE", status="pending_delivery")
        task2 = add_task(db, item2)
        add_traced_receipt(db, "NO-MOVE", order2, item2)
        location2 = add_location(db, "NO-MOVE-COMPLETION")
        completion2 = add_completion(db, "NO-MOVE", task2, item2, location=location2)
        lot2 = add_lot(db, "NO-MOVE", customer2, product2, quantity_available=item2.quantity)
        lot2.source_type = "production_completion"
        lot2.source_ref_type = "production_completion"
        lot2.source_ref_id = completion2.id
        completion2.inventory_lot_id = lot2.id

        customer3, product3, order3, item3 = add_order(db, "DUPLICATE", status="pending_delivery")
        task3 = add_task(db, item3)
        add_traced_receipt(db, "DUPLICATE", order3, item3)
        location3 = add_location(db, "DUPLICATE-COMPLETION")
        completion3 = add_completion(db, "DUPLICATE", task3, item3, location=location3)
        lot3a = add_lot(db, "DUPLICATE-A", customer3, product3, quantity_available=item3.quantity)
        lot3b = add_lot(db, "DUPLICATE-B", customer3, product3, quantity_available=item3.quantity)
        for lot in (lot3a, lot3b):
            lot.source_type = "production_completion"
            lot.source_ref_type = "production_completion"
            lot.source_ref_id = completion3.id
        completion3.inventory_lot_id = lot3a.id
        add_movement(
            db,
            "DUPLICATE-IN",
            lot3a,
            "manual_in",
            item3.quantity,
            order=order3,
            item=item3,
        )
        db.commit()
        no_move_lot_id = lot2.id

        report = run_audit(db)

    codes = finding_codes(report)
    assert codes.count("P015_COMPLETION_WITHOUT_ACTIVE_FINISHED_LOT") == 1
    movement_findings = [
        row
        for row in report["findings"]
        if row["code"] == "P015_COMPLETION_WITHOUT_INVENTORY_MOVEMENT"
    ]
    assert len(movement_findings) == 1
    assert any(
        row["evidence"]["inventory_lot_id"] == no_move_lot_id
        for row in movement_findings
    )
    assert codes.count("P015_DUPLICATE_RECEIPT_FINISHED_OUTPUT") == 1


def test_completion_transfer_descendants_preserve_source_identity_but_forged_edges_do_not(
    tmp_path: Path,
):
    """A real location-transfer graph is not a second completion lot."""
    engine = build_p0_15_database(tmp_path / "completion-transfer.sqlite3")
    with Session(engine) as db:
        customer, product, order, item = add_order(db, "TRANSFER-OK", status="pending_delivery")
        task = add_task(db, item)
        add_traced_receipt(db, "TRANSFER-OK", order, item)
        source_location = add_location(db, "TRANSFER-SOURCE")
        target_location = add_location(db, "TRANSFER-TARGET")
        completion = add_completion(db, "TRANSFER-OK", task, item, location=source_location)
        source = add_lot(db, "TRANSFER-OK-SOURCE", customer, product, source_ref_id=completion.id,
                         quantity_available=0, location=source_location)
        source.status = "closed"
        target = add_lot(db, "TRANSFER-OK-TARGET", customer, product, source_ref_id=completion.id,
                         quantity_available=item.quantity, location=target_location)
        target.source_type = "transfer"
        completion.inventory_lot_id = source.id
        add_movement(db, "TRANSFER-OK-IN", source, "manual_in", item.quantity, order=order, item=item)
        add_location_transfer(
            db, "transfer-ok", source, target,
            source_location=source_location, target_location=target_location,
            quantity=item.quantity, available_quantity=item.quantity,
            reserved_quantity=0,
        )

        bad_customer, bad_product, bad_order, bad_item = add_order(db, "TRANSFER-BAD", status="pending_delivery")
        bad_task = add_task(db, bad_item)
        add_traced_receipt(db, "TRANSFER-BAD", bad_order, bad_item)
        bad_source_location = add_location(db, "TRANSFER-BAD-SOURCE")
        bad_target_location = add_location(db, "TRANSFER-BAD-TARGET")
        bad_completion = add_completion(db, "TRANSFER-BAD", bad_task, bad_item, location=bad_source_location)
        bad_source = add_lot(db, "TRANSFER-BAD-SOURCE", bad_customer, bad_product, source_ref_id=bad_completion.id,
                             quantity_available=0, location=bad_source_location)
        bad_source.status = "closed"
        wrong_product = Product(customer_id=bad_customer.id, product_code="ANON-WRONG-TRANSFER",
                                customer_material_code="ANON-WRONG-TRANSFER", product_name="错误移库", pieces_per_box=1)
        db.add(wrong_product); db.flush()
        bad_target = add_lot(db, "TRANSFER-BAD-TARGET", bad_customer, wrong_product,
                             source_ref_id=bad_completion.id, quantity_available=bad_item.quantity,
                             location=bad_target_location)
        bad_target.source_type = "transfer"
        bad_completion.inventory_lot_id = bad_source.id
        add_movement(db, "TRANSFER-BAD-IN", bad_source, "manual_in", bad_item.quantity, order=bad_order, item=bad_item)
        add_location_transfer(
            db, "transfer-bad", bad_source, bad_target,
            source_location=bad_source_location, target_location=bad_target_location,
            quantity=bad_item.quantity, available_quantity=bad_item.quantity,
            reserved_quantity=0,
        )
        db.commit()
        report = run_audit(db)

    active_errors = [row for row in report["findings"]
                     if row["code"] == "P015_COMPLETION_WITHOUT_ACTIVE_FINISHED_LOT"]
    assert len(active_errors) == 1
    assert finding_codes(report).count("P015_DUPLICATE_RECEIPT_FINISHED_OUTPUT") == 1


def test_completion_transfer_chain_keeps_partial_two_level_full_move_and_consumption_explainable(
    tmp_path: Path,
):
    engine = build_p0_15_database(tmp_path / "completion-transfer-lineage.sqlite3")
    with Session(engine) as db:
        customer, product, order, item = add_order(
            db, "TRANSFER-LINEAGE", status="pending_delivery", quantity=10
        )
        task = add_task(db, item)
        add_traced_receipt(db, "TRANSFER-LINEAGE", order, item)
        root_location = add_location(db, "TRANSFER-LINEAGE-ROOT")
        first_target = add_location(db, "TRANSFER-LINEAGE-FIRST")
        moved_target = add_location(db, "TRANSFER-LINEAGE-MOVED")
        final_target = add_location(db, "TRANSFER-LINEAGE-FINAL")
        completion = add_completion(
            db, "TRANSFER-LINEAGE", task, item, location=root_location
        )
        root = add_lot(
            db, "TRANSFER-LINEAGE-ROOT", customer, product,
            source_ref_id=completion.id, quantity_available=4, location=root_location,
        )
        child = add_lot(
            db, "TRANSFER-LINEAGE-CHILD", customer, product,
            source_ref_id=completion.id, quantity_available=6, location=first_target,
        )
        child.source_type = "transfer"
        grandchild = add_lot(
            db, "TRANSFER-LINEAGE-GRANDCHILD", customer, product,
            source_ref_id=completion.id, quantity_available=3, location=final_target,
        )
        grandchild.source_type = "transfer"
        completion.inventory_lot_id = root.id
        add_movement(
            db, "TRANSFER-LINEAGE-IN", root, "manual_in", 10,
            order=order, item=item,
        )
        add_location_transfer(
            db, "lineage-first", root, child,
            source_location=root_location, target_location=first_target,
            quantity=6, available_quantity=6, reserved_quantity=0,
        )
        child.warehouse_location_id = moved_target.id
        add_location_transfer(
            db, "lineage-full", child, child,
            source_location=first_target, target_location=moved_target,
            quantity=6, available_quantity=6, reserved_quantity=0,
        )
        child.quantity_available = 3
        second_transfer = add_location_transfer(
            db, "lineage-second", child, grandchild,
            source_location=moved_target, target_location=final_target,
            quantity=3, available_quantity=3, reserved_quantity=0,
        )
        second_transfer.source_version_before = 2
        second_transfer.source_version_after = 3
        grandchild.quantity_available = 0
        grandchild.quantity_consumed = 3
        consume = add_movement(
            db, "TRANSFER-LINEAGE-CONSUME", grandchild, "consume", 3
        )
        consume.before_available = 3
        consume.after_available = 0
        consume.before_consumed = 0
        consume.after_consumed = 3
        db.commit()
        report = run_audit(db)

    codes = finding_codes(report)
    assert "P015_COMPLETION_WITHOUT_ACTIVE_FINISHED_LOT" not in codes
    assert "P015_DUPLICATE_RECEIPT_FINISHED_OUTPUT" not in codes
    assert not any(
        row["evidence"].get("trace_kind") == "completion_transfer_graph_invalid"
        for row in report["findings"]
    )


def test_split_completion_transfer_conserves_stock_disposition_not_direct_quantity(
    tmp_path: Path,
):
    engine = build_p0_15_database(tmp_path / "completion-transfer-split.sqlite3")
    with Session(engine) as db:
        customer, product, order, item = add_order(
            db, "TRANSFER-SPLIT-COMPLETION", status="pending_delivery", quantity=10
        )
        task = add_task(db, item)
        add_traced_receipt(db, "TRANSFER-SPLIT-COMPLETION", order, item)
        source_location = add_location(db, "TRANSFER-SPLIT-SOURCE")
        target_location = add_location(db, "TRANSFER-SPLIT-TARGET")
        completion = add_completion(
            db, "TRANSFER-SPLIT-COMPLETION", task, item, location=source_location
        )
        completion.initial_disposition = "split"
        completion.stock_quantity = 6
        completion.direct_delivery_quantity = 4
        completion.order_reserved_quantity = 6
        completion.surplus_finished_quantity = 4
        source = add_lot(
            db, "TRANSFER-SPLIT-SOURCE", customer, product,
            source_ref_id=completion.id, quantity_available=0,
            location=source_location,
        )
        source.status = "closed"
        target = add_lot(
            db, "TRANSFER-SPLIT-TARGET", customer, product,
            source_ref_id=completion.id, quantity_available=6,
            location=target_location,
        )
        target.source_type = "transfer"
        completion.inventory_lot_id = source.id
        add_movement(
            db, "TRANSFER-SPLIT-IN", source, "manual_in", 6,
            order=order, item=item,
        )
        add_location_transfer(
            db, "split-completion", source, target,
            source_location=source_location, target_location=target_location,
            quantity=6, available_quantity=6, reserved_quantity=0,
        )
        db.commit()
        report = run_audit(db)

    codes = finding_codes(report)
    assert "P015_COMPLETION_WITHOUT_ACTIVE_FINISHED_LOT" not in codes
    assert "P015_COMPLETION_WITHOUT_INVENTORY_MOVEMENT" not in codes
    assert not any(
        row["evidence"].get("trace_kind") == "completion_transfer_graph_invalid"
        for row in report["findings"]
    )


def test_completion_transfer_chain_reports_closed_no_transfer_equal_quantity_wrong_legs_and_active_bad_edge(
    tmp_path: Path,
):
    engine = build_p0_15_database(tmp_path / "completion-transfer-red.sqlite3")
    with Session(engine) as db:
        # A closed completion root without a real transfer remains an error.
        customer1, product1, order1, item1 = add_order(db, "TRANSFER-CLOSED", status="pending_delivery")
        task1 = add_task(db, item1)
        add_traced_receipt(db, "TRANSFER-CLOSED", order1, item1)
        location1 = add_location(db, "TRANSFER-CLOSED")
        completion1 = add_completion(db, "TRANSFER-CLOSED", task1, item1, location=location1)
        closed_root = add_lot(
            db, "TRANSFER-CLOSED", customer1, product1,
            source_ref_id=completion1.id, quantity_available=0, location=location1,
        )
        closed_root.status = "closed"
        completion1.inventory_lot_id = closed_root.id
        add_movement(db, "TRANSFER-CLOSED-IN", closed_root, "manual_in", item1.quantity, order=order1, item=item1)
        completion1_id = completion1.id

        # Same amounts on the opposite lots cannot substitute for this
        # transfer's exact source/target ledger keys.
        customer2, product2, order2, item2 = add_order(db, "TRANSFER-WRONG-LEGS", status="pending_delivery")
        task2 = add_task(db, item2)
        add_traced_receipt(db, "TRANSFER-WRONG-LEGS", order2, item2)
        source_location2 = add_location(db, "TRANSFER-WRONG-LEGS-SOURCE")
        target_location2 = add_location(db, "TRANSFER-WRONG-LEGS-TARGET")
        completion2 = add_completion(db, "TRANSFER-WRONG-LEGS", task2, item2, location=source_location2)
        root2 = add_lot(
            db, "TRANSFER-WRONG-LEGS-ROOT", customer2, product2,
            source_ref_id=completion2.id, quantity_available=0, location=source_location2,
        )
        root2.status = "closed"
        target2 = add_lot(
            db, "TRANSFER-WRONG-LEGS-TARGET", customer2, product2,
            source_ref_id=completion2.id, quantity_available=item2.quantity, location=target_location2,
        )
        target2.source_type = "transfer"
        completion2.inventory_lot_id = root2.id
        add_movement(db, "TRANSFER-WRONG-LEGS-IN", root2, "manual_in", item2.quantity, order=order2, item=item2)
        wrong_source = add_movement(db, "TRANSFER-WRONG-LEGS-SOURCE", target2, "location_transfer", item2.quantity)
        wrong_source.idempotency_key = "location-transfer:anon-transfer-wrong-legs:source"
        wrong_target = add_movement(db, "TRANSFER-WRONG-LEGS-TARGET", root2, "location_transfer", item2.quantity)
        wrong_target.idempotency_key = "location-transfer:anon-transfer-wrong-legs:target"
        db.add(InventoryLotTransfer(
            source_lot_id=root2.id, target_lot_id=target2.id,
            source_location_id=source_location2.id, target_location_id=target_location2.id,
            quantity=item2.quantity, available_quantity=item2.quantity, reserved_quantity=0,
            source_version_before=1, source_version_after=2,
            idempotency_key="anon-transfer-wrong-legs", request_hash="c" * 64, transferred_at=NOW,
        ))
        completion2_id = completion2.id

        # An active root must also be reported when a transfer ledger is
        # malformed; otherwise it would silently bypass the closed-lot check.
        customer3, product3, order3, item3 = add_order(db, "TRANSFER-ACTIVE-BAD", status="pending_delivery")
        task3 = add_task(db, item3)
        add_traced_receipt(db, "TRANSFER-ACTIVE-BAD", order3, item3)
        source_location3 = add_location(db, "TRANSFER-ACTIVE-BAD-SOURCE")
        target_location3 = add_location(db, "TRANSFER-ACTIVE-BAD-TARGET")
        completion3 = add_completion(db, "TRANSFER-ACTIVE-BAD", task3, item3, location=source_location3)
        root3 = add_lot(
            db, "TRANSFER-ACTIVE-BAD-ROOT", customer3, product3,
            source_ref_id=completion3.id, quantity_available=4, location=source_location3,
        )
        target3 = add_lot(
            db, "TRANSFER-ACTIVE-BAD-TARGET", customer3, product3,
            source_ref_id=completion3.id, quantity_available=item3.quantity - 4, location=target_location3,
        )
        target3.source_type = "transfer"
        completion3.inventory_lot_id = root3.id
        add_movement(db, "TRANSFER-ACTIVE-BAD-IN", root3, "manual_in", item3.quantity, order=order3, item=item3)
        add_location_transfer(
            db, "active-bad", root3, target3,
            source_location=source_location3, target_location=target_location3,
            quantity=item3.quantity - 4, available_quantity=item3.quantity - 4,
            reserved_quantity=0,
        )
        bad_source_move = db.scalar(
            select(InventoryMovement).where(
                InventoryMovement.idempotency_key == "location-transfer:anon-transfer-active-bad:source"
            )
        )
        assert bad_source_move is not None
        bad_source_move.after_available += 1
        completion3_id = completion3.id
        db.commit()
        report = run_audit(db)

    closed_errors = [
        row for row in report["findings"]
        if row["code"] == "P015_COMPLETION_WITHOUT_ACTIVE_FINISHED_LOT"
        and row["evidence"].get("completion_id") == completion1_id
    ]
    assert len(closed_errors) == 1
    invalid_transfers = [
        row for row in report["findings"]
        if row["evidence"].get("trace_kind") == "completion_transfer_graph_invalid"
    ]
    assert {row["evidence"]["completion_id"] for row in invalid_transfers} == {
        completion2_id,
        completion3_id,
    }


def test_completion_transfer_chain_rejects_unexplained_location_and_balance_mutation(
    tmp_path: Path,
):
    engine = build_p0_15_database(tmp_path / "completion-transfer-state-red.sqlite3")
    with Session(engine) as db:
        invalid_completion_ids = set()
        for token, defect in (
            ("TRANSFER-LEAF-LOCATION", "location"),
            ("TRANSFER-BALANCE-FAMILY", "consumed"),
            ("TRANSFER-TERMINAL-TOTAL", "terminal_total"),
        ):
            customer, product, order, item = add_order(
                db, token, status="pending_delivery", quantity=10
            )
            task = add_task(db, item)
            add_traced_receipt(db, token, order, item)
            source_location = add_location(db, f"{token}-SOURCE")
            target_location = add_location(db, f"{token}-TARGET")
            completion = add_completion(
                db, token, task, item, location=source_location
            )
            source = add_lot(
                db, f"{token}-SOURCE", customer, product,
                source_ref_id=completion.id, quantity_available=0,
                location=source_location,
            )
            source.status = "closed"
            target = add_lot(
                db, f"{token}-TARGET", customer, product,
                source_ref_id=completion.id, quantity_available=10,
                location=target_location,
            )
            target.source_type = "transfer"
            completion.inventory_lot_id = source.id
            manual_in = add_movement(
                db, f"{token}-IN", source, "manual_in", 10,
                order=order, item=item,
            )
            add_location_transfer(
                db, token.lower(), source, target,
                source_location=source_location, target_location=target_location,
                quantity=10, available_quantity=10, reserved_quantity=0,
            )
            if defect == "location":
                target.warehouse_location_id = add_location(
                    db, f"{token}-UNEXPLAINED"
                ).id
            elif defect == "consumed":
                source_move = db.scalar(
                    select(InventoryMovement).where(
                        InventoryMovement.idempotency_key
                        == f"location-transfer:anon-transfer-{token.lower()}:source"
                    )
                )
                assert source_move is not None
                source_move.after_consumed = source_move.before_consumed + 1
            else:
                manual_in.after_available = 11
                source_move = db.scalar(
                    select(InventoryMovement).where(
                        InventoryMovement.idempotency_key
                        == f"location-transfer:anon-transfer-{token.lower()}:source"
                    )
                )
                assert source_move is not None
                source_move.before_available = 11
                source_move.after_available = 1
                source.quantity_available = 1
            invalid_completion_ids.add(completion.id)
        db.commit()
        report = run_audit(db)

    invalid_transfers = {
        row["evidence"]["completion_id"]
        for row in report["findings"]
        if row["evidence"].get("trace_kind") == "completion_transfer_graph_invalid"
    }
    assert invalid_transfers == invalid_completion_ids


def test_valid_transfer_does_not_hide_unrelated_transfer_labelled_duplicate(
    tmp_path: Path,
):
    engine = build_p0_15_database(tmp_path / "completion-transfer-orphan.sqlite3")
    with Session(engine) as db:
        customer, product, order, item = add_order(
            db, "TRANSFER-ORPHAN", status="pending_delivery", quantity=10
        )
        task = add_task(db, item)
        add_traced_receipt(db, "TRANSFER-ORPHAN", order, item)
        source_location = add_location(db, "TRANSFER-ORPHAN-SOURCE")
        target_location = add_location(db, "TRANSFER-ORPHAN-TARGET")
        completion = add_completion(
            db, "TRANSFER-ORPHAN", task, item, location=source_location
        )
        source = add_lot(
            db, "TRANSFER-ORPHAN-SOURCE", customer, product,
            source_ref_id=completion.id, quantity_available=0,
            location=source_location,
        )
        source.status = "closed"
        target = add_lot(
            db, "TRANSFER-ORPHAN-TARGET", customer, product,
            source_ref_id=completion.id, quantity_available=10,
            location=target_location,
        )
        target.source_type = "transfer"
        orphan = add_lot(
            db, "TRANSFER-ORPHAN-FORGED", customer, product,
            source_ref_id=completion.id, quantity_available=10,
        )
        orphan.source_type = "transfer"
        orphan_id = orphan.id
        completion.inventory_lot_id = source.id
        add_movement(
            db, "TRANSFER-ORPHAN-IN", source, "manual_in", 10,
            order=order, item=item,
        )
        add_location_transfer(
            db, "orphan-valid-edge", source, target,
            source_location=source_location, target_location=target_location,
            quantity=10, available_quantity=10, reserved_quantity=0,
        )
        db.commit()
        report = run_audit(db)

    duplicate = [
        row for row in report["findings"]
        if row["code"] == "P015_DUPLICATE_RECEIPT_FINISHED_OUTPUT"
    ]
    assert len(duplicate) == 1
    assert orphan_id in duplicate[0]["evidence"]["inventory_lot_ids"]


def test_transfer_graph_rejects_two_sources_merging_into_one_lot(tmp_path: Path):
    engine = build_p0_15_database(tmp_path / "completion-transfer-merge.sqlite3")
    with Session(engine) as db:
        customer, product, order, item = add_order(
            db, "TRANSFER-MERGE", status="pending_delivery", quantity=10
        )
        task = add_task(db, item)
        add_traced_receipt(db, "TRANSFER-MERGE", order, item)
        root_location = add_location(db, "TRANSFER-MERGE-ROOT")
        left_location = add_location(db, "TRANSFER-MERGE-LEFT")
        right_location = add_location(db, "TRANSFER-MERGE-RIGHT")
        merged_location = add_location(db, "TRANSFER-MERGE-TARGET")
        completion = add_completion(
            db, "TRANSFER-MERGE", task, item, location=root_location
        )
        completion_id = completion.id
        root = add_lot(
            db, "TRANSFER-MERGE-ROOT", customer, product,
            source_ref_id=completion.id, quantity_available=5,
            location=root_location,
        )
        left = add_lot(
            db, "TRANSFER-MERGE-LEFT", customer, product,
            source_ref_id=completion.id, quantity_available=5,
            location=left_location,
        )
        right = add_lot(
            db, "TRANSFER-MERGE-RIGHT", customer, product,
            source_ref_id=completion.id, quantity_available=5,
            location=right_location,
        )
        merged = add_lot(
            db, "TRANSFER-MERGE-TARGET", customer, product,
            source_ref_id=completion.id, quantity_available=5,
            location=merged_location,
        )
        left.source_type = right.source_type = merged.source_type = "transfer"
        completion.inventory_lot_id = root.id
        add_movement(
            db, "TRANSFER-MERGE-IN", root, "manual_in", 10,
            order=order, item=item,
        )
        add_location_transfer(
            db, "merge-root-left", root, left,
            source_location=root_location, target_location=left_location,
            quantity=5, available_quantity=5, reserved_quantity=0,
        )
        root.quantity_available = 0
        second_root = add_location_transfer(
            db, "merge-root-right", root, right,
            source_location=root_location, target_location=right_location,
            quantity=5, available_quantity=5, reserved_quantity=0,
        )
        second_root.source_version_before = 2
        second_root.source_version_after = 3
        left.quantity_available = 0
        add_location_transfer(
            db, "merge-left-target", left, merged,
            source_location=left_location, target_location=merged_location,
            quantity=5, available_quantity=5, reserved_quantity=0,
        )
        right.quantity_available = 0
        merged.quantity_available = 10
        add_location_transfer(
            db, "merge-right-target", right, merged,
            source_location=right_location, target_location=merged_location,
            quantity=5, available_quantity=5, reserved_quantity=0,
        )
        db.commit()
        report = run_audit(db)

    assert any(
        row["evidence"].get("trace_kind") == "completion_transfer_graph_invalid"
        and row["evidence"].get("completion_id") == completion_id
        for row in report["findings"]
    )


def test_completion_manual_in_without_auxiliary_order_links_needs_source_identity(
    tmp_path: Path,
):
    engine = build_p0_15_database(tmp_path / "manual-in-identity.sqlite3")
    with Session(engine) as db:
        customer, product, order, item = add_order(db, "MANUAL-IDENTITY", status="pending_delivery")
        task = add_task(db, item)
        add_traced_receipt(db, "MANUAL-IDENTITY", order, item)
        location = add_location(db, "MANUAL-IDENTITY")
        completion = add_completion(db, "MANUAL-IDENTITY", task, item, location=location)
        lot = add_lot(db, "MANUAL-IDENTITY", customer, product, source_ref_id=completion.id,
                      quantity_available=item.quantity, location=location)
        completion.inventory_lot_id = lot.id
        movement = add_movement(db, "MANUAL-IDENTITY", lot, "manual_in", item.quantity)
        movement.related_order_id = None
        movement.related_order_item_id = None
        db.commit()
        report = run_audit(db)
    assert "P015_COMPLETION_WITHOUT_INVENTORY_MOVEMENT" not in finding_codes(report)


def test_delivery_fractional_credit_uses_frozen_denominator_and_accept_over_is_review(
    tmp_path: Path,
):
    engine = build_p0_15_database(tmp_path / "delivery-fraction.sqlite3")
    with Session(engine) as db:
        customer, product, order, item = add_order(
            db, "DELIVERY-FRACTION", status="partially_delivered", quantity=4,
            delivered_quantity=3,
        )
        lot = add_lot(db, "DELIVERY-FRACTION", customer, product, quantity_available=0,
                      quantity_reserved=4)
        reservation = InventoryReservation(
            reservation_number="ANON-RESERVATION-FRACTION", inventory_lot_id=lot.id,
            reservation_type="finished_order", order_id=order.id, order_item_id=item.id,
            reserved_stock_quantity=4, credited_requirement_quantity=4,
            requirement_quantity_denominator=2, consumed_stock_quantity=4,
            consumed_requirement_quantity=4, status="consumed",
        )
        db.add(reservation); db.flush()
        delivery = Delivery(delivery_number="ANON-DELIVERY-FRACTION", customer_id=customer.id,
                            delivery_date=TODAY, status="dispatched", total_quantity=2)
        db.add(delivery); db.flush()
        line = DeliveryItem(delivery_id=delivery.id, order_item_id=item.id,
                            delivered_quantity=2, ordered_quantity_snapshot=item.quantity,
                            source_type="order", is_current=True)
        db.add(line); db.flush()
        movement = add_movement(db, "DELIVERY-FRACTION", lot, "consume", 4,
                                order=order, item=item, delivery=delivery, reservation=reservation)
        db.add(DeliveryInventoryAllocation(
            delivery_item_id=line.id, reservation_id=reservation.id,
            consume_movement_id=movement.id, consumed_stock_quantity=4,
            credited_requirement_quantity=4, requirement_quantity_denominator=2,
            status="active",
        ))
        receipt = ReturnReceipt(delivery_id=delivery.id, actual_received_date=TODAY, status="confirmed")
        db.add(receipt); db.flush()
        db.add(ReturnReceiptItem(return_receipt_id=receipt.id, delivery_item_id=line.id,
                                 actual_received_quantity=3, resolution_action="accept_over"))
        db.commit()
        assert db.scalar(select(ReturnReceiptItem.resolution_action)) == "accept_over"
        from app.services.incomplete_order_chain_audit import (
            _delivery_rows,
            _effective_delivery_quantities,
        )
        delivery_rows, allocation_rows = _delivery_rows(db, [item.id])
        assert delivery_rows[item.id][0]["id"] == line.id
        assert allocation_rows[line.id][0]["requirement_quantity_denominator"] == 2
        effective = _effective_delivery_quantities(db, [line.id])
        assert effective[line.id]["resolution_action"] == "accept_over"
        assert effective[line.id]["actual_received_quantity"] == 3
        report = run_audit(db)
    codes = finding_codes(report)
    assert "P015_DELIVERY_INVENTORY_QUANTITY_MISMATCH" not in codes
    assert "P015_ORDER_STATUS_SNAPSHOT_DIVERGENCE" not in codes
    assert any(
        row["evidence"].get("trace_kind") == "delivery_receipt_quantity_divergence"
        for row in report["findings"]
    )


def test_accept_over_completed_fulfillment_matches_persisted_status_and_keeps_review(
    tmp_path: Path,
):
    engine = build_p0_15_database(tmp_path / "delivery-accept-over-complete.sqlite3")
    with Session(engine) as db:
        customer, product, order, item = add_order(
            db, "DELIVERY-ACCEPT-OVER", status="delivered", quantity=100,
            delivered_quantity=101,
        )
        lot = add_lot(
            db, "DELIVERY-ACCEPT-OVER", customer, product,
            quantity_available=0, quantity_reserved=100,
        )
        reservation = InventoryReservation(
            reservation_number="ANON-RESERVATION-ACCEPT-OVER",
            inventory_lot_id=lot.id, reservation_type="finished_order",
            order_id=order.id, order_item_id=item.id,
            reserved_stock_quantity=100, credited_requirement_quantity=100,
            requirement_quantity_denominator=1, consumed_stock_quantity=100,
            consumed_requirement_quantity=100, status="consumed",
        )
        db.add(reservation); db.flush()
        delivery = Delivery(
            delivery_number="ANON-DELIVERY-ACCEPT-OVER",
            customer_id=customer.id, delivery_date=TODAY,
            status="dispatched", total_quantity=100,
        )
        db.add(delivery); db.flush()
        line = DeliveryItem(
            delivery_id=delivery.id, order_item_id=item.id,
            delivered_quantity=100, ordered_quantity_snapshot=100,
            source_type="order", is_current=True,
        )
        db.add(line); db.flush()
        movement = add_movement(
            db, "DELIVERY-ACCEPT-OVER", lot, "consume", 100,
            order=order, item=item, delivery=delivery, reservation=reservation,
        )
        db.add(DeliveryInventoryAllocation(
            delivery_item_id=line.id, reservation_id=reservation.id,
            consume_movement_id=movement.id, consumed_stock_quantity=100,
            credited_requirement_quantity=100,
            requirement_quantity_denominator=1, status="active",
        ))
        receipt = ReturnReceipt(
            delivery_id=delivery.id, actual_received_date=TODAY,
            status="confirmed",
        )
        db.add(receipt); db.flush()
        db.add(ReturnReceiptItem(
            return_receipt_id=receipt.id, delivery_item_id=line.id,
            actual_received_quantity=101, resolution_action="accept_over",
        ))
        db.commit()
        report = run_audit(db)

    codes = finding_codes(report)
    assert "P015_DELIVERY_INVENTORY_QUANTITY_MISMATCH" not in codes
    assert "P015_ORDER_STATUS_SNAPSHOT_DIVERGENCE" not in codes
    assert any(
        row["evidence"].get("trace_kind") == "delivery_receipt_quantity_divergence"
        for row in report["findings"]
    )


def test_p0_15_cli_scans_only_a_new_synthetic_copy(tmp_path: Path, capsys):
    database = tmp_path / "cli.sqlite3"
    engine = build_p0_15_database(database)
    with Session(engine) as db:
        add_order(db, "CLI-READONLY")
        db.commit()
    engine.dispose()
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    from scripts.audit.p0_15_incomplete_order_chain import main
    code = main([
        "--database", str(database),
        "--json-output", str(tmp_path / "report.json"),
        "--csv-output", str(tmp_path / "report.csv"),
        "--markdown-output", str(tmp_path / "report.md"),
        "--source-label", "synthetic-cli-only",
        "--expected-revision", REVISION,
        "--expected-sha256", before,
    ])
    assert code == 0, capsys.readouterr().err
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert not any((tmp_path / f"cli.sqlite3{suffix}").exists()
                   for suffix in ("-journal", "-wal", "-shm"))


def test_active_finished_reservation_must_really_be_deliverable(tmp_path: Path):
    engine = build_p0_15_database(tmp_path / "reservation.sqlite3")
    with Session(engine) as db:
        customer, product, order, item = add_order(db, "BAD-RESERVE", status="pending_delivery")
        other_customer, other_product, _, _ = add_order(db, "OTHER", status="pending_confirmation")
        lot = add_lot(
            db,
            "BAD-RESERVE",
            customer,
            product,
            quantity_available=0,
            quantity_reserved=4,
            detail_product=other_product,
        )
        db.add(
            InventoryReservation(
                reservation_number="ANON-RESERVATION-BAD",
                inventory_lot_id=lot.id,
                reservation_type="finished_order",
                order_id=order.id,
                order_item_id=item.id,
                reserved_stock_quantity=4,
                credited_requirement_quantity=4,
            )
        )
        db.commit()

        report = run_audit(db)

    assert finding_codes(report).count("P015_FINISHED_STOCK_NOT_DELIVERABLE") == 1
    finding = next(
        row
        for row in report["findings"]
        if row["code"] == "P015_FINISHED_STOCK_NOT_DELIVERABLE"
    )
    assert finding["evidence"]["remaining_reserved_quantity"] == 4


def test_component_finished_reservation_uses_frozen_component_product(
    tmp_path: Path,
):
    engine = build_p0_15_database(tmp_path / "component-reservation.sqlite3")
    with Session(engine) as db:
        customer, parent_product, order, item = add_order(
            db, "COMPONENT", status="pending_delivery"
        )
        parent_product.is_virtual_composite_parent = True
        component_product = Product(
            customer_id=customer.id,
            product_code="ANON-COMPONENT-PRODUCT",
            customer_material_code="ANON-COMPONENT-MATERIAL",
            product_name="匿名组合组件",
            pieces_per_box=1,
        )
        db.add(component_product)
        db.flush()
        component = SalesOrderItemBomComponent(
            sales_order_item_id=item.id,
            component_product_id=component_product.id,
            order_set_quantity=item.quantity,
            quantity_per_set=Decimal("1"),
            required_piece_quantity=Decimal(item.quantity),
            display_order=0,
            internal_component_code="ANON-COMPONENT-01",
            is_die_cut=False,
            spare_sheet_quantity=0,
            display_mode="internal_only",
            show_on_delivery=False,
            is_required=True,
            snapshot_component_product_code=component_product.product_code,
            snapshot_component_product_name=component_product.product_name,
            snapshot_component_box_category="common_box",
            snapshot_component_default_cutting_mode="一开一",
        )
        db.add(component)
        db.flush()
        db.add(
            ProductionTask(
                order_item_id=item.id,
                sales_order_item_bom_component_id=component.id,
                task_role="component_internal",
                status="pending",
                planned_quantity=item.quantity,
                ordered_quantity_snapshot=item.quantity,
                material_received_quantity=item.quantity,
            )
        )
        lot = add_lot(
            db,
            "COMPONENT",
            customer,
            component_product,
            quantity_available=0,
            quantity_reserved=4,
        )
        db.add(
            InventoryReservation(
                reservation_number="ANON-RESERVATION-COMPONENT",
                inventory_lot_id=lot.id,
                reservation_type="finished_order",
                order_id=order.id,
                order_item_id=item.id,
                sales_order_item_bom_component_id=component.id,
                reserved_stock_quantity=4,
                credited_requirement_quantity=4,
            )
        )
        db.commit()

        report = run_audit(db)

    assert "P015_FINISHED_STOCK_NOT_DELIVERABLE" not in finding_codes(report)


def test_finished_reservation_never_crosses_customer_scope(tmp_path: Path):
    engine = build_p0_15_database(tmp_path / "customer-isolation.sqlite3")
    with Session(engine) as db:
        customer, product, order, item = add_order(
            db, "OWNER", status="pending_delivery"
        )
        other_customer, _, _, _ = add_order(
            db, "FOREIGN", status="pending_confirmation"
        )
        lot = add_lot(
            db,
            "FOREIGN-OWNER",
            other_customer,
            product,
            quantity_available=0,
            quantity_reserved=3,
        )
        db.add(
            InventoryReservation(
                reservation_number="ANON-RESERVATION-FOREIGN-OWNER",
                inventory_lot_id=lot.id,
                reservation_type="finished_order",
                order_id=order.id,
                order_item_id=item.id,
                reserved_stock_quantity=3,
                credited_requirement_quantity=3,
            )
        )
        db.commit()

        report = run_audit(db)

    assert finding_codes(report).count("P015_FINISHED_STOCK_NOT_DELIVERABLE") == 1


def test_delivery_uses_confirmed_net_quantity_ignores_void_and_reviews_legacy_no_allocation(
    tmp_path: Path,
):
    engine = build_p0_15_database(tmp_path / "delivery.sqlite3")
    with Session(engine) as db:
        customer, product, order, item = add_order(
            db, "NET-DELIVERY", status="partially_delivered", delivered_quantity=2
        )
        lot = add_lot(db, "NET-DELIVERY", customer, product, quantity_available=6)
        reservation = InventoryReservation(
            reservation_number="ANON-RESERVATION-NET",
            inventory_lot_id=lot.id,
            reservation_type="finished_order",
            order_id=order.id,
            order_item_id=item.id,
            reserved_stock_quantity=4,
            credited_requirement_quantity=4,
            consumed_stock_quantity=4,
            consumed_requirement_quantity=4,
            status="consumed",
        )
        db.add(reservation)
        db.flush()
        delivery = Delivery(
            delivery_number="ANON-DELIVERY-NET",
            customer_id=customer.id,
            delivery_date=TODAY,
            status="dispatched",
            total_quantity=4,
        )
        db.add(delivery)
        db.flush()
        delivery_item = DeliveryItem(
            delivery_id=delivery.id,
            order_item_id=item.id,
            delivered_quantity=4,
            ordered_quantity_snapshot=item.quantity,
        )
        db.add(delivery_item)
        db.flush()
        consume = add_movement(
            db,
            "NET-CONSUME",
            lot,
            "consume",
            4,
            order=order,
            item=item,
            delivery=delivery,
            reservation=reservation,
        )
        db.add(
            DeliveryInventoryAllocation(
                delivery_item_id=delivery_item.id,
                reservation_id=reservation.id,
                consume_movement_id=consume.id,
                consumed_stock_quantity=4,
                credited_requirement_quantity=4,
                reversed_stock_quantity=2,
                reversed_requirement_quantity=2,
                status="partial",
            )
        )
        receipt = ReturnReceipt(
            delivery_id=delivery.id,
            actual_received_date=TODAY,
            status="confirmed",
        )
        db.add(receipt)
        db.flush()
        db.add(
            ReturnReceiptItem(
                return_receipt_id=receipt.id,
                delivery_item_id=delivery_item.id,
                actual_received_quantity=2,
            )
        )

        void_delivery = Delivery(
            delivery_number="ANON-DELIVERY-VOID",
            customer_id=customer.id,
            delivery_date=TODAY,
            status="voided",
            total_quantity=1,
        )
        db.add(void_delivery)
        db.flush()
        db.add(
            DeliveryItem(
                delivery_id=void_delivery.id,
                order_item_id=item.id,
                delivered_quantity=1,
                ordered_quantity_snapshot=item.quantity,
            )
        )

        legacy_customer, _, legacy_order, legacy_item = add_order(
            db, "LEGACY-DISPATCH", status="partially_delivered", delivered_quantity=3
        )
        legacy_delivery = Delivery(
            delivery_number="ANON-DELIVERY-LEGACY",
            customer_id=legacy_customer.id,
            delivery_date=TODAY,
            status="dispatched",
            total_quantity=3,
        )
        db.add(legacy_delivery)
        db.flush()
        db.add(
            DeliveryItem(
                delivery_id=legacy_delivery.id,
                order_item_id=legacy_item.id,
                delivered_quantity=3,
                ordered_quantity_snapshot=legacy_item.quantity,
            )
        )
        db.commit()

        report = run_audit(db)

    delivery_mismatches = [
        row
        for row in report["findings"]
        if row["code"] == "P015_DELIVERY_INVENTORY_QUANTITY_MISMATCH"
    ]
    legacy_reviews = [
        row
        for row in report["findings"]
        if row["code"] == "P015_TRACE_LINK_BROKEN"
        and row["evidence"].get("trace_kind") == "dispatch_without_inventory_allocation"
    ]
    assert delivery_mismatches == []
    assert len(legacy_reviews) == 1
    assert legacy_reviews[0]["severity"] == "review"


def test_two_finished_boxes_per_received_sheet_keeps_input_and_output_units_separate(
    tmp_path: Path,
):
    engine = build_p0_15_database(tmp_path / "two-up.sqlite3")
    with Session(engine) as db:
        customer, product, order, item = add_order(
            db, "TWO-UP", material_status="received", quantity=10
        )
        product.pieces_per_box = 2
        task = add_task(db, item)
        add_traced_receipt(db, "TWO-UP", order, item, quantity=5)
        location = add_location(db, "TWO-UP")
        completion = add_completion(
            db,
            "TWO-UP",
            task,
            item,
            location=location,
        )
        completion.material_input_quantity = 5
        lot = add_lot(
            db,
            "TWO-UP",
            customer,
            product,
            source_ref_id=completion.id,
            quantity_available=10,
            location=location,
        )
        completion.inventory_lot_id = lot.id
        add_movement(
            db,
            "TWO-UP-IN",
            lot,
            "manual_in",
            10,
            order=order,
            item=item,
        )
        db.commit()

        report = run_audit(db)

    assert "P015_COMPLETION_INPUT_EXCEEDS_EFFECTIVE_RECEIPT" not in finding_codes(
        report
    )


def test_frozen_purchase_and_receipt_purpose_chain_is_fully_evaluated(
    tmp_path: Path,
):
    engine = build_p0_15_database(tmp_path / "purpose-valid.sqlite3")
    with Session(engine) as db:
        customer, product, order, item = add_order(
            db,
            "PURPOSE-VALID",
            quantity=10,
            material_status="received",
        )
        add_frozen_purpose_chain(
            db,
            "PURPOSE-VALID",
            customer,
            product,
            order,
            item,
            order_sheet_qty=8,
            reserve_sheet_qty=2,
            finished_output_qty=8,
        )
        db.commit()

        report = run_audit(db)

    codes = finding_codes(report)
    assert "P015_PURCHASE_PURPOSE_ALLOCATION_UNBALANCED" not in codes
    assert "P015_RECEIPT_PURPOSE_ALLOCATION_UNBALANCED" not in codes
    assert "P015_RECEIPT_AUTO_FINISHED_MISMATCH" not in codes
    assert "P015_RESERVE_PURPOSE_GENERATED_ORDER_FINISHED" not in codes
    assert "P015_RECEIVED_AWAITING_MANUAL_PRODUCTION" not in codes
    assert report["coverage"]["purchase_purpose_allocation"]["status"] == "evaluated"
    assert report["coverage"]["purchase_purpose_allocation"][
        "purpose_snapshot_count"
    ] == 1
    assert report["coverage"]["receipt_auto_finished"] == {
        "status": "evaluated",
        "active_purpose_allocation_count": 1,
        "reversed_purpose_allocation_count": 0,
    }


def test_frozen_receipt_without_allocation_and_unbalanced_purchase_are_reported(
    tmp_path: Path,
):
    engine = build_p0_15_database(tmp_path / "purpose-invalid.sqlite3")
    with Session(engine) as db:
        customer, product, order, item = add_order(
            db,
            "PURPOSE-MISSING",
            quantity=10,
            material_status="received",
        )
        facts = add_frozen_purpose_chain(
            db,
            "PURPOSE-MISSING",
            customer,
            product,
            order,
            item,
            order_sheet_qty=10,
            reserve_sheet_qty=0,
            finished_output_qty=0,
            with_allocation=False,
        )
        db.commit()
        snapshot_id = int(facts["snapshot"].id)

    with engine.begin() as connection:
        connection.execute(text("PRAGMA ignore_check_constraints=ON"))
        connection.execute(
            text(
                "UPDATE purchase_purpose_source_snapshots "
                "SET reserve_purpose_sheet_qty=1 WHERE id=:snapshot_id"
            ),
            {"snapshot_id": snapshot_id},
        )
    with Session(engine) as db:
        report = run_audit(db)

    codes = finding_codes(report)
    assert "P015_PURCHASE_PURPOSE_ALLOCATION_UNBALANCED" in codes
    assert "P015_RECEIPT_PURPOSE_ALLOCATION_UNBALANCED" in codes
    assert any(
        row["evidence"].get("trace_kind")
        == "receipt_purpose_contract_mismatch"
        for row in report["findings"]
        if row["code"] == "P015_RECEIPT_PURPOSE_ALLOCATION_UNBALANCED"
    )


def test_reserve_only_receipt_that_creates_order_finished_is_a_hard_error(
    tmp_path: Path,
):
    engine = build_p0_15_database(tmp_path / "reserve-finished.sqlite3")
    with Session(engine) as db:
        customer, product, order, item = add_order(
            db,
            "RESERVE-FINISHED",
            quantity=10,
            material_status="received",
        )
        add_frozen_purpose_chain(
            db,
            "RESERVE-FINISHED",
            customer,
            product,
            order,
            item,
            order_sheet_qty=0,
            reserve_sheet_qty=10,
            finished_output_qty=10,
        )
        db.commit()

        report = run_audit(db)

    reserve_findings = [
        row
        for row in report["findings"]
        if row["code"] == "P015_RESERVE_PURPOSE_GENERATED_ORDER_FINISHED"
    ]
    assert len(reserve_findings) == 1
    assert reserve_findings[0]["severity"] == "error"
    assert reserve_findings[0]["evidence"]["reserve_sheet_qty"] == 10
    assert reserve_findings[0]["evidence"]["finished_output_qty_delta"] == 10


def test_reversed_receipt_purpose_facts_do_not_count_as_current_output(
    tmp_path: Path,
):
    engine = build_p0_15_database(tmp_path / "purpose-reversed.sqlite3")
    with Session(engine) as db:
        customer, product, order, item = add_order(
            db,
            "PURPOSE-REVERSED",
            quantity=10,
            material_status="received",
        )
        facts = add_frozen_purpose_chain(
            db,
            "PURPOSE-REVERSED",
            customer,
            product,
            order,
            item,
            order_sheet_qty=10,
            reserve_sheet_qty=0,
            finished_output_qty=10,
        )
        allocation = facts["allocation"]
        receipt_item = facts["receipt_item"]
        receipt = db.get(IncomingReceipt, receipt_item.receipt_id)
        assert receipt is not None
        completion = facts["completion"]
        assert completion is not None
        db.add(
            IncomingReceiptPurposeReversal(
                incoming_receipt_purpose_allocation_id=allocation.id,
                incoming_receipt_item_id=receipt_item.id,
                reversed_production_completion_id=completion.id,
                reversed_finished_inventory_lot_id=facts["finished_lot"].id,
                cumulative_total_sheet_qty_before=10,
                cumulative_total_sheet_qty_after=0,
                cumulative_order_purpose_sheet_qty_before=10,
                cumulative_order_purpose_sheet_qty_after=0,
                cumulative_reserve_purpose_sheet_qty_before=0,
                cumulative_reserve_purpose_sheet_qty_after=0,
                request_hash=("ab" * 32),
                reversed_by=facts["user"].id,
            )
        )
        receipt.status = "reversed"
        receipt_item.status = "reversed"
        completion.status = "reversed"
        facts["task"].status = "waiting_material"
        facts["task"].planned_quantity = 0
        item.material_status = "pending"
        db.commit()

        report = run_audit(db)

    codes = finding_codes(report)
    assert "P015_RECEIPT_AUTO_FINISHED_MISMATCH" not in codes
    assert "P015_RESERVE_PURPOSE_GENERATED_ORDER_FINISHED" not in codes
    assert report["coverage"]["receipt_auto_finished"] == {
        "status": "evaluated",
        "active_purpose_allocation_count": 0,
        "reversed_purpose_allocation_count": 1,
    }


def test_common_box_contract_and_future_capabilities_are_explicit(tmp_path: Path):
    database = tmp_path / "common-box.sqlite3"
    engine = build_p0_15_database(database)
    with Session(engine) as db:
        _, product, _, _ = add_order(db, "COMMON-BOX")
        db.commit()
        product_id = product.id
    with engine.begin() as connection:
        connection.execute(text("PRAGMA ignore_check_constraints=ON"))
        connection.execute(
            text(
                "UPDATE products SET production_label_enabled=0, "
                "production_label_units_per_label=5, pieces_per_box=0 WHERE id=:id"
            ),
            {"id": product_id},
        )
    with Session(engine) as db:
        report = run_audit(db)

    finding = next(
        row
        for row in report["findings"]
        if row["code"] == "P015_COMMON_BOX_PERSISTED_CONTRACT_INVALID"
    )
    assert set(finding["evidence"]["invalid_reasons"]) == {
        "label_disabled_with_units",
        "non_positive_pieces_per_box",
    }
    assert report["coverage"]["purchase_purpose_allocation"] == {
        "status": "evaluated",
        "active_formal_source_count": 0,
        "purpose_snapshot_count": 0,
    }
    assert report["coverage"]["receipt_auto_finished"] == {
        "status": "evaluated",
        "active_purpose_allocation_count": 0,
        "reversed_purpose_allocation_count": 0,
    }
    assert report["coverage"]["workstation_membership"] == {
        "status": "evaluated",
        "rule_version": "p1-84-v1",
        "eligible_task_count": 0,
        "station_task_counts": {"printing": 0, "die_cut": 0},
        "dual_route_task_count": 0,
        "unrouted_task_count": 0,
    }
    assert report["coverage"]["current_chain_facts"] == {"status": "evaluated"}
    assert report["coverage"]["warehouse_map_stocktake_chain"]["status"] == (
        "evaluated_by_regression_contract"
    )
    assert report["summary"]["scan_complete"] is True


def test_report_and_focus_metadata_never_emit_raw_business_identifiers(tmp_path: Path):
    engine = build_p0_15_database(tmp_path / "redaction.sqlite3")
    with Session(engine) as db:
        _, _, order, item = add_order(db, "FOCUS-SECRET")
        add_traced_receipt(db, "FOCUS-SECRET", order, item)
        raw_values = {
            order.order_number,
            order.customer_po,
            item.item_order_number,
            item.snapshot_product_code,
            item.snapshot_product_name,
        }
        db.commit()

        default_report = run_audit(db)
        focused_report = run_audit(db, focus_terms=("FOCUS-SECRET",))

    default_text = json.dumps(default_report, ensure_ascii=False)
    focused_text = json.dumps(focused_report, ensure_ascii=False)
    assert all(finding["focus_match"] is None for finding in default_report["findings"])
    assert default_report["scope"]["focus_tokens"] == []
    assert focused_report["summary"]["focus_finding_count"] >= 1
    for raw_value in raw_values:
        assert raw_value not in default_text
        assert raw_value not in focused_text
    assert "FOCUS-SECRET" not in focused_text
    for finding in focused_report["findings"]:
        match = finding["focus_match"]
        if match is not None:
            assert set(match) == {"matched", "matched_fields", "focus_tokens"}
            assert match["matched"] is True
            assert all(token.startswith("FOCUS-") for token in match["focus_tokens"])
        assert finding["order_ref"].startswith("ORDER-")
        if finding["order_item_ref"] is not None:
            assert finding["order_item_ref"].startswith("ITEM-")


def test_anonymization_key_is_mandatory(tmp_path: Path):
    engine = build_p0_15_database(tmp_path / "empty-key.sqlite3")
    with Session(engine) as db:
        with pytest.raises(ValueError, match="anonymization_key"):
            audit_incomplete_order_chains(db, anonymization_key=b"")


def test_audit_query_families_do_not_grow_with_unfinished_order_count(
    tmp_path: Path,
):
    def measured_select_count(path: Path, order_count: int) -> int:
        engine = build_p0_15_database(path)
        with Session(engine) as db:
            for index in range(order_count):
                add_order(db, f"QUERY-{index:02d}")
            db.commit()

        statements: list[str] = []

        def capture_statement(
            _connection,
            _cursor,
            statement: str,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture_statement)
        try:
            with Session(engine) as db:
                run_audit(db)
        finally:
            event.remove(engine, "before_cursor_execute", capture_statement)
            engine.dispose()
        return len(statements)

    one_order = measured_select_count(tmp_path / "query-one.sqlite3", 1)
    twenty_orders = measured_select_count(tmp_path / "query-twenty.sqlite3", 20)

    assert one_order == twenty_orders
