"""Validate exact identities before a procurement snapshot can serve a node.

This internal adapter owns no commit and does not enable external receipt paths.
The route/caller must enforce its existing customer and procurement permission.
"""
from fractions import Fraction
from sqlalchemy import select

from app.models.multilevel_bom import OrderBomExternalComponent
from app.models.order_external_packaging import SalesOrderItemExternalComponent
from app.services.bom_transactions import atomic_bom
from app.services.multilevel_bom_orders import read_compiled_order_bom
from app.services.multilevel_bom_plan import BomPlanError, plan_bom


def _validate(db, *, external_component_id, order_item_id, product_id):
    compiled = read_compiled_order_bom(db, order_item_id)
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
    if Fraction(row.quantity_per_finished_unit) != ratio:
        raise BomPlanError('外购组件用量与真实BOM冻结比例不一致，禁止近似绑定')
    snapshot = next(s for s in compiled.snapshots if s.component_product_id == product_id)
    return snapshot


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
                OrderBomExternalComponent.product_id == product_id)):
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
        result[component.id] = purchase_quantity_for_stock(node, demands[oid][node.product_id])
    return result
