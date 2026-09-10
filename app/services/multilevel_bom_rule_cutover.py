"""Explicit administrator replacement of an unstarted frozen graph.

Orders with execution or placed procurement require the separate source handoff.
This writer appends immutable versions and never modifies inventory balances.
"""
from dataclasses import asdict, replace
import hashlib
import json
from decimal import Decimal

from sqlalchemy import select, update

from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.user import User
from app.models.multilevel_bom import (
    OrderBomGraphProduct, OrderBomRuleRevision, OrderBomRuleProduct, OrderBomRuleSource,
    OrderBomExecutionCutover, BomAssembly, OrderBomExternalComponent,
)
from app.models.product_bom import SalesOrderItemBomComponent, RequisitionItemBomSource, SalesOrderItemBomDemandAdjustment
from app.models.requisition import RequisitionItem
from app.models.production import ProductionTask, ProductionCompletion
from app.models.delivery import DeliveryItem
from app.models.warehouse_inventory import InventoryReservation, InventoryMovement
from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem
from app.models.order_external_packaging import SalesOrderItemExternalComponent
from app.services.audit_log import append_audit_event
from app.services.bom_transactions import atomic_bom
from app.services.multilevel_bom_plan import BomPlanError, plan_bom
from app.services.multilevel_bom_rule_impact import review_current_rule_requirements
from app.services.multilevel_bom_rule_revision import prepare_rule_revision
from app.services.multilevel_bom_cutover_review import _row


def _qualified_review(db, order_item_id, customer_id):
    review = review_current_rule_requirements(db, order_item_id=order_item_id, customer_id=customer_id)
    item = db.get(OrderItem, order_item_id)
    if ((item.delivered_quantity or 0) != 0 or item.requisition_status != "未报料"
            or item.material_status == "received"):
        raise BomPlanError("本入口要求尚未报料、收料或送货；已有执行数量须先交接原来源")
    source_ids = select(SalesOrderItemBomComponent.id).where(
        SalesOrderItemBomComponent.sales_order_item_id == item.id)
    checks = (
        (RequisitionItem, RequisitionItem.order_item_id == item.id, "报料"),
        (RequisitionItemBomSource, RequisitionItemBomSource.sales_order_item_bom_component_id.in_(source_ids), "BOM报料来源"),
        (SalesOrderItemBomDemandAdjustment, SalesOrderItemBomDemandAdjustment.sales_order_item_bom_component_id.in_(source_ids), "需求调整"),
        (InventoryReservation, InventoryReservation.order_item_id == item.id, "预占"),
        (InventoryMovement, InventoryMovement.related_order_item_id == item.id, "库存流水"),
        (ProductionTask, ProductionTask.order_item_id == item.id, "生产任务"),
        (ProductionCompletion, ProductionCompletion.order_item_id == item.id, "完工"),
        (BomAssembly, BomAssembly.order_item_id == item.id, "组装"),
        (DeliveryItem, DeliveryItem.order_item_id == item.id, "送货单"),
        (ExternalPackagingPurchaseItem, ExternalPackagingPurchaseItem.sales_order_item_id == item.id, "外购采购"),
    )
    for model, condition, label in checks:
        identifier = db.scalar(select(model.id).where(condition).order_by(model.id).limit(1))
        if identifier is not None:
            raise BomPlanError(f"订单已有{label}#{identifier}，须保留并交接原来源；不能按无执行来源订单切换")
    from app.services.multilevel_bom_external_identity import current_external_links, read_external_node
    sources = list(db.scalars(select(SalesOrderItemExternalComponent).where(
        SalesOrderItemExternalComponent.sales_order_item_id == item.id).order_by(SalesOrderItemExternalComponent.id)))
    links = list(db.scalars(select(OrderBomExternalComponent).where(OrderBomExternalComponent.order_item_id == item.id)))
    if {source.id for source in sources} != {link.external_component_id for link in links}:
        raise BomPlanError("旧外购冻结来源缺少真实BOM关联，须先核实来源身份")
    active = current_external_links(db, review.previous)
    if {link.product_id for link in active} != {node.product_id for node in review.previous.graph.nodes if node.source == "purchased"}:
        raise BomPlanError("旧版本外购来源不完整，不能用主档推测补写")
    for link in active:
        read_external_node(db, link.external_component_id)
    def values(row):
        return {column.name: getattr(row, column.name) for column in row.__table__.columns}
    external = [dict(source=values(source), candidates=[values(candidate) for candidate in sorted(
        source.candidates, key=lambda candidate: candidate.id)]) for source in sources]
    document = json.dumps(dict(rule_review=review.document, external_sources=external),
        sort_keys=True, ensure_ascii=False, default=str)
    return replace(review, document=document, checksum=hashlib.sha256(document.encode()).hexdigest(),
        impact={**review.impact, "retained_external_source_ids": [source.id for source in sources]})


def rule_cutover_preview(db, *, order_item_id, customer_id):
    review = _qualified_review(db, order_item_id, customer_id)
    quantity = review.impact["remaining_quantity"]
    previous = db.scalar(select(OrderBomRuleRevision).where(OrderBomRuleRevision.order_item_id == order_item_id)
        .order_by(OrderBomRuleRevision.revision.desc()))
    compiled = review.proposed
    nodes = {node.product_id: node for node in compiled.graph.nodes}
    old_units = {node.product_id: node.unit for node in review.previous.graph.nodes}
    from app.services.multilevel_bom_material_estimate import compiled_material_estimate_inputs
    from app.services.order_material_cost import _component
    from app.services.multilevel_bom_purchase_units import purchase_quantity_for_stock
    estimates, missing = [], []
    for source in compiled_material_estimate_inputs(compiled, quantity)[0]:
        component, errors = _component(db, **source, context=None)
        if component is not None:
            estimates.append(component)
        missing.extend(errors)
    if any(node.source == "purchased" for node in nodes.values()):
        missing.append("新外购来源须经正常采购确认，本表不计未确认外购金额")
    plan = plan_bom(compiled.graph, quantity)
    return dict(scope="未发生执行或采购的真实BOM订单显式切换；旧外购冻结来源保留", ready=True,
        order_item_id=order_item_id, quantity=quantity, execution_quantity=quantity, delivered_quantity=0,
        rule_revision=previous.revision if previous else 0,
        reviewed_hash=review.checksum, preview_hash=review.checksum, target_locations={}, source_lot_versions={},
        outputs=[], release_reservations=[], source_costs=[], retained_locations=[],
        old_sources=[{**_row(row), "unit": old_units[row.component_product_id]} for row in review.previous.snapshots],
        new_sources=[{**_row(row), "unit": nodes[row.component_product_id].unit,
            "source_kind": nodes[row.component_product_id].source} for row in compiled.snapshots],
        old_delivery_mode=db.get(OrderItem, order_item_id).composite_fulfillment_mode_snapshot,
        new_modes=asdict(compiled.graph.modes) if compiled.graph.modes else None,
        new_requirements=asdict(plan), rule_impact=review.impact,
        purchase_requirements=[dict(product_id=row.product_id, name=nodes[row.product_id].name,
            stock_quantity=row.make_units, stock_unit=nodes[row.product_id].unit,
            purchase_quantity=str(purchase_quantity_for_stock(nodes[row.product_id], row.make_units)),
            purchase_unit=nodes[row.product_id].purchase_units.purchase_unit)
            for row in plan.products if nodes[row.product_id].source == "purchased"],
        proposed_material_estimate=dict(known_subtotal=str(sum((Decimal(row["estimated_material_cost"]) for row in estimates), Decimal(0))),
            components=estimates, missing_items=list(dict.fromkeys(missing)),
            scope="当前纸板材料报价估算，非实际成本；不含加工、损耗及未确认外购。不改变原预计成本快照"),
        material_impact="后续按新版本及开料换算报料；旧来源完整保留",
        procurement_impact="没有旧采购执行；保留旧外购冻结来源，新版本另建来源并经正常采购确认",
        inventory_impact="不生成、不消耗库存，不创建或释放预占",
        cost_impact="保留原预计成本快照；无实际执行成本转移，不把估算作为实际成本",
        picking_impact="后续按新版本库存和交货规则拿货",
        product_versions={node.product_id: node.version for node in compiled.graph.nodes})


def _result(row):
    return dict(order_item_id=row.order_item_id, execution_quantity=row.order_quantity - row.delivered_before,
        rule_revision=row.revision, rule_revision_id=row.id, assembly_ids=[], output_lot_ids=[])


def execute_rule_cutover(db, *, order_item_id, customer_id, reviewed_hash, expected_revision, operation_key, actor):
    if (type(expected_revision) is not int or expected_revision < 0
            or type(operation_key) is not str or not operation_key.strip() or len(operation_key) > 64
            or type(reviewed_hash) is not str or len(reviewed_hash) != 64
            or any(c not in "0123456789abcdef" for c in reviewed_hash)):
        raise BomPlanError("规则版本、操作标识或预览摘要无效")
    if db.new or db.dirty or db.deleted:
        raise BomPlanError("请先保存或撤销未提交修改")
    request = json.dumps(dict(mode="unstarted_graph", item=order_item_id, customer=customer_id,
        review=reviewed_hash, revision=expected_revision, key=operation_key, actor=getattr(actor, "id", None)), sort_keys=True)
    request_hash = hashlib.sha256(request.encode()).hexdigest()
    with atomic_bom(db):
        actor = db.get(User, getattr(actor, "id", None), populate_existing=True)
        if actor is None or not actor.is_active or actor.role != "admin":
            raise BomPlanError("仅活动管理员可切换订单规则")
        existing = db.scalar(select(OrderBomRuleRevision).where(OrderBomRuleRevision.idempotency_key == operation_key))
        if existing is not None:
            if existing.request_hash != request_hash or existing.order_item_id != order_item_id:
                raise BomPlanError("同一操作标识的订单、版本或内容不一致")
            from app.services.multilevel_bom_orders import read_compiled_order_bom
            read_compiled_order_bom(db, order_item_id)
            return _result(existing)
        if db.scalar(select(OrderBomExecutionCutover.order_item_id).where(
                OrderBomExecutionCutover.idempotency_key == operation_key)) is not None:
            raise BomPlanError("操作标识已用于旧BOM转换")
        item = db.get(OrderItem, order_item_id)
        if item is None:
            raise BomPlanError("订单明细不存在")
        db.execute(update(Order).where(Order.id == item.order_id).values(status=Order.status, updated_at=Order.updated_at))
        db.execute(update(OrderItem).where(OrderItem.id == item.id).values(quantity=OrderItem.quantity))
        review = _qualified_review(db, order_item_id, customer_id)
        previous = db.scalar(select(OrderBomRuleRevision).where(OrderBomRuleRevision.order_item_id == item.id)
            .order_by(OrderBomRuleRevision.revision.desc()))
        if (previous.revision if previous else 0) != expected_revision or review.checksum != reviewed_hash:
            raise BomPlanError("订单或规则版本已变化，请重新预览")
        for node in review.proposed.graph.nodes:
            changed = db.execute(update(Product).where(Product.id == node.product_id, Product.version == node.version,
                Product.customer_id == customer_id, Product.is_active.is_(True), Product.deleted_at.is_(None), Product.purged_at.is_(None))
                .values(version=Product.version, updated_at=Product.updated_at))
            if changed.rowcount != 1:
                raise BomPlanError("BOM产品已变化，请重新预览")
        db.add_all(review.proposed.snapshots)
        db.flush()
        document, checksum = prepare_rule_revision(review.previous, review.proposed,
            order_quantity=item.quantity, delivered_before=0)
        from app.services.multilevel_bom_production_versions import production_revisions
        row = OrderBomRuleRevision(order_item_id=item.id, revision=expected_revision + 1,
            previous_id=previous.id if previous else None, previous_revision=previous.revision if previous else None,
            production_revision_before=len(production_revisions(db, item.id)), order_quantity=item.quantity,
            delivered_before=0, document_json=document, content_hash=checksum, review_hash=reviewed_hash,
            request_hash=request_hash, idempotency_key=operation_key, created_by=actor.id)
        db.add(row)
        db.flush()
        for node in review.proposed.graph.nodes:
            db.add(OrderBomRuleProduct(revision_id=row.id, order_item_id=item.id,
                product_id=node.product_id, product_version=node.version))
            if db.get(OrderBomGraphProduct, (item.id, node.product_id)) is None:
                db.add(OrderBomGraphProduct(order_item_id=item.id, product_id=node.product_id, product_version=node.version))
        db.flush()
        db.add_all([OrderBomRuleSource(snapshot_id=s.id, revision_id=row.id, order_item_id=item.id,
            product_id=s.component_product_id) for s in review.proposed.snapshots])
        db.flush()
        from app.services.multilevel_bom_external_freeze import freeze_order_procurement
        freeze_order_procurement(db, order_item_id=item.id, actor=actor)
        previous_delivery = item.composite_fulfillment_mode_snapshot
        if review.proposed.graph.modes is not None:
            item.composite_fulfillment_mode_snapshot = (
                "component_delivery" if review.proposed.graph.modes.delivery == "components" else "parent_delivery")
        append_audit_event(db, event_category="business", result="success", source="web", module_code="orders",
            action_code="switch_unstarted_graph_rule", resource="order_bom_rule_revision", actor=actor,
            entity_type="order_item", entity_id=item.id, customer_id=customer_id,
            details=dict(rule_revision=row.revision, rule_revision_id=row.id, previous_id=row.previous_id,
                review_hash=reviewed_hash, content_hash=checksum, request_hash=request_hash, operation_key=operation_key,
                previous_delivery_mode=previous_delivery, delivery_mode=item.composite_fulfillment_mode_snapshot,
                previous_source_ids=[source.id for source in review.previous.snapshots],
                retained_external_source_ids=review.impact["retained_external_source_ids"],
                current_source_ids=[source.id for source in review.proposed.snapshots]))
        db.flush()
        return _result(row)
