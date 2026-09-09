"""Exact proportional cost slices and frozen multi-source purchase lineage."""
import json
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select, func

from app.models.bom_subkit import SubkitConversion, SubkitConversionInput, SubkitReceiptOutput
from app.services.bom_subkits import SubkitError


def cost_slice(total, quantity, used, take):
    if quantity <= 0 or min(used, take) < 0 or used + take > quantity:
        raise SubkitError("库存数量已超出来源成本范围，请先核对成本")
    def cumulative(q):
        return (Decimal(total) * q / quantity).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return cumulative(used + take) - cumulative(used)


def source_cost(db, lot, take):
    # A real assembled product can be an input of the next assembly level.
    # Carry its exact frozen total and purchase lineage forward; multiplying
    # its rounded display unit cost would lose both cents and cost provenance.
    if lot.source_ref_type == "subkit_conversion":
        return delivery_cost(db, lot, take)
    source = db.get(SubkitReceiptOutput, lot.source_ref_id) if lot.source_ref_type == "subkit_receipt" else None
    if source:
        from app.models.purchase_receipt import IncomingReceiptPurposeAllocation, PurchaseReceiptFact
        allocation = db.get(IncomingReceiptPurposeAllocation, source.allocation_id)
        fact = db.get(PurchaseReceiptFact, allocation.purchase_receipt_fact_id)
        if fact is None:
            raise SubkitError("原片缺少冻结采购成本来源")
        used = lineage_used(db, lot)
        if used + take > source.quantity:
            return estimated_slice(lot, take)
        amount = cost_slice(source.total_cost, source.quantity, used, take)
        return amount, {"currency": fact.currency, "actual": True,
                        "purchase_receipt_fact_id": fact.id, "allocation_id": allocation.id}
    if lot.estimated_unit_cost_snapshot is None or lot.estimated_unit_cost_snapshot < 0:
        raise SubkitError("组套原片缺少有效来源成本")
    # Legacy stock is not promoted to actual purchase cost merely by assembling it.
    return estimated_slice(lot, take)


def delivery_cost(db, lot, take):
    conversion = db.get(SubkitConversion, lot.source_ref_id) if lot.source_ref_type == "subkit_conversion" else None
    if conversion is None or conversion.status != "posted":
        raise SubkitError("内衬缺少组套成本来源")
    used = lineage_used(db, lot)
    if used + take > conversion.quantity:
        return estimated_slice(lot, take)
    amount = cost_slice(conversion.total_cost, conversion.quantity, used, take)
    detail = json.loads(conversion.cost_detail_json or "{}")
    return amount, detail


def estimated_slice(lot, take):
    if lot.estimated_unit_cost_snapshot is None or lot.estimated_unit_cost_snapshot < 0:
        raise SubkitError("库存缺少有效成本")
    amount = (lot.estimated_unit_cost_snapshot * take).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return amount, {"currency": "", "actual": False, "lot_id": lot.id,
                    "reason": "库存超出冻结采购来源或仅有估算成本"}


def lineage_used(db, lot):
    from app.models.warehouse_inventory import InventoryLot
    return int(db.scalar(select(func.coalesce(func.sum(
        InventoryLot.quantity_consumed + InventoryLot.quantity_damaged + InventoryLot.quantity_scrapped), 0)).where(
        InventoryLot.source_ref_type == lot.source_ref_type, InventoryLot.source_ref_id == lot.source_ref_id)) or 0)
