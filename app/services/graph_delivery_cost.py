"""Freeze multi-source material cost atomically with an actual inventory debit."""
import hashlib
import json
from decimal import Decimal
from sqlalchemy import select

from app.models.graph_material_cost import FinanceDeliveryGraphCostFact as Fact, FinanceDeliveryGraphCostPortion as Portion
from app.models.delivery import Delivery, DeliveryItem
from app.models.warehouse_inventory import InventoryMovement, InventoryReservation
from app.services.bom_subkit_costs import lineage_used, cost_slice
from app.services.bom_subkits import SubkitError
from app.services.multilevel_bom_cost_lineage import graph_material_sources, graph_output_quantity


def is_graph_output(lot):
    source_type = getattr(lot, "source_ref_type", None)
    if source_type == "return_receipt_item":
        return "bom_return_cost" in json.loads(lot.cost_snapshot_detail_json or "{}")
    return source_type in ("bom_assembly", "bom_external_receipt") or (
        source_type == "production_completion"
        and any(key in json.loads(lot.cost_snapshot_detail_json or "{}") for key in ('bom_material_product_id', 'component_processing_confirmation')))


def freeze_graph_delivery_cost(db, *, allocation, lot, operator_id, unordered=False):
    quantity = int(allocation.consumed_quantity if unordered else allocation.consumed_stock_quantity)
    line = db.get(DeliveryItem, allocation.delivery_item_id)
    delivery = db.get(Delivery, line.delivery_id) if line else None
    movement = db.get(InventoryMovement, allocation.consume_movement_id)
    source_lot_id = allocation.inventory_lot_id if unordered else db.get(InventoryReservation, allocation.reservation_id).inventory_lot_id
    if (quantity <= 0 or source_lot_id != lot.id or not lot.finished_detail or delivery is None
            or delivery.customer_id != lot.finished_detail.owner_customer_id
            or movement is None or movement.inventory_lot_id != lot.id or movement.quantity != quantity
            or movement.movement_type != "consume" or movement.related_delivery_id != delivery.id):
        raise SubkitError("发货成本库存身份不完整")
    column = Fact.unordered_allocation_id if unordered else Fact.delivery_inventory_allocation_id
    latest = db.scalar(select(Fact).where(column == allocation.id).order_by(Fact.snapshot_version.desc()).limit(1))
    if latest and latest.consume_movement_id == movement.id:
        if latest.consumed_quantity != quantity or latest.inventory_lot_id != lot.id:
            raise SubkitError("发货成本重复请求身份不一致")
        return latest
    rows = graph_material_sources(db, lot)
    if rows is None:
        return None
    output_quantity = graph_output_quantity(db, lot)
    offset = lineage_used(db, lot) - quantity
    if offset < 0 or offset + quantity > output_quantity:
        # Counts above the frozen output remain an explicit accounting gap.
        return None
    currencies = {str(r["currency"]).upper() for r in rows}
    if len(currencies) != 1 or len(next(iter(currencies))) != 3:
        raise SubkitError("发货成本币种来源不完整")
    amounts = [cost_slice(r["amount"], output_quantity, offset, quantity) for r in rows]
    payload = {"movement": movement.id, "lot": lot.id, "source_quantity": output_quantity,
               "offset": offset, "quantity": quantity, "sources": rows, "amounts": amounts}
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    fact = Fact(delivery_item_id=line.id, inventory_lot_id=lot.id, consume_movement_id=movement.id,
        delivery_inventory_allocation_id=None if unordered else allocation.id,
        unordered_allocation_id=allocation.id if unordered else None,
        snapshot_version=latest.snapshot_version + 1 if latest else 1,
        source_quantity=output_quantity, source_offset=offset, consumed_quantity=quantity,
        total_cost=sum(amounts, Decimal(0)), currency=next(iter(currencies)),
        source_fingerprint=fingerprint, created_by=operator_id)
    db.add(fact)
    db.flush()
    for index, (row, amount) in enumerate(zip(rows, amounts)):
        if row.get('raw_receipt_allocation_id'):
            from app.models.raw_purchase_plan import RawPurchaseDeliveryCostPortion
            db.add(RawPurchaseDeliveryCostPortion(fact_id=fact.id,ordinal=index,raw_receipt_allocation_id=row['raw_receipt_allocation_id'],
                full_output_cost=row['amount'],charged_cost=amount,tax_included=row['tax_included'],tax_rate=row['tax_rate']))
            continue
        db.add(Portion(fact_id=fact.id, ordinal=index, purchase_receipt_fact_id=row.get("purchase_receipt_fact_id"),
            purpose_allocation_id=row.get("allocation_id"), external_receipt_item_id=row.get("external_receipt_item_id"),
            full_output_cost=row["amount"], charged_cost=amount,
            tax_included=row["tax_included"], tax_rate=row["tax_rate"]))
    db.flush()
    return fact


def cost_portions(db,fact_ids):
    from app.models.raw_purchase_plan import RawPurchaseDeliveryCostPortion
    if not fact_ids:return []
    rows=list(db.scalars(select(Portion).where(Portion.fact_id.in_(fact_ids))))
    rows+=list(db.scalars(select(RawPurchaseDeliveryCostPortion).where(RawPurchaseDeliveryCostPortion.fact_id.in_(fact_ids))))
    return sorted(rows,key=lambda row:(row.fact_id,row.ordinal))


def graph_cost_report_sources(db, delivery_item_ids):
    facts = list(db.scalars(select(Fact).where(Fact.delivery_item_id.in_(delivery_item_ids)).order_by(Fact.id))) if delivery_item_ids else []
    portions = {}
    for row in cost_portions(db,[f.id for f in facts]):
        portions.setdefault(row.fact_id, []).append(row)
    return {("unordered_inventory_allocation" if f.unordered_allocation_id else "inventory_allocation",
             f.unordered_allocation_id or f.delivery_inventory_allocation_id): (f, portions.get(f.id, [])) for f in facts}


def active_graph_cost(fact, portions, active_quantity):
    if not portions or sum((p.charged_cost for p in portions), Decimal(0)) != fact.total_cost:
        raise SubkitError("发货成本冻结明细不完整")
    if not 0 <= active_quantity <= fact.consumed_quantity:
        raise SubkitError("发货成本有效数量不一致")
    return sum((cost_slice(p.full_output_cost, fact.source_quantity, fact.source_offset, active_quantity)
                for p in portions), Decimal(0))
