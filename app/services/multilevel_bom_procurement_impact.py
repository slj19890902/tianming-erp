"""Read existing obligations without cancelling or rebinding their sources."""
import hashlib
import json

from sqlalchemy import select

from app.models.product_bom import RequisitionItemBomSource, SalesOrderItemBomComponent
from app.models.requisition import RequisitionItem
from app.models.incoming_receipt import IncomingReceiptItem
from app.models.purchase_receipt import PurchaseReceiptFact
from app.services.multilevel_bom_cutover_review import _row
from app.services.multilevel_bom_plan import BomPlanError


def review_procurement_impact(db, *, order_item_id, customer_id):
    from app.services.multilevel_bom_rule_impact import review_current_rule_requirements
    from app.services.incoming_receipts import _target, source_summary, IncomingReceiptError
    rule = review_current_rule_requirements(db, order_item_id=order_item_id, customer_id=customer_id)
    from app.services.multilevel_bom_source_handoffs import current_source_handoffs
    handoffs = [_row(row) for row in current_source_handoffs(db, rule.previous)]
    handed_sources = {row["source_snapshot_id"] for row in handoffs}
    current = {row.id: row for row in rule.previous.snapshots}
    products = {row["product_id"]: row for row in rule.impact["products"]}
    paper = []
    for line in db.scalars(select(RequisitionItem).where(
            RequisitionItem.order_item_id == order_item_id).order_by(RequisitionItem.id)):
        sources = list(db.scalars(select(RequisitionItemBomSource).where(
            RequisitionItemBomSource.requisition_item_id == line.id).order_by(RequisitionItemBomSource.id)))
        mappings = []
        for source in sources:
            snapshot = db.get(SalesOrderItemBomComponent, source.sales_order_item_bom_component_id)
            if snapshot is None or snapshot.sales_order_item_id != order_item_id:
                raise BomPlanError(f"报料行#{line.id}的冻结来源跨订单或缺失")
            impact = products.get(snapshot.component_product_id)
            from app.services.multilevel_bom_orders import read_order_bom_source_contract
            from app.services.multilevel_bom_rule_impact import rule_quantity_impact
            original = read_order_bom_source_contract(db, order_item_id, snapshot.id)
            original_impact = next((row for row in rule_quantity_impact(original, rule.proposed,
                remaining_quantity=1)["products"] if row["product_id"] == snapshot.component_product_id), None)
            mappings.append(dict(source=_row(source), product_id=snapshot.component_product_id,
                current_source=snapshot.id in current,
                handed_to_current_source=snapshot.id in handed_sources,
                material_conversion_compatible=bool((snapshot.id in current or snapshot.id in handed_sources)
                    and original_impact and original_impact["material_conversion_compatible"]),
                proposed_gross_materials=impact["after"]["materials"] if impact and impact["after"] else []))
        receipts = list(db.scalars(select(IncomingReceiptItem).where(
            IncomingReceiptItem.requisition_item_id == line.id).order_by(IncomingReceiptItem.id)))
        costs = list(db.scalars(select(PurchaseReceiptFact).where(
            PurchaseReceiptFact.material_requisition_item_id == line.id).order_by(PurchaseReceiptFact.id)))
        try:
            summary = source_summary(db, _target(db, f"r{line.id}", allow_closed=True))
            summary_error = None
        except IncomingReceiptError as error:
            # Keep cancelled/historical lines visible without inventing a
            # balance when the canonical source reader cannot qualify them.
            summary, summary_error = None, str(error)
        paper.append(dict(line=_row(line), unit="张", mappings=mappings,
            receipt_summary=summary, receipt_summary_error=summary_error,
            receipts=[_row(row) for row in receipts], frozen_costs=[_row(row) for row in costs]))

    from app.models.external_packaging_purchase import (
        ExternalPackagingPurchaseItem, ExternalPackagingPurchaseOrder,
        ExternalPackagingReceiptItem, ExternalPackagingPurchaseCancellation)
    from app.services.external_receipt_state import active_receipt_item
    from app.services.multilevel_bom_external_identity import read_external_source_contract
    from app.services.multilevel_bom_purchase_units import cumulative_receipt_conversion
    external = []
    for line in db.scalars(select(ExternalPackagingPurchaseItem).where(
            ExternalPackagingPurchaseItem.sales_order_item_id == order_item_id)
            .order_by(ExternalPackagingPurchaseItem.id)):
        header = db.get(ExternalPackagingPurchaseOrder, line.purchase_order_id)
        link, contract = read_external_source_contract(db, line.order_component_id)
        if header is None or link.order_item_id != order_item_id or contract.graph.customer_id != customer_id:
            raise BomPlanError(f"采购行#{line.id}的客户或冻结来源不一致")
        receipts = list(db.scalars(select(ExternalPackagingReceiptItem).where(
            ExternalPackagingReceiptItem.purchase_item_id == line.id).order_by(ExternalPackagingReceiptItem.id)))
        active = set(db.scalars(select(ExternalPackagingReceiptItem.id).where(
            ExternalPackagingReceiptItem.purchase_item_id == line.id, active_receipt_item())))
        received = sum(row.received_quantity for row in receipts if row.id in active)
        cancellation = db.scalar(select(ExternalPackagingPurchaseCancellation).where(
            ExternalPackagingPurchaseCancellation.purchase_order_id == header.id))
        node = next(node for node in contract.graph.nodes if node.product_id == link.product_id)
        if line.purchase_unit != node.purchase_units.purchase_unit:
            raise BomPlanError(f"采购行#{line.id}的单位与原冻结换算不一致")
        remaining = max(line.purchase_quantity-received, 0) if cancellation is None else 0
        received_stock, received_remainder = cumulative_receipt_conversion(
            node, received_before=0, received_now=received)
        pending_stock, final_remainder = cumulative_receipt_conversion(
            node, received_before=received, received_now=remaining)
        impact = products.get(link.product_id)
        # A contract carried into this revision retains its original source ID.
        # Compare that original contract with the proposed rule, rather than
        # mistaking an explicitly validated historical source for an orphan.
        from app.services.multilevel_bom_rule_impact import rule_quantity_impact
        source_is_current = link.bom_snapshot_id in current
        source_is_handed = link.bom_snapshot_id in handed_sources
        contract_impact = next((row for row in rule_quantity_impact(
            contract, rule.proposed, remaining_quantity=1)["products"]
            if row["product_id"] == link.product_id), None)
        # These are source capacities, not an allocation or an authorization:
        # old contract prices, supplier and receipt identity stay unchanged.
        mapping = dict(source_snapshot_id=link.bom_snapshot_id,
            current_source=source_is_current,
            handed_to_current_source=source_is_handed,
            purchase_conversion_compatible=bool((source_is_current or source_is_handed)
                and contract_impact and contract_impact["purchase_conversion_compatible"]),
            stock_unit=node.unit, stock_basis=node.purchase_units.stock_basis,
            purchase_basis=node.purchase_units.purchase_basis,
            received_stock_capacity=received_stock, pending_stock_capacity=pending_stock,
            received_purchase_remainder=str(received_remainder),
            final_purchase_remainder=str(final_remainder),
            proposed_gross_purchase=impact["after"]["purchase"] if impact and impact["after"] else None)
        external.append(dict(line=_row(line), header=_row(header), product_id=link.product_id,
            unit=line.purchase_unit, received_quantity=received,
            remaining_quantity=remaining, mapping=mapping,
            cancellation=_row(cancellation) if cancellation else None,
            receipts=[_row(row) for row in receipts], active_receipt_ids=sorted(active)))
    document = json.dumps(dict(rule_document=rule.document, paper=paper, external=external, source_handoffs=handoffs),
        ensure_ascii=False, sort_keys=True, default=str)
    return dict(order_item_id=order_item_id, quantity_impact=rule.impact, paper=paper, external=external, source_handoffs=handoffs,
        evidence_hash=hashlib.sha256(document.encode()).hexdigest(), executable=False,
        scope="逐行采购与实收事实核对；新规则材料为毛需求，未分配旧采购，不可作为执行凭据")
