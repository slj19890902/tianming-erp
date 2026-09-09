"""Order-owned amendment chain. Caller owns API permissions and final commit."""
from sqlalchemy import select

from app.models.multilevel_bom import OrderBomProductionRevision
from app.models.order import OrderItem
from app.services.bom_transactions import atomic_bom
from app.services.multilevel_bom_plan import BomPlanError
from app.services.multilevel_bom_production_revision import (
    FIELDS, apply_production_revision, prepare_production_revision,
)


def order_production_values(item):
    mapped = dict(material_id="material_id", material="snapshot_material",
        supplier_name="snapshot_supplier_name", layer_count="layer_count", flute_type="flute_type",
        production_notes="snapshot_production_notes", default_cutting_mode="special_process")
    return {field: getattr(item, mapped.get(field, "snapshot_" + field)) for field in FIELDS}


def production_revisions(db, order_item_id):
    return tuple(db.scalars(select(OrderBomProductionRevision).where(
        OrderBomProductionRevision.order_item_id == order_item_id).order_by(OrderBomProductionRevision.revision)))


def production_revisions_by_order_ids(db, order_item_ids):
    from collections import defaultdict
    grouped = defaultdict(list)
    if order_item_ids:
        for row in db.scalars(select(OrderBomProductionRevision).where(
                OrderBomProductionRevision.order_item_id.in_(order_item_ids)).order_by(
                    OrderBomProductionRevision.order_item_id, OrderBomProductionRevision.revision)):
            grouped[row.order_item_id].append(row)
    return grouped


def project_complete_order_material_rows(db, rows):
    """Batch adapter for complete order snapshots, never partial historical rows."""
    from collections import defaultdict
    from app.models.multilevel_bom import OrderBomGraph, OrderBomGraphProduct
    from app.models.order import Order
    from app.services.multilevel_bom_orders import validate_order_graph_rows, validate_compiled_order_rows
    groups = defaultdict(list)
    for row in rows:
        if (getattr(row, "snapshot_schema_version", 0) or 0) >= 5:
            groups[row.sales_order_item_id].append(row)
    if not groups:
        return rows
    identities = defaultdict(set)
    for row in db.scalars(select(OrderBomGraphProduct).where(OrderBomGraphProduct.order_item_id.in_(groups))):
        identities[row.order_item_id].add((row.product_id, row.product_version))
    revisions = production_revisions_by_order_ids(db, groups)
    projected = {}
    graph_rows = db.execute(select(OrderBomGraph, OrderItem, Order).join(
        OrderItem, OrderItem.id == OrderBomGraph.order_item_id).join(Order, Order.id == OrderItem.order_id)
        .where(OrderBomGraph.order_item_id.in_(groups))).all()
    if len(graph_rows) != len(groups):
        raise BomPlanError("BOM生产资料缺少原冻结图")
    for graph_row, item, order in graph_rows:
        graph = validate_order_graph_rows(graph_row, item, order, identities[item.id])
        compiled = validate_compiled_order_rows(graph, tuple(groups[item.id]))
        compiled = project_production_versions(compiled, revisions[item.id])
        projected.update((row.id, row) for row in compiled.snapshots)
    return [projected.get(row.id, row) for row in rows]


def project_production_versions(compiled, revisions):
    previous = None
    item_ids = {row.sales_order_item_id for row in compiled.snapshots}
    for number, row in enumerate(revisions, 1):
        if row.revision != number or row.previous_id != previous or item_ids != {row.order_item_id}:
            raise BomPlanError("BOM生产资料版本链不完整")
        compiled = apply_production_revision(compiled, row.document_json, expected_hash=row.content_hash)
        for snapshot in compiled.snapshots:
            snapshot.production_revision = row.revision
        previous = row.id
    return compiled


def append_order_production_revision(db, *, order_item_id, changes, expected_revision, actor):
    from app.models.requisition import RequisitionItem
    from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem
    from app.models.warehouse_inventory import InventoryReservation
    from app.services.multilevel_bom_orders import read_compiled_order_bom
    from app.services.production_workflow import lock_order_rows_for_production_transition, has_production_completion_facts
    from app.services.audit_log import append_audit_event

    if type(expected_revision) is not int or expected_revision < 0:
        raise BomPlanError("修改BOM生产资料须提供当前版本，请刷新订单")
    if actor is None or not actor.is_active:
        raise BomPlanError("操作人已失效")
    with atomic_bom(db):
        item = db.get(OrderItem, order_item_id)
        if item is None:
            raise BomPlanError("订单明细不存在")
        order = lock_order_rows_for_production_transition(db, [item.order_id])[item.order_id]
        if (item.is_force_closed or int(item.delivered_quantity or 0) > 0
                or item.material_status == "received" or item.requisition_status != "未报料"
                or order.status in ("cancelled", "closed", "dead", "completed", "archived", "delivered")
                or has_production_completion_facts(db, [item.id])):
            raise BomPlanError("订单已有报料、收料或生产送货事实，不能修订BOM生产资料")
        # Historical procurement/stock references require their own controlled
        # reversal proof. Current zero balances alone are not that proof.
        if any(db.scalar(select(model.id).where(model.order_item_id == item.id).limit(1)) is not None
               for model in (RequisitionItem, InventoryReservation)) or db.scalar(
                   select(ExternalPackagingPurchaseItem.id).where(
                       ExternalPackagingPurchaseItem.sales_order_item_id == item.id).limit(1)) is not None:
            raise BomPlanError("订单已有采购或库存来源记录，须先核对历史链路再修订生产资料")
        revisions = production_revisions(db, item.id)
        if len(revisions) != expected_revision:
            raise BomPlanError("BOM生产资料已更新，请刷新后重新保存")
        compiled = read_compiled_order_bom(db, item.id)
        if compiled is None:
            raise BomPlanError("订单缺少真实BOM快照")
        document, checksum = prepare_production_revision(compiled, changes)
        row = OrderBomProductionRevision(order_item_id=item.id, revision=len(revisions) + 1,
            previous_id=revisions[-1].id if revisions else None, document_json=document,
            content_hash=checksum, created_by=actor.id)
        db.add(row)
        db.flush()
        append_audit_event(db, event_category="business", result="success", source="web",
            module_code="orders", action_code="revise_bom_production", resource="order_bom_graph",
            actor=actor, entity_type="order_item", entity_id=item.id, customer_id=order.customer_id,
            details={"revision": row.revision, "previous_id": row.previous_id,
                     "content_hash": checksum, "changes": changes})
        return row
