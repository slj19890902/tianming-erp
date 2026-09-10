"""Identify which immutable rule produced stock before granting own-output credit."""
import json

from sqlalchemy import select

from app.models.production import ProductionCompletion, ProductionTask
from app.models.warehouse_inventory import InventoryLot
from app.models.multilevel_bom import BomAssembly
from app.models.external_packaging_purchase import ExternalPackagingReceiptItem, ExternalPackagingPurchaseItem
from app.services.multilevel_bom_plan import BomPlanError


def current_finished_reservation_condition(db, compiled):
    from app.services.multilevel_bom_delivery_history import root_reservation_source_expression
    root = next(row for row in compiled.snapshots if row.component_product_id == compiled.graph.root_id)
    return root_reservation_source_expression(db, {root.sales_order_item_id: root.id}).in_(
        [row.id for row in compiled.snapshots])


def completion_source_id(db, completion):
    task = db.get(ProductionTask, completion.task_id)
    lot = db.get(InventoryLot, completion.inventory_lot_id) if completion.inventory_lot_id else None
    if task is None or task.order_item_id != completion.order_item_id:
        raise BomPlanError("完工任务与订单身份不一致")
    sid = task.sales_order_item_bom_component_id
    if lot is not None:
        if lot.source_ref_type != "production_completion" or lot.source_ref_id != completion.id:
            raise BomPlanError("完工原批次来源不一致")
        try:
            detail = json.loads(lot.cost_snapshot_detail_json or "{}")
            recorded = detail.get("bom_snapshot_id")
        except (ValueError, AttributeError) as error:
            raise BomPlanError("完工原批次冻结来源无效") from error
        if recorded is not None:
            if type(recorded) is not int or recorded <= 0 or (sid is not None and sid != recorded):
                raise BomPlanError("完工任务与冻结BOM来源不一致")
            sid = recorded
    if sid is None:
        raise BomPlanError("跨版本完工缺少准确BOM来源，不能计入当前产出")
    from app.services.multilevel_bom_orders import read_order_bom_source_contract
    contract = read_order_bom_source_contract(db, completion.order_item_id, sid)
    source = next(row for row in contract.snapshots if row.id == sid)
    if lot is not None and lot.finished_detail is not None and (
            lot.finished_detail.product_id != source.component_product_id
            or lot.finished_detail.owner_customer_id != contract.graph.customer_id):
        raise BomPlanError("完工批次产品或客户与冻结来源不一致")
    if lot is not None and lot.inventory_type == "assembly_body":
        from app.models.multilevel_bom import BomBodyInventoryDetail
        body = db.get(BomBodyInventoryDetail, lot.id)
        if (body is None or body.production_completion_id != completion.id
                or body.order_item_id != completion.order_item_id or body.product_id != source.component_product_id):
            raise BomPlanError("待装配本体与冻结完工来源不一致")
    return sid


def _external_source(db, receipt_item_id, order_item_id):
    receipt = db.get(ExternalPackagingReceiptItem, receipt_item_id)
    purchase = db.get(ExternalPackagingPurchaseItem, receipt.purchase_item_id) if receipt else None
    from app.services.multilevel_bom_external_identity import read_external_source_contract
    link = read_external_source_contract(db, purchase.order_component_id)[0] if purchase else None
    if link is None or link.order_item_id != order_item_id or purchase.sales_order_item_id != order_item_id:
        raise BomPlanError("外购产出缺少准确订单BOM来源")
    return link.bom_snapshot_id


def output_source_ids(db, lot, order_item_id):
    if lot.source_ref_type == "production_completion":
        completion = db.get(ProductionCompletion, lot.source_ref_id)
        if completion is None or completion.order_item_id != order_item_id:
            raise BomPlanError("产出完工不属于本订单")
        return {completion_source_id(db, completion)}
    if lot.source_ref_type == "bom_external_receipt":
        return {_external_source(db, lot.source_ref_id, order_item_id)}
    assembly = db.get(BomAssembly, lot.source_ref_id) if lot.source_ref_type == "bom_assembly" else None
    if assembly is None or assembly.order_item_id != order_item_id:
        raise BomPlanError("组装产出缺少原转换身份")
    try:
        operation = json.loads(assembly.cost_detail_json)["graph_operation"]
        key = operation["key"]
        if "source_ids" in operation:
            ids = operation["source_ids"]
            if (not isinstance(ids, list) or not ids or len(set(ids)) != len(ids)
                    or any(type(sid) is not int or sid <= 0 for sid in ids)):
                raise ValueError("invalid source identities")
            return set(ids)
        if key.startswith("bom-receipt:"):
            from app.models.purchase_receipt import IncomingReceiptPurposeAllocation
            from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot
            from app.models.product_bom import RequisitionItemBomSource
            allocation = db.get(IncomingReceiptPurposeAllocation, int(key.removeprefix("bom-receipt:")))
            purpose = db.get(PurchasePurposeSourceSnapshot, allocation.purchase_purpose_source_snapshot_id) if allocation else None
            source = db.get(RequisitionItemBomSource, purpose.source_bom_requisition_source_id) if purpose else None
            if source is not None:
                return {source.sales_order_item_bom_component_id}
        elif key.startswith("bom-external-receipt:"):
            prefix, rid, oid = key.split(":")
            if int(oid) != order_item_id:
                raise ValueError("order identity differs")
            ids = list(db.scalars(select(ExternalPackagingReceiptItem.id).join(ExternalPackagingPurchaseItem,
                ExternalPackagingPurchaseItem.id == ExternalPackagingReceiptItem.purchase_item_id).where(
                    ExternalPackagingReceiptItem.receipt_id == int(rid),
                    ExternalPackagingPurchaseItem.sales_order_item_id == order_item_id)))
            if ids:
                return {_external_source(db, rid, order_item_id) for rid in ids}
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise BomPlanError("组装产出原操作来源无效") from error
    raise BomPlanError("跨版本组装产出缺少准确收料来源，须显式交接该批次")


def current_output_lots(db, compiled, lots):
    if compiled.rule_revision_id is None:
        return lots
    from app.services.multilevel_bom_orders import read_order_bom_source_contract
    order_item_id = compiled.snapshots[0].sales_order_item_id
    current_ids = {row.id for row in compiled.snapshots}
    result = []
    for lot in lots:
        ids = output_source_ids(db, lot, order_item_id)
        contract = read_order_bom_source_contract(db, order_item_id, min(ids))
        if not ids.issubset({row.id for row in contract.snapshots}):
            raise BomPlanError("同一产出批次混用了不同规则来源")
        if lot.finished_detail is not None:
            from app.services.finished_stock_identity import compiled_product_bases
            expected_product = (db.get(BomAssembly, lot.source_ref_id).output_product_id
                if lot.source_ref_type == "bom_assembly" else
                next(row.component_product_id for row in contract.snapshots if row.id == min(ids)))
            if (lot.finished_detail.product_id != expected_product
                    or lot.finished_detail.owner_customer_id != contract.graph.customer_id
                    or lot.finished_detail.physical_basis_json != compiled_product_bases(contract)[expected_product]):
                raise BomPlanError("产出批次产品、客户或规格工艺与原冻结版本不一致")
        if ids.issubset(current_ids):
            result.append(lot)
    return result
