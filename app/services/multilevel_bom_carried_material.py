"""Remaining original material capacity, distinct from physical inventory."""
from collections import defaultdict
import json

from sqlalchemy import select

from app.models.product_bom import RequisitionItemBomSource
from app.models.requisition import RequisitionItem
from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot
from app.models.purchase_receipt import IncomingReceiptPurposeAllocation
from app.models.production import ProductionCompletion
from app.models.warehouse_inventory import InventoryLot
from app.services.multilevel_bom_plan import BomPlanError
from app.services.multilevel_bom_source_handoffs import current_source_handoffs


def source_semi_reservations(db, order_id, source_ids, *, require_pending=False):
    """Keep the original reservation identity and its consumed history."""
    from app.models.warehouse_inventory import InventoryReservation, OrderItemSemiRequirement
    from app.models.product_bom import SalesOrderItemBomComponent
    from app.models.order import Order, OrderItem
    from sqlalchemy import or_
    rows = list(db.execute(select(InventoryReservation, OrderItemSemiRequirement).join(
        OrderItemSemiRequirement, OrderItemSemiRequirement.id == InventoryReservation.semi_requirement_id).where(
            InventoryReservation.order_item_id == order_id, InventoryReservation.reservation_type == "semi_order",
            InventoryReservation.status != "cancelled", or_(
                InventoryReservation.sales_order_item_bom_component_id.in_(source_ids),
                OrderItemSemiRequirement.sales_order_item_bom_component_id.in_(source_ids)))))
    item = db.get(OrderItem, order_id)
    order = db.get(Order, item.order_id) if item else None
    reserved_by_lot = defaultdict(int)
    for reservation, requirement in rows:
        source_id = requirement.sales_order_item_bom_component_id
        source = db.get(SalesOrderItemBomComponent, source_id)
        lot = db.get(InventoryLot, reservation.inventory_lot_id)
        if (order is None or requirement.customer_id != order.customer_id
                or source_id not in source_ids or source is None or source.sales_order_item_id != order_id
                or reservation.sales_order_item_bom_component_id not in (None, source_id)
                or requirement.order_item_id != order_id or lot is None or lot.inventory_type != "semi_finished"
                or lot.status != "active" or lot.semi_finished_detail is None
                or lot.semi_finished_detail.owner_customer_id not in (None, order.customer_id)
                or lot.estimated_unit_cost_snapshot is None or lot.estimated_unit_cost_snapshot < 0
                or reservation.yield_factor <= 0
                or reservation.credited_requirement_quantity > reservation.reserved_stock_quantity * reservation.yield_factor
                or reservation.reserved_stock_quantity-reservation.consumed_stock_quantity-reservation.released_stock_quantity > lot.quantity_reserved):
            raise BomPlanError(f"半成品预占#{reservation.id}的原来源、单位、成本或批次余额不一致")
        if reservation.credited_requirement_quantity > reservation.released_requirement_quantity + reservation.consumed_requirement_quantity:
            pending = db.scalar(select(RequisitionItem.id).join(RequisitionItemBomSource,
                RequisitionItemBomSource.requisition_item_id == RequisitionItem.id).where(
                    RequisitionItemBomSource.sales_order_item_bom_component_id == source_id,
                    RequisitionItem.status.in_({"有效", "supplier_requisition_created"}),
                    RequisitionItem.requisition_qty > 0).limit(1))
            cancelled = db.scalar(select(RequisitionItem.id).join(RequisitionItemBomSource,
                RequisitionItemBomSource.requisition_item_id == RequisitionItem.id).where(
                    RequisitionItemBomSource.sales_order_item_bom_component_id == source_id,
                    RequisitionItem.status.in_({"已取消", "已作废", "已撤回"})).limit(1))
            if pending is None and (require_pending or cancelled is not None):
                raise BomPlanError(f"半成品预占#{reservation.id}尚未用完，但原报料无后续收料；须先明确将剩余预占转给新报料，不能重复抵扣")
        reserved_by_lot[lot.id] += reservation.reserved_stock_quantity-reservation.consumed_stock_quantity-reservation.released_stock_quantity
    for lot_id, quantity in reserved_by_lot.items():
        if quantity > db.get(InventoryLot, lot_id).quantity_reserved:
            raise BomPlanError("半成品交接预占合计超过批次余额")
    return rows


def carried_semi_pieces(db, compiled):
    links = {row.source_snapshot_id: row for row in current_source_handoffs(db, compiled)
        if row.source_kind == "manufactured"}
    if not links:
        return {}
    order_id = compiled.snapshots[0].sales_order_item_id
    rows = source_semi_reservations(db, order_id, links)
    balances = {reservation.id: reservation.credited_requirement_quantity-reservation.released_requirement_quantity
        for reservation, _ in rows}
    from app.services.multilevel_bom_output_history import completion_source_id
    current_ids = {row.id for row in compiled.snapshots}
    for completion in db.scalars(select(ProductionCompletion).where(
            ProductionCompletion.order_item_id == order_id, ProductionCompletion.status == "posted")):
        if completion_source_id(db, completion) in current_ids:
            continue
        lot = db.get(InventoryLot, completion.inventory_lot_id)
        detail = json.loads(lot.cost_snapshot_detail_json or "{}") if lot else {}
        from app.services.multilevel_bom_body_inventory import carried_body_quantity
        body_quantity = carried_body_quantity(db, compiled, completion)
        for entry in detail.get("bom_material_inputs", []):
            if entry.get("kind") == "reservation" and entry.get("id") in balances:
                if type(entry.get("quantity")) is not int or entry["quantity"] <= 0:
                    raise BomPlanError("半成品历史消耗片数无效")
                historical = entry["quantity"] * (completion.quantity-body_quantity)
                if historical % completion.quantity:
                    raise BomPlanError("历史本体半成品消耗不能按完整物理片数分摊")
                balances[entry["id"]] -= historical // completion.quantity
    result = defaultdict(int)
    for reservation, requirement in rows:
        quantity = balances[reservation.id]
        if quantity < 0:
            raise BomPlanError("半成品历史消耗超过预占片数")
        result[links[requirement.sales_order_item_bom_component_id].product_id, requirement.component_type] += quantity
    return dict(result)


def carried_material_pieces(db, compiled, *, include_pending=True):
    """Credit old paid/pending material minus its historical output inputs.

    Current-version outputs stay represented by their original material, just
    like ordinary reported material. Historical outputs must be allocated as
    real lots separately; their consumed material cannot be credited again.
    Reserve-purpose sheets and other orders never contribute.
    """
    links = {row.source_snapshot_id: row for row in current_source_handoffs(db, compiled)
        if row.source_kind == "manufactured"}
    if not links:
        return {}
    current_ids = {row.id for row in compiled.snapshots}
    order_id = compiled.snapshots[0].sales_order_item_id
    nodes = {node.product_id: node for node in compiled.graph.nodes}
    by_source = defaultdict(int)
    for source, line, purpose in db.execute(select(RequisitionItemBomSource, RequisitionItem, PurchasePurposeSourceSnapshot)
            .join(RequisitionItem, RequisitionItem.id == RequisitionItemBomSource.requisition_item_id)
            .join(PurchasePurposeSourceSnapshot,
                PurchasePurposeSourceSnapshot.source_bom_requisition_source_id == RequisitionItemBomSource.id)
            .where(RequisitionItemBomSource.sales_order_item_bom_component_id.in_(links))):
        link = links[source.sales_order_item_bom_component_id]
        route = next((route for route in nodes[link.product_id].routes if route.key == source.component_type), None)
        if (line.order_item_id != order_id or purpose.source_order_item_id not in (None, order_id)
                or purpose.customer_id != compiled.graph.customer_id
                or purpose.material_requisition_item_id != line.id or route is None
                or purpose.component_type != route.key or purpose.yield_per_sheet_snapshot != route.pieces_per_sheet):
            raise BomPlanError("交接材料的订单、客户、物理片组或开料换算不一致")
        allocations = list(db.scalars(select(IncomingReceiptPurposeAllocation).where(
            IncomingReceiptPurposeAllocation.purchase_purpose_source_snapshot_id == purpose.id,
            IncomingReceiptPurposeAllocation.status == "posted")))
        received = sum(row.receipt_order_purpose_sheet_qty for row in allocations)
        if line.status in {"有效", "supplier_requisition_created"}:
            capacity = max(received, purpose.order_purpose_sheet_qty) if include_pending else received
        elif line.status in {"已入库", "已取消", "已作废", "已撤回"}:
            capacity = received
        else:
            raise BomPlanError(f"交接报料行#{line.id}状态{line.status}无法核定采购余量")
        by_source[link.source_snapshot_id, route.key] += capacity * route.pieces_per_sheet
    from app.services.multilevel_bom_output_history import completion_source_id, completion_material_source_id
    for completion in db.scalars(select(ProductionCompletion).where(
            ProductionCompletion.order_item_id == order_id, ProductionCompletion.status == "posted")):
        execution = completion_source_id(db, completion)
        if execution in current_ids:
            continue
        material = completion_material_source_id(db, completion)
        if material not in links:
            continue
        lot = db.get(InventoryLot, completion.inventory_lot_id)
        if lot is None:
            raise BomPlanError("历史材料完工缺少原产出成本依据")
        detail = json.loads(lot.cost_snapshot_detail_json or "{}")
        inputs = detail.get("bom_material_inputs")
        from app.services.multilevel_bom_body_inventory import carried_body_quantity
        body_quantity = carried_body_quantity(db, compiled, completion)
        if not isinstance(inputs, list) or not inputs:
            raise BomPlanError("历史材料完工缺少逐片消耗依据")
        for entry in inputs:
            if entry["kind"] in {"allocation", "current_receipt"}:
                if (type(entry.get("quantity")) is not int or entry["quantity"] <= 0
                        or type(entry.get("before")) is not int or entry["before"] < 0):
                    raise BomPlanError("历史材料消耗片数无效")
                if entry["kind"] == "allocation":
                    allocation = db.get(IncomingReceiptPurposeAllocation, entry["id"])
                else:
                    candidates = list(db.scalars(select(IncomingReceiptPurposeAllocation).where(
                        IncomingReceiptPurposeAllocation.production_completion_id == completion.id,
                        IncomingReceiptPurposeAllocation.purchase_purpose_source_snapshot_id == entry["id"])))
                    allocation = candidates[0] if len(candidates) == 1 else None
                source = db.get(RequisitionItemBomSource, allocation.source_bom_requisition_source_id) if allocation else None
                purpose = db.get(PurchasePurposeSourceSnapshot, allocation.purchase_purpose_source_snapshot_id) if allocation else None
                if (allocation is None or allocation.status != "posted" or source is None or purpose is None
                        or source.sales_order_item_bom_component_id != material
                        or source.component_type != entry["route"]
                        or entry["before"] + entry["quantity"] >
                            allocation.receipt_order_purpose_sheet_qty * purpose.yield_per_sheet_snapshot):
                    raise BomPlanError("历史材料消耗与原实收片数或来源不一致")
                historical = entry["quantity"] * (completion.quantity-body_quantity)
                if historical % completion.quantity:
                    raise BomPlanError("历史本体材料消耗不能按完整物理片数分摊")
                by_source[material, entry["route"]] -= historical // completion.quantity
            elif entry["kind"] != "reservation":
                raise BomPlanError("历史材料投入来源类型无效")
    result = defaultdict(int)
    for (source_id, route), quantity in by_source.items():
        if quantity < 0:
            raise BomPlanError("原报料材料不足以支持历史完工消耗，不能追加抵扣")
        result[links[source_id].product_id, route] += quantity
    return dict(result)
