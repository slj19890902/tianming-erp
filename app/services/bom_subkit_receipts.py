"""Receipt adapter: component receipts become real components then sub-kits."""
from decimal import Decimal, ROUND_HALF_UP
import json

from sqlalchemy import select, or_
from sqlalchemy.orm import Session

from app.core.time_contract import beijing_today, utc_now_naive
from app.models.bom_subkit import OrderSubkit, SubkitReceiptOutput, SubkitConversion
from app.models.product_bom import RequisitionItemBomSource, SalesOrderItemBomComponent
from app.models.warehouse_inventory import InventoryLot, InventoryReservation
from app.services.bom_subkits import SubkitError, recipe_rows
from app.services.bom_subkit_inventory import assemble_subkit_inventory, reverse_subkit_conversion
from app.services.warehouse_inventory import manual_finished_in, _balances, _movement


def post_component_receipt(db: Session, *, allocation, purpose_snapshot, operator_id: int, order_item_id: int):
    group = db.get(OrderSubkit, order_item_id)
    if group is None or purpose_snapshot.source_bom_requisition_source_id is None:
        return
    if db.get(SubkitReceiptOutput, allocation.id):
        return
    bom_source = db.get(RequisitionItemBomSource, purpose_snapshot.source_bom_requisition_source_id)
    component = db.get(SalesOrderItemBomComponent, bom_source.sales_order_item_bom_component_id)
    if component.id not in {r["bom_snapshot_id"] for r in recipe_rows(group)}:
        raise SubkitError("收料组件不属于订单冻结子套件")
    sheets = allocation.receipt_order_purpose_sheet_qty
    pieces = int(sheets) * int(purpose_snapshot.yield_per_sheet_snapshot or 1)
    if not pieces:
        return
    physical_per_component = int(component.snapshot_component_pieces_per_box or 1)
    if physical_per_component != 1:
        raise SubkitError("子套件原片必须按单片登记，不能混用拼接成品数量")
    from app.services.production_workflow import _receipt_auto_finished_ground_target
    target = _receipt_auto_finished_ground_target(db, claim=True, customer_id=allocation.customer_id)
    lot = manual_finished_in(db, customer_id=allocation.customer_id, product_id=component.component_product_id,
        location_id=target.location.id, quantity=pieces, stock_date=beijing_today(),
        source_type="production_completion", source_ref_type="subkit_receipt", source_ref_id=allocation.id,
        operator_id=operator_id, idempotency_key=f"subkit-raw:{allocation.id}",
        expected_layout_version=target.location.floor3_layout.version if target.location.floor3_layout else None,
        remarks="子套件原片收料", movement_reason="订单组件收料自动形成原片")
    lot.estimated_unit_cost_snapshot = (allocation.order_purpose_cost / pieces).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    lot.cost_snapshot_source = "purchase_receipt_actual"
    lot.cost_snapshot_detail_json = json.dumps({"allocation_id": allocation.id,
        "purchase_receipt_fact_id": allocation.purchase_receipt_fact_id,
        "total_cost": str(allocation.order_purpose_cost), "quantity": pieces})
    lot.cost_snapshot_at = utc_now_naive()
    db.add(SubkitReceiptOutput(allocation_id=allocation.id, order_item_id=order_item_id,
        lot_id=lot.id, quantity=pieces, total_cost=allocation.order_purpose_cost, reversed=False))
    db.flush()
    assemble_after_receipt(db, order_item_id=order_item_id, allocation_id=allocation.id,
        operator_id=operator_id, target_location_id=target.location.id)


def assemble_after_receipt(db, *, order_item_id, allocation_id, operator_id, target_location_id=None):
    group = db.get(OrderSubkit, order_item_id)
    if group is None:
        return
    own_allocations = select(SubkitReceiptOutput.allocation_id).where(
        SubkitReceiptOutput.order_item_id == order_item_id, SubkitReceiptOutput.reversed.is_(False),
    )
    own_raw_ids = select(InventoryLot.id).where(InventoryLot.source_ref_type == "subkit_receipt",
        InventoryLot.source_ref_id.in_(own_allocations))
    reserved_ids = select(InventoryReservation.inventory_lot_id).where(
        InventoryReservation.order_item_id == order_item_id,
        InventoryReservation.sales_order_item_bom_component_id.in_([r["bom_snapshot_id"] for r in recipe_rows(group)]),
        InventoryReservation.status.in_(("active", "partial")))
    eligible = list(db.scalars(select(InventoryLot).where(
        or_(InventoryLot.id.in_(own_raw_ids), InventoryLot.id.in_(reserved_ids)),
        InventoryLot.status == "active", InventoryLot.quantity_available + InventoryLot.quantity_reserved > 0)))
    if target_location_id is None:
        from app.services.production_workflow import _receipt_auto_finished_ground_target
        from app.models.order import OrderItem, Order
        item = db.get(OrderItem, order_item_id)
        order = db.get(Order, item.order_id)
        target_location_id = _receipt_auto_finished_ground_target(db, claim=True, customer_id=order.customer_id).location.id
    assemble_subkit_inventory(db, order_item_id=order_item_id,
        source_lot_versions={row.id: row.version for row in eligible}, target_location_id=target_location_id,
        operation_key=f"subkit-receipt:{allocation_id}", operator_id=operator_id,
        available_lot_ids=list(db.scalars(own_raw_ids)))


def reverse_component_receipt(db: Session, *, allocation_id: int, operator_id: int):
    conversion = db.scalar(select(SubkitConversion).where(SubkitConversion.idempotency_key == f"subkit-receipt:{allocation_id}"))
    if conversion:
        reverse_subkit_conversion(db, conversion_id=conversion.id, operator_id=operator_id)
    source = db.get(SubkitReceiptOutput, allocation_id)
    if source is None or source.reversed:
        return
    lot = db.get(InventoryLot, source.lot_id)
    if (lot is None or lot.status != "active" or lot.quantity_available != source.quantity
            or lot.quantity_reserved or lot.quantity_consumed or lot.quantity_damaged or lot.quantity_scrapped):
        raise SubkitError("该次收料原片已使用，需先撤销后续组套或操作")
    before = _balances(lot)
    lot.quantity_available = 0
    lot.quantity_consumed = source.quantity
    lot.status = "closed"
    lot.version += 1
    _movement(db, lot=lot, movement_type="consume", quantity=source.quantity, before=before,
        operator_id=operator_id, reason="撤销子套件原片收料", idempotency_key=f"subkit-raw-reverse:{allocation_id}")
    source.reversed = True
    db.flush()
