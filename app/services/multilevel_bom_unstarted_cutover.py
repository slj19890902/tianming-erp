"""Explicit legacy recipe handoff before procurement or physical execution."""
from dataclasses import asdict
import hashlib
import json

from sqlalchemy import select, update

from app.models.order import Order, OrderItem
from app.models.user import User
from app.models.multilevel_bom import OrderBomExecutionCutover
from app.models.requisition import RequisitionItem
from app.models.product_bom import RequisitionItemBomSource
from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem
from app.models.order_external_packaging import SalesOrderItemExternalComponent
from app.services.audit_log import append_audit_event
from app.services.bom_transactions import atomic_bom
from app.services.multilevel_bom_cutover_review import review_legacy_cutover
from app.services.multilevel_bom_cutover import persist_cutover_review
from app.services.multilevel_bom_plan import BomPlanError, plan_bom


def review_unstarted_cutover(db, *, order_item_id, customer_id):
    review = review_legacy_cutover(db, order_item_id=order_item_id, customer_id=customer_id)
    manifest = json.loads(review.document)
    item = manifest["item"]
    if ((item["delivered_quantity"] or 0) != 0 or item["requisition_status"] != "未报料"
            or item["material_status"] == "received"):
        raise BomPlanError("本入口仅用于未开始订单；已有报料、收料或送货状态须先核对执行来源")
    for key, label in (("reservations", "库存预占"), ("tasks", "生产任务"),
                       ("completions", "完工"), ("deliveries", "送货单"),
                       ("adjustments", "需求调整")):
        if manifest[key]:
            raise BomPlanError(f"订单已有{label}记录，不能按未开始订单切换；需保留并核对该来源")
    checks = (
        (RequisitionItem, RequisitionItem.order_item_id == order_item_id, "报料"),
        (RequisitionItemBomSource, RequisitionItemBomSource.sales_order_item_bom_component_id.in_(
            [row.id for row in review.history]), "BOM报料来源"),
        (ExternalPackagingPurchaseItem, ExternalPackagingPurchaseItem.sales_order_item_id == order_item_id, "外购采购"),
        (SalesOrderItemExternalComponent, SalesOrderItemExternalComponent.sales_order_item_id == order_item_id, "旧外购冻结来源"),
    )
    for model, condition, label in checks:
        if db.scalar(select(model.id).where(condition).limit(1)) is not None:
            raise BomPlanError(f"订单已有{label}记录，不能按未开始订单切换；需先审计交接")
    return review


def unstarted_preview(db, *, order_item_id, customer_id):
    review = review_unstarted_cutover(db, order_item_id=order_item_id, customer_id=customer_id)
    manifest = json.loads(review.document)
    quantity = manifest["remaining_quantity"]
    plan = plan_bom(review.compiled.graph, quantity)
    nodes = {node.product_id: node for node in review.compiled.graph.nodes}
    from app.services.multilevel_bom_purchase_units import purchase_quantity_for_stock
    return {"scope": "未发生报料、采购、库存或生产送货事实的旧BOM订单切换",
        "ready": True, "order_item_id": order_item_id, "quantity": quantity,
        "delivered_quantity": 0, "execution_quantity": quantity,
        "reviewed_hash": review.checksum, "preview_hash": review.checksum,
        "target_locations": {}, "source_lot_versions": {}, "outputs": [],
        "release_reservations": [], "source_costs": [], "retained_locations": [],
        "old_sources": manifest["history"],
        "old_delivery_mode": manifest["item"]["composite_fulfillment_mode_snapshot"],
        "new_modes": asdict(review.compiled.graph.modes) if review.compiled.graph.modes else None,
        "new_sources": [{**row, "unit": nodes[row["component_product_id"]].unit,
            "source_kind": nodes[row["component_product_id"]].source} for row in manifest["current"]],
        "new_requirements": asdict(plan),
        "purchase_requirements": [{"product_id": row.product_id,
            "name": nodes[row.product_id].name, "stock_quantity": row.make_units,
            "stock_unit": nodes[row.product_id].unit,
            "purchase_quantity": str(purchase_quantity_for_stock(nodes[row.product_id], row.make_units)),
            "purchase_unit": nodes[row.product_id].purchase_units.purchase_unit}
            for row in plan.products if nodes[row.product_id].source == "purchased"],
        "material_impact": "之后按新冻结子件及开料换算报料；下列旧快照保留，不覆写",
        "procurement_impact": "没有采购执行记录；外购节点重新冻结当前真实候选，之后由正常采购确认执行",
        "inventory_impact": "不生成、不消耗库存，也不创建预占",
        "cost_impact": "没有已执行成本可转移；保留原订单预计成本，不把预计金额作为实际成本",
        "picking_impact": "后续按新冻结库存和交货规则拿货",
        "product_versions": {node.product_id: node.version for node in review.compiled.graph.nodes}}


def execute_unstarted_cutover(db, *, order_item_id, customer_id, reviewed_hash, operation_key, actor):
    if (not isinstance(operation_key, str) or not 1 <= len(operation_key) <= 64
            or not isinstance(reviewed_hash, str) or len(reviewed_hash) != 64):
        raise BomPlanError("切换标识或预览摘要无效")
    if db.new or db.dirty or db.deleted:
        raise BomPlanError("请先保存或撤销未提交修改")
    request = json.dumps({"mode": "unstarted", "item": order_item_id, "customer": customer_id,
        "review": reviewed_hash, "key": operation_key, "actor": getattr(actor, "id", None)}, sort_keys=True)
    request_hash = hashlib.sha256(request.encode()).hexdigest()
    with atomic_bom(db):
        actor = db.get(User, getattr(actor, "id", None), populate_existing=True)
        if actor is None or not actor.is_active or actor.role != "admin":
            raise BomPlanError("仅活动管理员可切换旧订单BOM")
        existing = db.get(OrderBomExecutionCutover, order_item_id)
        if existing is not None:
            if existing.request_hash != request_hash or existing.idempotency_key != operation_key:
                raise BomPlanError("订单已切换或同键载荷不一致")
            from app.services.multilevel_bom_orders import read_compiled_order_bom
            read_compiled_order_bom(db, order_item_id)
            return {"order_item_id": order_item_id, "execution_quantity": existing.order_quantity,
                    "assembly_ids": [], "output_lot_ids": []}
        if db.scalar(select(OrderBomExecutionCutover.order_item_id).where(
                OrderBomExecutionCutover.idempotency_key == operation_key)) is not None:
            raise BomPlanError("操作标识已用于其他订单")
        item = db.get(OrderItem, order_item_id)
        if item is None:
            raise BomPlanError("订单明细不存在")
        # Acquire the same Order -> OrderItem write locks without changing a
        # timestamp that is part of the caller's exact review manifest.
        db.execute(update(Order).where(Order.id == item.order_id).values(
            status=Order.status, updated_at=Order.updated_at))
        db.execute(update(OrderItem).where(OrderItem.id == item.id).values(quantity=OrderItem.quantity))
        review = review_unstarted_cutover(db, order_item_id=order_item_id, customer_id=customer_id)
        if review.checksum != reviewed_hash:
            raise BomPlanError("订单或BOM版本已变化，请重新预览")
        checksum = persist_cutover_review(db, review=review, item=item, customer_id=customer_id,
            operation_key=operation_key, request_hash=request_hash, actor=actor)
        from app.services.multilevel_bom_external_freeze import freeze_order_procurement
        freeze_order_procurement(db, order_item_id=order_item_id, actor=actor)
        previous_delivery = item.composite_fulfillment_mode_snapshot
        if review.compiled.graph.modes is not None:
            # This adapter proved there are no historical delivery documents.
            # The explicit operation may therefore update the order display
            # contract; a partial-order conversion must not use this shortcut.
            item.composite_fulfillment_mode_snapshot = (
                "component_delivery" if review.compiled.graph.modes.delivery == "components" else "parent_delivery")
        append_audit_event(db, event_category="business", result="success", source="web",
            module_code="orders", action_code="switch_unstarted_bom", resource="order_bom_execution_cutover",
            actor=actor, entity_type="order_item", entity_id=order_item_id, customer_id=customer_id,
            details={"review_hash": reviewed_hash, "request_hash": request_hash, "basis_hash": checksum,
                     "operation_key": operation_key, "quantity": item.quantity,
                     "previous_delivery_mode": previous_delivery,
                     "delivery_mode": item.composite_fulfillment_mode_snapshot,
                     "history_source_ids": [row.id for row in review.history]})
        db.flush()
        return {"order_item_id": order_item_id, "execution_quantity": item.quantity,
                "assembly_ids": [], "output_lot_ids": []}
