"""Freeze one canonical graph atomically; caller owns permission and commit.

An existing snapshot is never replaced using changed Common Box master data.
Public order entry uses the procurement adapter to freeze graph, material and
external-node identities together; schema creation alone is not that contract.
"""

from sqlalchemy import select, update

from app.models.multilevel_bom import OrderBomGraph, OrderBomGraphProduct
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.user import User
from app.services.audit_log import append_audit_event
from app.services.bom_transactions import atomic_bom
from app.services.multilevel_bom_plan import BomPlanError
from app.services.multilevel_bom_snapshot import (
    dump_graph, graph_hash, load_graph, verify_current_identities, graph_schema_version,
)


def read_order_graph(db, order_item_id):
    from app.services.multilevel_bom_rule_history import rule_histories_by_order, base_graph_with_rule_registry
    row = db.get(OrderBomGraph, order_item_id)
    if row is None:
        return None
    item = db.get(OrderItem, order_item_id)
    order = db.get(Order, item.order_id) if item else None
    identities = {(r.product_id, r.product_version) for r in db.scalars(
        select(OrderBomGraphProduct).where(OrderBomGraphProduct.order_item_id == order_item_id))}
    history = rule_histories_by_order(db, {order_item_id})[order_item_id]
    if history.revisions:
        return read_compiled_order_bom(db, order_item_id).graph
    return base_graph_with_rule_registry(row, item, order, identities, history)


def validate_order_graph_rows(row, item, order, identities):
    """Same identity/hash gate for individual and batched read paths."""
    graph = load_graph(row.document_json, expected_hash=row.content_hash)
    if (item is None or order is None or item.product_id != graph.root_id
            or order.customer_id != graph.customer_id
            or row.schema_version != graph_schema_version(graph) or row.root_product_id != graph.root_id
            or row.customer_id != graph.customer_id
            or identities != {(n.product_id, n.version) for n in graph.nodes}):
        raise BomPlanError("订单BOM快照身份校验失败")
    return graph


def freeze_order_graph(db, *, order_item_id, graph, actor: User):
    document = dump_graph(graph)
    existing = db.get(OrderBomGraph, order_item_id)
    if existing is not None:
        frozen = read_order_graph(db, order_item_id)
        if graph_hash(dump_graph(frozen)) != graph_hash(document):
            raise BomPlanError("订单BOM已冻结，不能用新配方覆盖")
        return frozen
    with atomic_bom(db):
        item = db.get(OrderItem, order_item_id)
        order = db.get(Order, item.order_id) if item else None
        if (item is None or order is None or item.product_id != graph.root_id
                or order.customer_id != graph.customer_id):
            raise BomPlanError("订单与BOM产品或客户不一致")
        if item.is_force_closed or int(item.delivered_quantity or 0) > 0 or order.status in (
                "cancelled", "closed", "dead", "completed", "archived", "delivered"):
            raise BomPlanError("已有送货或已结束订单不能补写BOM快照")
        if actor is None or not actor.is_active:
            raise BomPlanError("操作人已失效")
        from app.services.production_workflow import lock_order_rows_for_production_transition
        lock_order_rows_for_production_transition(db, [order.id])
        products = list(db.scalars(select(Product).where(
            Product.id.in_([node.product_id for node in graph.nodes]))))
        verify_current_identities(graph, products)
        # CAS-lock each master row through the same order transaction. A master
        # edit cannot slip between version validation and snapshot insertion.
        for node in sorted(graph.nodes, key=lambda n: n.product_id):
            changed = db.execute(update(Product).where(
                Product.id == node.product_id, Product.version == node.version,
                Product.customer_id == graph.customer_id, Product.is_active.is_(True),
                Product.deleted_at.is_(None), Product.purged_at.is_(None),
            ).values(version=Product.version, updated_at=Product.updated_at))
            if changed.rowcount != 1:
                raise BomPlanError("BOM产品已变化，请刷新")
        row = OrderBomGraph(order_item_id=item.id, root_product_id=graph.root_id,
            customer_id=graph.customer_id, schema_version=graph_schema_version(graph),
            document_json=document, content_hash=graph_hash(document), created_by=actor.id)
        db.add(row)
        db.flush()
        db.add_all([OrderBomGraphProduct(order_item_id=item.id, product_id=n.product_id,
            product_version=n.version) for n in graph.nodes])
        db.flush()
        append_audit_event(db, event_category="business", result="success", source="web",
            module_code="orders", action_code="freeze_multilevel_bom", resource="order_bom_graph",
            actor=actor, entity_type="order_item", entity_id=item.id, customer_id=order.customer_id,
            details={"content_hash": row.content_hash, "root_product_id": graph.root_id,
                "products": [n.product_id for n in graph.nodes], "schema_version": graph_schema_version(graph)})
    return graph


def read_compiled_order_bom(db, order_item_id):
    """Read graph AND material facts; never repair missing facts from master."""
    return _read_compiled_order_bom(db, order_item_id)


def read_order_bom_source_contract(db, order_item_id, snapshot_id):
    """Read a source's frozen rule for historical evidence, not current demand.

    A removed product remains readable by its exact source ID. The full event
    chain is still verified; this cannot bypass a corrupt later revision.
    """
    from app.services.multilevel_bom_plan import _integer
    _integer(snapshot_id, "历史BOM来源ID", 1)
    compiled = _read_compiled_order_bom(db, order_item_id, source_snapshot_id=snapshot_id)
    if compiled is None:
        raise BomPlanError("历史BOM来源缺少订单冻结图")
    return compiled


def _read_compiled_order_bom(db, order_item_id, *, source_snapshot_id=None):
    from app.models.product_bom import SalesOrderItemBomComponent

    header = db.get(OrderBomGraph, order_item_id)
    if header is None:
        return None
    from app.models.multilevel_bom import OrderBomExecutionCutover, OrderBomCutoverSource
    from app.services.multilevel_bom_rule_history import rule_histories_by_order, project_order_rule_history
    # One source query still covers ordinary and converted orders. Historical
    # rows are read for checksum validation, never returned as current demand.
    records = db.execute(select(SalesOrderItemBomComponent, OrderBomExecutionCutover,
        OrderBomCutoverSource.role, OrderItem).join(OrderItem,
            OrderItem.id == SalesOrderItemBomComponent.sales_order_item_id)
        .outerjoin(OrderBomExecutionCutover, OrderBomExecutionCutover.order_item_id == OrderItem.id)
        .outerjoin(OrderBomCutoverSource,
            (OrderBomCutoverSource.snapshot_id == SalesOrderItemBomComponent.id)
            & (OrderBomCutoverSource.order_item_id == OrderItem.id))
        .where(SalesOrderItemBomComponent.sales_order_item_id == order_item_id)
        .order_by(SalesOrderItemBomComponent.display_order)).all()
    if not records:
        raise BomPlanError("订单多级BOM材料快照不完整，不能用当前主档补写")
    item = records[0][3]
    identities = {(row.product_id, row.product_version) for row in db.scalars(
        select(OrderBomGraphProduct).where(OrderBomGraphProduct.order_item_id == order_item_id))}
    from app.services.multilevel_bom_production_versions import production_revisions
    return project_order_rule_history(header=header, item=item, order=db.get(Order, item.order_id),
        identities=identities, cutover=records[0][1],
        rows_with_roles=((row, role) for row, _, role, _ in records),
        production_rows=production_revisions(db, order_item_id),
        history=rule_histories_by_order(db, {order_item_id})[order_item_id],
        source_snapshot_id=source_snapshot_id)


def validate_compiled_order_rows(graph, rows):
    from app.services.multilevel_bom_compile import CompiledMasterBom, physical_routes
    from app.services.multilevel_bom_plan import plan_bom
    nodes = {n.product_id: n for n in graph.nodes}
    if len(rows) != len(nodes) or {r.component_product_id for r in rows} != set(nodes):
        raise BomPlanError("订单多级BOM材料快照不完整，不能用当前主档补写")
    gross = {d.product_id: d.required_units for d in plan_bom(graph, 1).products}
    if len({row.order_set_quantity for row in rows}) != 1:
        raise BomPlanError("订单多级BOM冻结数量不一致")
    for row in rows:
        node = nodes[row.component_product_id]
        if (row.snapshot_schema_version != 5 or row.component_product_version != node.version
                or row.parent_product_version != nodes[graph.root_id].version
                or row.snapshot_component_product_name != node.name
                or row.quantity_per_set != gross[node.product_id]
                or row.required_piece_quantity != row.order_set_quantity * row.quantity_per_set
                or row.order_set_quantity <= 0):
            raise BomPlanError("订单多级BOM材料身份或数量校验失败")
        if node.source == "manufactured" and sorted(physical_routes(row), key=lambda r: r.key) != sorted(node.routes, key=lambda r: r.key):
            raise BomPlanError("订单多级BOM物理片组校验失败")
    return CompiledMasterBom(graph, rows)


def freeze_master_order_bom(db, *, order_item_id, actor: User, root_order_snapshot=False):
    """Atomically freeze the real recipe and existing material/process columns.

    All nodes have one source row, including the root and intermediate outputs.
    Execution must select sources by frozen node.source, NOT assume every row
    is a material to buy. Public order entry uses freeze_order_procurement so
    external-node identities are frozen in the same transaction.
    """
    from app.models.product_bom import SalesOrderItemBomComponent
    from app.services.multilevel_bom_compile import compile_master_order_bom

    with atomic_bom(db):
        if db.get(OrderBomGraph, order_item_id) is not None:
            return read_compiled_order_bom(db, order_item_id)
        if db.scalar(select(SalesOrderItemBomComponent.id).where(
                SalesOrderItemBomComponent.sales_order_item_id == order_item_id).limit(1)) is not None:
            raise BomPlanError("已有旧BOM冻结事实，必须通过审计转换，不能覆盖")
        item = db.get(OrderItem, order_item_id)
        if item is None:
            raise BomPlanError("订单明细不存在")
        compiled = compile_master_order_bom(db, item, root_order_snapshot=root_order_snapshot)
        freeze_order_graph(db, order_item_id=order_item_id, graph=compiled.graph, actor=actor)
        db.add_all(compiled.snapshots)
        db.flush()
        append_audit_event(db, event_category="business", result="success", source="web",
            module_code="orders", action_code="freeze_multilevel_materials", resource="order_bom_graph",
            actor=actor, entity_type="order_item", entity_id=order_item_id, customer_id=compiled.graph.customer_id,
            details={"sources": [{"product_id": r.component_product_id, "snapshot_id": r.id}
                                 for r in compiled.snapshots]})
        return read_compiled_order_bom(db, order_item_id)
