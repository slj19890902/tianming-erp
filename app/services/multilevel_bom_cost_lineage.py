"""Read exact multi-source material evidence without changing inventory or finance.

Each returned portion keeps its real receipt allocation and purchase fact.  A
single arbitrary receipt must never stand in for the cost of an assembly.
"""
import json
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select

from app.models.multilevel_bom import BomAssembly, BomAssemblyInput
from app.models.production import ProductionCompletion
from app.models.external_packaging_purchase import ExternalPackagingReceiptItem
from app.models.product_bom import RequisitionItemBomSource, SalesOrderItemBomComponent
from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot
from app.models.purchase_receipt import IncomingReceiptPurposeAllocation, PurchaseReceiptFact
from app.models.warehouse_inventory import InventoryLot
from app.services.bom_subkits import SubkitError
from app.services.bom_subkit_costs import cost_slice
from app.services.multilevel_bom_body_inventory import stock_product_identity

QUANTUM = Decimal("0.0001")


def graph_output_quantity(db, lot):
    if lot.source_ref_type == "return_receipt_item":
        from app.services.bom_return_cost import return_graph_sources
        result = return_graph_sources(db, lot)
        if result is None:
            raise SubkitError("退回批次缺少冻结实际成本")
        return result[0]
    if lot.source_ref_type == "bom_external_receipt":
        output = db.get(ExternalPackagingReceiptItem, lot.source_ref_id)
        return output.converted_finished_quantity
    model = BomAssembly if lot.source_ref_type == "bom_assembly" else ProductionCompletion
    return db.get(model, lot.source_ref_id).quantity


def graph_material_cost_slice(db, lot, *, used, take):
    """Explicit source-output offset; independent of mutable current balances."""
    rows = graph_material_sources(db, lot)
    if rows is None:
        return None
    quantity = graph_output_quantity(db, lot)
    total = sum((r["amount"] for r in rows), Decimal(0))
    # Validate the whole interval before calculating either endpoint.
    cost_slice(total, quantity, used, take)
    # Round each real receipt cumulatively, then sum the portions. Re-apportioning
    # a rounded aggregate at both endpoints can produce a negative tiny portion.
    return [{**row, "amount": cost_slice(row["amount"], quantity, used, take)}
            for row in rows]


def _amount(value):
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise SubkitError("组套材料成本金额无效")
    return amount


def _scale(rows, total):
    """Cumulative allocation preserves every 0.0001, including the last slice."""
    denominator = sum((r["amount"] for r in rows), Decimal(0))
    if denominator == 0:
        if total != 0:
            raise SubkitError("组套材料成本来源合计不一致")
        return rows
    cumulative = Decimal(0)
    previous = Decimal(0)
    result = []
    for row in rows:
        cumulative += row["amount"]
        boundary = (total * cumulative / denominator).quantize(QUANTUM, rounding=ROUND_HALF_UP)
        result.append({**row, "amount": boundary - previous})
        previous = boundary
    return result


def graph_material_sources(db, lot, *, _visited=frozenset()):
    """Return full frozen output cost portions, or None for estimated inputs.

    This is not the remaining lot balance. Moved/split descendants retain the
    original output's quantity and evidence. Dispatch slicing is a separate step.
    """
    if lot.source_ref_type == "return_receipt_item":
        from app.services.bom_return_cost import return_graph_sources
        result = return_graph_sources(db, lot)
        return result[1] if result is not None else None
    identity = (lot.source_ref_type, lot.source_ref_id)
    if identity in _visited:
        raise SubkitError("组套材料成本来源循环")
    visited = _visited | {identity}
    if lot.source_ref_type == "bom_external_receipt":
        from app.services.multilevel_bom_external_costs import validated_external_lot_detail
        detail = validated_external_lot_detail(db, lot)
        return [{"external_receipt_item_id": row["external_receipt_item_id"],
                 "amount": _amount(row["amount"]), "currency": detail["currency"],
                 "tax_included": detail["tax_included"], "tax_rate": _amount(detail["tax_rate"])}
                for row in detail["sources"]]
    if lot.source_ref_type == "bom_assembly":
        assembly = db.get(BomAssembly, lot.source_ref_id)
        original = db.get(InventoryLot, assembly.output_lot_id) if assembly else None
        if assembly is None or assembly.status != "posted" or original is None:
            raise SubkitError("组套材料成本产出来源无效")
        identity = _same_output(db, lot, original)
        if assembly.output_product_id != identity[0]:
            raise SubkitError("组套材料产出产品不一致")
        detail = json.loads(assembly.cost_detail_json or "{}")
        inputs = list(db.scalars(select(BomAssemblyInput).where(
            BomAssemblyInput.conversion_id == assembly.id).order_by(BomAssemblyInput.id)))
        frozen = {r["lot_id"]: r for r in detail.get("sources", [])}
        if not inputs or len(frozen) != len(inputs):
            raise SubkitError("组套材料成本投入来源不完整")
        rows = []
        for source in inputs:
            evidence = frozen.get(source.lot_id)
            child = db.get(InventoryLot, source.lot_id)
            child_identity = stock_product_identity(db, child)
            if (evidence is None or child is None
                    or child_identity[0] != source.product_id
                    or child_identity[1] != identity[1]
                    or evidence["quantity"] != source.quantity
                    or _amount(evidence["cost"]) != source.total_cost):
                raise SubkitError("组套材料成本投入身份不一致")
            portions = graph_material_sources(db, child, _visited=visited)
            if portions is None:
                return None
            rows.extend(_scale(portions, _amount(source.total_cost)))
        if sum((r["amount"] for r in rows), Decimal(0)) != assembly.total_cost:
            raise SubkitError("组套材料成本投入产出不守恒")
        if len({r["currency"] for r in rows}) != 1:
            raise SubkitError("组套材料采购币种不一致")
        return rows
    if lot.source_ref_type != "production_completion":
        return None
    completion = db.get(ProductionCompletion, lot.source_ref_id)
    original = db.get(InventoryLot, completion.inventory_lot_id) if completion else None
    if completion is None or completion.status != "posted" or original is None:
        raise SubkitError("组套材料完工成本来源无效")
    identity = _same_output(db, lot, original)
    detail = json.loads(original.cost_snapshot_detail_json or "{}")
    if "bom_material_product_id" not in detail:
        return None
    if detail["bom_material_product_id"] != identity[0]:
        raise SubkitError("组套材料完工产品身份不一致")
    rows = []
    for source in detail.get("bom_material_inputs", []):
        if not source.get("actual"):
            return None
        if source["kind"] == "current_receipt":
            allocations = list(db.scalars(select(IncomingReceiptPurposeAllocation).where(
                IncomingReceiptPurposeAllocation.production_completion_id == completion.id,
                IncomingReceiptPurposeAllocation.purchase_purpose_source_snapshot_id == source["id"])))
            if len(allocations) != 1:
                raise SubkitError("组套材料当前收料来源不唯一")
            allocation = allocations[0]
        elif source["kind"] == "allocation":
            allocation = db.get(IncomingReceiptPurposeAllocation, source["id"])
        else:
            return None
        fact = db.get(PurchaseReceiptFact, allocation.purchase_receipt_fact_id) if allocation else None
        if allocation is None or allocation.status != "posted" or fact is None:
            raise SubkitError("组套材料采购事实无效")
        bom_source = db.get(RequisitionItemBomSource, allocation.source_bom_requisition_source_id) if allocation.source_bom_requisition_source_id else None
        snapshot = db.get(SalesOrderItemBomComponent, bom_source.sales_order_item_bom_component_id) if bom_source else None
        purpose = db.get(PurchasePurposeSourceSnapshot, allocation.purchase_purpose_source_snapshot_id)
        if (snapshot is None or purpose is None
                or allocation.customer_id != identity[1]
                or snapshot.sales_order_item_id != completion.order_item_id
                or snapshot.component_product_id != identity[0]
                or snapshot.id != detail["bom_snapshot_id"]):
            raise SubkitError("组套材料采购产品或客户不一致")
        expected = cost_slice(allocation.order_purpose_cost,
            allocation.receipt_order_purpose_sheet_qty * int(purpose.yield_per_sheet_snapshot),
            source["before"], source["quantity"])
        if expected != _amount(source["total_cost"]):
            raise SubkitError("组套材料采购成本分摊不一致")
        rows.append({"allocation_id": allocation.id, "purchase_receipt_fact_id": fact.id,
                     "amount": _amount(source["total_cost"]), "currency": fact.currency,
                     "tax_included": fact.tax_included, "tax_rate": fact.tax_rate})
    if not rows or sum((r["amount"] for r in rows), Decimal(0)) != _amount(detail["capitalized_material_cost"]):
        raise SubkitError("组套材料完工成本不守恒")
    return rows


def _same_output(db, lot, original):
    identity = stock_product_identity(db, lot)
    original_identity = stock_product_identity(db, original)
    if (lot.source_ref_type != original.source_ref_type or lot.source_ref_id != original.source_ref_id
            or lot.inventory_type != original.inventory_type
            or identity != original_identity
            or json.loads(lot.cost_snapshot_detail_json or "{}") != json.loads(original.cost_snapshot_detail_json or "{}")):
        raise SubkitError("组套材料批次成本身份不一致")
    return identity
