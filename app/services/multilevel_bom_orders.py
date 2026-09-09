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
    row = db.get(OrderBomGraph, order_item_id)
    if row is None:
        return None
    item = db.get(OrderItem, order_item_id)
    order = db.get(Order, item.order_id) if item else None
    identities = {(r.product_id, r.product_version) for r in db.scalars(
        select(OrderBomGraphProduct).where(OrderBomGraphProduct.order_item_id == order_item_id))}
    return validate_order_graph_rows(row, item, order, identities)


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
        if existing.content_hash != graph_hash(document):
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
    from app.models.product_bom import SalesOrderItemBomComponent

    graph = read_order_graph(db, order_item_id)
    if graph is None:
        return None
    rows = tuple(db.scalars(select(SalesOrderItemBomComponent).where(
        SalesOrderItemBomComponent.sales_order_item_id == order_item_id).order_by(
        SalesOrderItemBomComponent.display_order)))
    return validate_compiled_order_rows(graph, rows)


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
