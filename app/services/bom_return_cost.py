"""A returned slice keeps the original dispatched receipt-cost evidence."""
import json

from sqlalchemy import select

from app.models.graph_material_cost import FinanceDeliveryGraphCostFact as Fact, FinanceDeliveryGraphCostPortion as Portion
from app.models.warehouse_inventory import InventoryLot, OrderedFinishedReceiptReturn as Return, InventoryMovement
from app.services.bom_subkits import SubkitError
from app.services.bom_subkit_costs import cost_slice


def _interval(db, returned, fact):
    movement = db.get(InventoryMovement, returned.source_reverse_movement_id)
    expected_fact = db.scalar(select(Fact.id).where(
        Fact.delivery_inventory_allocation_id == returned.delivery_inventory_allocation_id,
        Fact.consume_movement_id < returned.source_reverse_movement_id)
        .order_by(Fact.consume_movement_id.desc(), Fact.snapshot_version.desc()).limit(1))
    if (movement is None or movement.inventory_lot_id != fact.inventory_lot_id
            or expected_fact != fact.id
            or movement.before_consumed-movement.after_consumed != returned.quantity
            or returned.delivery_inventory_allocation_id != fact.delivery_inventory_allocation_id
            or returned.source_inventory_lot_id != fact.inventory_lot_id
            or returned.delivery_item_id != fact.delivery_item_id
            or movement.id <= fact.consume_movement_id):
        raise SubkitError("退回成本与原发货冲回来源不一致")
    # Reconstruct the reversals that existed at this movement, including a
    # previously returned then cancelled receipt. Later edits cannot shift it.
    earlier = db.scalars(select(Return).where(
        Return.delivery_inventory_allocation_id == returned.delivery_inventory_allocation_id,
        Return.source_reverse_movement_id > fact.consume_movement_id,
        Return.source_reverse_movement_id <= movement.id))
    reversed_quantity = sum(row.quantity for row in earlier
        if row.source_reconsume_movement_id is None or row.source_reconsume_movement_id > movement.id)
    offset = fact.source_offset + fact.consumed_quantity - reversed_quantity
    if offset < fact.source_offset or offset+returned.quantity > fact.source_offset+fact.consumed_quantity:
        raise SubkitError("退回数量超出原发货成本份额")
    return offset


def freeze_return_graph_cost(db, *, returned, lot, allocation):
    fact = db.scalar(select(Fact).where(Fact.delivery_inventory_allocation_id == allocation.id,
        Fact.consume_movement_id == allocation.consume_movement_id).order_by(Fact.snapshot_version.desc()))
    if fact is None:
        return  # An estimated original delivery is never promoted to actual.
    detail = json.loads(lot.cost_snapshot_detail_json or "{}")
    detail["bom_return_cost"] = dict(fact_id=fact.id, return_lot_id=lot.id,
        quantity=returned.quantity, offset=_interval(db, returned, fact))
    lot.cost_snapshot_detail_json = json.dumps(detail, ensure_ascii=False, sort_keys=True)


def return_graph_sources(db, lot):
    data = json.loads(lot.cost_snapshot_detail_json or "{}").get("bom_return_cost")
    if data is None:
        return None
    if (lot.source_ref_type != "return_receipt_item" or type(data) is not dict
            or set(data) != {"fact_id", "return_lot_id", "quantity", "offset"}
            or any(type(value) is not int for value in data.values())):
        raise SubkitError("退回成本冻结内容无效")
    returned = db.scalar(select(Return).where(Return.return_inventory_lot_id == data["return_lot_id"]))
    fact = db.get(Fact, data["fact_id"])
    original = db.get(InventoryLot, data["return_lot_id"])
    source = db.get(InventoryLot, fact.inventory_lot_id) if fact else None
    if (returned is None or fact is None or original is None
            or returned.return_receipt_item_id != lot.source_ref_id
            or returned.quantity != data["quantity"] or data["offset"] != _interval(db, returned, fact)
            or original.cost_snapshot_detail_json != lot.cost_snapshot_detail_json
            or lot.finished_detail is None or original.finished_detail is None
            or source is None or source.finished_detail is None
            or lot.finished_detail.product_id != original.finished_detail.product_id
            or lot.finished_detail.owner_customer_id != original.finished_detail.owner_customer_id
            or lot.finished_detail.physical_basis_json != original.finished_detail.physical_basis_json
            or lot.finished_detail.product_id != source.finished_detail.product_id
            or lot.finished_detail.owner_customer_id != source.finished_detail.owner_customer_id
            or lot.finished_detail.physical_basis_json != source.finished_detail.physical_basis_json):
        raise SubkitError("退回批次与原发货成本身份不一致")
    from app.services.graph_delivery_cost import cost_portions
    portions = cost_portions(db,[fact.id])
    if not portions or sum(row.charged_cost for row in portions) != fact.total_cost:
        raise SubkitError("退回成本缺少原采购份额")
    rows = [dict(purchase_receipt_fact_id=row.purchase_receipt_fact_id,
        allocation_id=row.purpose_allocation_id, external_receipt_item_id=row.external_receipt_item_id,
        raw_receipt_allocation_id=getattr(row,'raw_receipt_allocation_id',None),
        amount=cost_slice(row.full_output_cost, fact.source_quantity, data["offset"], data["quantity"]),
        currency=fact.currency, tax_included=row.tax_included, tax_rate=row.tax_rate) for row in portions]
    return data["quantity"], rows
