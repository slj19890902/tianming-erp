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
    if lot.source_ref_type == "return_receipt_item":
        from app.services.bom_return_cost import return_graph_sources
        result = return_graph_sources(db, lot)
        if result is not None:
            quantity, rows = result
            used = lineage_used(db, lot)
            amount = sum((cost_slice(row["amount"], quantity, used, take) for row in rows), Decimal(0))
            return amount, {"actual": True, "currency": rows[0]["currency"], "sources": rows}
    if lot.source_ref_type == 'bom_external_receipt':
        from app.services.multilevel_bom_external_costs import external_lot_cost
        return external_lot_cost(db, lot, take)
    # A real assembled product can be an input of the next assembly level.
    # Carry its exact frozen total and purchase lineage forward; multiplying
    # its rounded display unit cost would lose both cents and cost provenance.
    if lot.source_ref_type in ("subkit_conversion", "bom_assembly"):
        return delivery_cost(db, lot, take)
    if lot.source_ref_type == "production_completion":
        from app.models.production import ProductionCompletion
        from app.models.warehouse_inventory import InventoryLot
        completion = db.get(ProductionCompletion, lot.source_ref_id)
        detail = json.loads(lot.cost_snapshot_detail_json or "{}")
        original = db.get(InventoryLot, completion.inventory_lot_id) if completion else None
        frozen = json.loads(original.cost_snapshot_detail_json or "{}") if original else {}
        if "bom_material_product_id" in detail or "bom_material_product_id" in frozen:
            from app.services.multilevel_bom_body_inventory import stock_product_identity
            identity = stock_product_identity(db, lot)
            original_identity = stock_product_identity(db, original)
            if (completion is None or completion.status != "posted" or original is None
                    or original.source_ref_type != lot.source_ref_type or original.source_ref_id != lot.source_ref_id
                    or lot.inventory_type != original.inventory_type
                    or identity[0] != frozen.get("bom_material_product_id")
                    or identity != original_identity
                    or detail != frozen):
                raise SubkitError("多级BOM完工成本身份不完整")
            used = lineage_used(db, lot)
            if used + take > completion.quantity:
                if lot.inventory_type == "assembly_body":
                    raise SubkitError("本体数量超出真实完工成本来源")
                return estimated_slice(lot, take)
            return cost_slice(Decimal(frozen["capitalized_material_cost"]), completion.quantity, used, take), frozen
    if lot.inventory_type == "assembly_body":
        raise SubkitError("本体库存缺少冻结完工成本，不能按估算组装")
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
    from app.services.inventory_valuation import require_inherited_entry_cost
    from app.services.warehouse_inventory import WarehouseInventoryError
    try:
        require_inherited_entry_cost(db, lot)
    except WarehouseInventoryError as error:
        raise SubkitError(str(error)) from error
    amount, evidence = estimated_slice(lot, take)
    return amount, dict(evidence, currency='CNY', reason='继承已核对入库参考成本，不认定新的实际采购')


def delivery_cost(db, lot, take):
    from app.models.multilevel_bom import BomAssembly
    model = {"subkit_conversion": SubkitConversion, "bom_assembly": BomAssembly}.get(lot.source_ref_type)
    conversion = db.get(model, lot.source_ref_id) if model else None
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
    from app.models.warehouse_inventory import InventoryLot, OrderedFinishedReceiptReturn
    extra = []
    if lot.source_ref_type == "return_receipt_item":
        # One receipt line can return multiple source lots, each with its own
        # exact cost slice. Moved descendants keep this frozen document.
        extra = [InventoryLot.cost_snapshot_detail_json == lot.cost_snapshot_detail_json]
    identity = [InventoryLot.source_ref_type == lot.source_ref_type,
                InventoryLot.source_ref_id == lot.source_ref_id, *extra]
    consumed = int(db.scalar(select(func.coalesce(func.sum(
        InventoryLot.quantity_consumed + InventoryLot.quantity_damaged + InventoryLot.quantity_scrapped), 0)).where(
        *identity)) or 0)
    # A short return moves a cost interval to a separately identified output.
    # Its original interval stays used, even before the returned lot is sent
    # again; otherwise a later debit of the source would reuse that interval.
    transferred = int(db.scalar(select(func.coalesce(func.sum(OrderedFinishedReceiptReturn.quantity), 0)).where(
        OrderedFinishedReceiptReturn.source_inventory_lot_id.in_(select(InventoryLot.id).where(*identity)),
        OrderedFinishedReceiptReturn.status == "active")) or 0)
    return consumed + transferred
