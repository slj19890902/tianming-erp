"""Validate exact identities before a procurement snapshot can serve a node.

This internal adapter owns no commit and does not enable external receipt paths.
The route/caller must enforce its existing customer and procurement permission.
"""
from fractions import Fraction
from decimal import Decimal
from sqlalchemy import select

from app.models.multilevel_bom import OrderBomExternalComponent
from app.models.order_external_packaging import SalesOrderItemExternalComponent
from app.services.bom_transactions import atomic_bom
from app.services.multilevel_bom_orders import read_compiled_order_bom
from app.services.multilevel_bom_plan import BomPlanError, plan_bom


def current_external_component_predicate(component=SalesOrderItemExternalComponent):
    """Exclude proven historical links; retain corrupt/unlinked rows to fail validation."""
    return current_external_source_predicate(component.id, component.sales_order_item_id)


def current_external_source_predicate(component_id, order_item_id):
    """Apply the same current-source scope to component and purchase queries."""
    from app.models.product_bom import SalesOrderItemBomComponent as Source
    from app.services.multilevel_bom_execution_boundary import current_snapshot_predicate
    historical = select(1).select_from(OrderBomExternalComponent).join(Source,
        (Source.id == OrderBomExternalComponent.bom_snapshot_id)
        & (Source.sales_order_item_id == OrderBomExternalComponent.order_item_id)
        & (Source.component_product_id == OrderBomExternalComponent.product_id)).where(
        OrderBomExternalComponent.external_component_id == component_id,
        OrderBomExternalComponent.order_item_id == order_item_id,
        ~current_snapshot_predicate(Source)).exists()
    return ~historical


def current_external_links(db, compiled):
    source_ids = {source.id for source in compiled.snapshots}
    return list(db.scalars(select(OrderBomExternalComponent).where(
        OrderBomExternalComponent.bom_snapshot_id.in_(source_ids))))


def _validate(db, *, external_component_id, order_item_id, product_id, compiled=None):
    compiled = compiled if compiled is not None else read_compiled_order_bom(db, order_item_id)
    node = next((n for n in compiled.graph.nodes if n.product_id == product_id), None) if compiled else None
    row = db.get(SalesOrderItemExternalComponent, external_component_id)
    if node is None or node.source != 'purchased' or node.purchase_units is None:
        raise BomPlanError('缺少真实外购节点或冻结采购比例')
    if (row is None or row.sales_order_item_id != order_item_id
            or row.source_kind != 'direct_product'
            or row.source_component_set_version != node.version
            or not row.is_required or row.waste_rate != 0
            or row.units_per_purchase_unit is not None
            or row.consumption_unit != node.purchase_units.purchase_unit):
        raise BomPlanError('外购组件与真实BOM订单、版本或单位不一致')
    gross = next(p.required_units for p in plan_bom(compiled.graph, 1).products if p.product_id == product_id)
    ratio = Fraction(gross) * Fraction(node.purchase_units.purchase_basis) / Fraction(node.purchase_units.stock_basis)
    if row.quantity_per_finished_unit != legacy_multiplier(ratio):
        raise BomPlanError('外购组件用量与真实BOM冻结比例不一致，禁止近似绑定')
    snapshot = next(s for s in compiled.snapshots if s.component_product_id == product_id)
    return snapshot


def legacy_multiplier(ratio):
    """Bounded compatibility projection; exact graph ratio owns arithmetic."""
    scaled = ratio * 1_000_000
    value = Decimal(-(-scaled.numerator // scaled.denominator)) / Decimal(1_000_000)
    if not 0 < value < Decimal('1e12'):
        raise BomPlanError('外购每父件采购比例超出数据库精度')
    return value


def bind_external_component(db, *, external_component_id, order_item_id, product_id, actor):
    with atomic_bom(db):
        if actor is None or not actor.is_active:
            raise BomPlanError('操作人已失效')
        snapshot = _validate(db, external_component_id=external_component_id,
            order_item_id=order_item_id, product_id=product_id)
        existing = db.get(OrderBomExternalComponent, external_component_id)
        if existing:
            if (existing.order_item_id, existing.product_id, existing.bom_snapshot_id) != (order_item_id, product_id, snapshot.id):
                raise BomPlanError('外购组件已绑定其他BOM节点，不能覆盖')
            return existing
        if db.scalar(select(OrderBomExternalComponent).where(
                OrderBomExternalComponent.order_item_id == order_item_id,
                OrderBomExternalComponent.bom_snapshot_id == snapshot.id)):
            raise BomPlanError('该BOM节点已有外购组件快照，不能重复绑定')
        link = OrderBomExternalComponent(external_component_id=external_component_id,
            order_item_id=order_item_id, product_id=product_id, bom_snapshot_id=snapshot.id)
        db.add(link)
        db.flush()
        from app.services.audit_log import append_audit_event
        append_audit_event(db, event_category='business', result='success', source='web',
            module_code='orders', action_code='bind_bom_external_component', resource='order_bom_external_components',
            actor=actor, entity_type='order_item', entity_id=order_item_id,
            details={'external_component_id': external_component_id, 'product_id': product_id,
                     'bom_snapshot_id': snapshot.id})
        return link


def read_external_node(db, external_component_id):
    link = db.get(OrderBomExternalComponent, external_component_id)
    if link is None:
        return None
    snapshot = _validate(db, external_component_id=external_component_id,
        order_item_id=link.order_item_id, product_id=link.product_id)
    if snapshot.id != link.bom_snapshot_id:
        raise BomPlanError('外购BOM来源快照身份不一致')
    return link


def read_external_source_contract(db, external_component_id):
    """Return exact historical procurement identity and its validated recipe.

    New procurement continues to use read_external_node's current-only guard.
    This reader grants no right to receive, reserve or switch an old purchase.
    """
    from app.services.multilevel_bom_orders import read_order_bom_source_contract
    link = db.get(OrderBomExternalComponent, external_component_id)
    if link is None:
        raise BomPlanError("外购历史来源缺少真实BOM关联")
    compiled = read_order_bom_source_contract(db, link.order_item_id, link.bom_snapshot_id)
    snapshot = _validate(db, external_component_id=external_component_id,
        order_item_id=link.order_item_id, product_id=link.product_id, compiled=compiled)
    if snapshot.id != link.bom_snapshot_id:
        raise BomPlanError("外购历史来源快照身份不一致")
    return link, compiled


def external_receipt_contract(db, external_component_id, *, compiled=None):
    """Old quote owns conversion/cost; explicit handoff owns new execution."""
    link, original = read_external_source_contract(db, external_component_id)
    current = compiled if compiled is not None else read_compiled_order_bom(db, link.order_item_id)
    if current is None or current.graph.customer_id != original.graph.customer_id:
        raise BomPlanError("外购实收缺少同客户当前执行规则")
    if link.bom_snapshot_id in {row.id for row in current.snapshots}:
        return link, original, current, None
    from app.services.multilevel_bom_source_handoffs import current_source_handoffs
    handoff = next((row for row in current_source_handoffs(db, current)
        if row.source_snapshot_id == link.bom_snapshot_id and row.source_kind == "purchased"), None)
    if handoff is None:
        raise BomPlanError("旧外购来源尚未明确交接到当前版本，不能收料")
    return link, original, current, handoff


def external_receipt_execution_contract(db, receipt_item_id):
    """Read the version recorded at receipt time, including after later edits."""
    from app.models.external_packaging_purchase import ExternalPackagingReceiptItem, ExternalPackagingPurchaseItem
    from app.models.multilevel_bom import OrderBomExternalReceiptExecution, OrderBomSourceHandoff
    from app.services.multilevel_bom_orders import read_order_bom_source_contract
    from app.services.multilevel_bom_source_handoffs import validate_source_handoff
    receipt = db.get(ExternalPackagingReceiptItem, receipt_item_id)
    purchase = db.get(ExternalPackagingPurchaseItem, receipt.purchase_item_id) if receipt else None
    if purchase is None:
        raise BomPlanError("外购实收缺少原采购来源")
    link, original = read_external_source_contract(db, purchase.order_component_id)
    ownership = db.get(OrderBomExternalReceiptExecution, receipt_item_id)
    if ownership is None:
        return link, original
    handoff = db.get(OrderBomSourceHandoff, (ownership.revision_id, ownership.source_snapshot_id))
    if (handoff is None or handoff.source_snapshot_id != link.bom_snapshot_id
            or handoff.order_item_id != purchase.sales_order_item_id
            or handoff.order_item_id != link.order_item_id or handoff.product_id != link.product_id
            or handoff.source_kind != "purchased"):
        raise BomPlanError("外购实收执行来源与原合同不一致")
    execution = read_order_bom_source_contract(db, link.order_item_id, handoff.target_snapshot_id)
    validate_source_handoff(db, handoff, execution)
    return link, execution


def frozen_purchase_quantities(db, components, order_items):
    """Uncovered procurement demand from authoritative order reservations.

    Missing graph links must not fall back to approximate legacy multipliers.
    Ordinary orders take only one batched graph-presence lookup.
    """
    from app.models.multilevel_bom import OrderBomGraph
    from app.services.multilevel_bom_purchase_units import purchase_quantity_for_stock
    from app.services.multilevel_bom_requirements import read_graph_requirements
    ids = {c.sales_order_item_id for c in components}
    if not ids:
        return {}
    graph_ids = set(db.scalars(select(OrderBomGraph.order_item_id).where(OrderBomGraph.order_item_id.in_(ids))))
    result = {}
    requirements = {oid: read_graph_requirements(db, oid) for oid in graph_ids}
    compiled_by_id = {oid: requirement.compiled for oid, requirement in requirements.items()}
    from app.services.multilevel_bom_carried_procurement import carried_purchase_stock
    carried = {oid: carried_purchase_stock(db, compiled) for oid, compiled in compiled_by_id.items()}
    demands = {oid: {d.product_id: d.make_units for d in requirement.plan.products}
               for oid, requirement in requirements.items()}
    for component in components:
        oid = component.sales_order_item_id
        if oid not in graph_ids:
            continue
        link = read_external_node(db, component.id)
        if link is None:
            raise BomPlanError('真实BOM外购节点关联不完整，不能按旧组件数量采购')
        node = next(n for n in compiled_by_id[oid].graph.nodes if n.product_id == link.product_id)
        result[component.id] = purchase_quantity_for_stock(node,
            max(demands[oid][node.product_id] - carried[oid].get(node.product_id, 0), 0))
    return result
