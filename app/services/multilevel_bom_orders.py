"""Freeze one canonical graph atomically; caller owns permission and commit.

An existing snapshot is never replaced using changed Common Box master data.
This adapter is not registered with order entry until receipt/dispatch adapters
have passed acceptance; schema creation alone does not enable nested orders.
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
    SCHEMA_VERSION, dump_graph, graph_hash, load_graph, verify_current_identities,
)


def read_order_graph(db, order_item_id):
    row = db.get(OrderBomGraph, order_item_id)
    if row is None:
        return None
    graph = load_graph(row.document_json, expected_hash=row.content_hash)
    item = db.get(OrderItem, order_item_id)
    order = db.get(Order, item.order_id) if item else None
    identities = {(r.product_id, r.product_version) for r in db.scalars(
        select(OrderBomGraphProduct).where(OrderBomGraphProduct.order_item_id == order_item_id))}
    if (item is None or order is None or item.product_id != graph.root_id
            or order.customer_id != graph.customer_id
            or row.schema_version != SCHEMA_VERSION or row.root_product_id != graph.root_id
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
            customer_id=graph.customer_id, schema_version=SCHEMA_VERSION,
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
                "products": [n.product_id for n in graph.nodes], "schema_version": SCHEMA_VERSION})
    return graph
