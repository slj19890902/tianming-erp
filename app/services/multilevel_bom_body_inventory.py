"""Post explicit body-completion stock, never available finished product stock.

Private receipt adapter: the caller owns permission, frozen receipt validation,
cost capitalization and commit. No endpoint exposes this writer independently.
"""
import hashlib
import json

from sqlalchemy import select

from app.models.multilevel_bom import BomBodyInventoryDetail
from app.models.order import Order, OrderItem
from app.models.product_bom import SalesOrderItemBomComponent
from app.models.production import ProductionCompletion, ProductionTask
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot, InventoryMovement
from app.services.bom_subkits import SubkitError
from app.services.bom_transactions import atomic_bom
from app.services.multilevel_bom_orders import read_order_graph


def body_completion_identity(db, completion):
    if completion is None or completion.status != "posted" or completion.origin != "receipt_auto":
        raise SubkitError("本体入库需要有效的自动收料完工来源")
    task = db.get(ProductionTask, completion.task_id)
    item = db.get(OrderItem, completion.order_item_id)
    graph = read_order_graph(db, completion.order_item_id)
    if task is None or item is None or task.order_item_id != item.id or graph is None:
        raise SubkitError("本体完工任务或订单身份不完整")
    pid = graph.root_id
    if task.sales_order_item_bom_component_id is not None:
        snapshot = db.get(SalesOrderItemBomComponent, task.sales_order_item_bom_component_id)
        if snapshot is None or snapshot.sales_order_item_id != item.id or snapshot.snapshot_schema_version != 5:
            raise SubkitError("本体完工缺少冻结产品来源")
        pid = snapshot.component_product_id
    nodes, children, _ = graph.validated()
    if (pid not in nodes or nodes[pid].source != "manufactured"
            or not any(e.relation == "assembly" for e in children[pid])):
        raise SubkitError("该完工产品不属于待装配本体")
    return item, graph, pid


def stock_product_identity(db, lot):
    """Real product/customer identity; never infer an ID from names or cost JSON."""
    if lot is not None and lot.inventory_type == "finished" and lot.finished_detail is not None:
        return lot.finished_detail.product_id, lot.finished_detail.owner_customer_id
    if lot is None or lot.inventory_type != "assembly_body" or lot.finished_detail is not None:
        raise SubkitError("库存产品身份不完整")
    detail = db.get(BomBodyInventoryDetail, lot.id)
    if (detail is None or detail.inventory_type != "assembly_body" or lot.unit != "boxes"
            or lot.source_ref_type != "production_completion"
            or lot.source_ref_id != detail.production_completion_id):
        raise SubkitError("本体库存缺少真实完工来源")
    completion = db.get(ProductionCompletion, detail.production_completion_id)
    item, graph, pid = body_completion_identity(db, completion)
    if detail.order_item_id != item.id or detail.product_id != pid:
        raise SubkitError("本体库存与冻结完工产品不一致")
    return pid, graph.customer_id


def receive_body_inventory(db, *, completion_id, location_id, operator_id,
                           idempotency_key, expected_layout_version=None):
    from app.services.production_workflow import lock_order_rows_for_production_transition
    from app.services.warehouse_inventory import (
        _claim_inventory_destination, _location, _number, _movement, _balances,
        beijing_today, utc_now_naive,
    )
    if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 100:
        raise SubkitError("本体入库操作标识无效")
    if any(type(value) is not int or value <= 0 for value in (completion_id, location_id, operator_id)):
        raise SubkitError("本体入库来源、位置或人员编号无效")
    with atomic_bom(db):
        actor = db.get(User, operator_id)
        if actor is None or not actor.is_active:
            raise SubkitError("操作人已失效")
        completion = db.get(ProductionCompletion, completion_id)
        item, graph, pid = body_completion_identity(db, completion)
        order = db.get(Order, item.order_id)
        lock_order_rows_for_production_transition(db, [order.id])
        if item.is_force_closed or order.status in ("cancelled", "closed", "dead", "completed", "archived", "delivered"):
            raise SubkitError("已结束订单不能增加本体库存")
        manifest = hashlib.sha256(json.dumps({"completion":completion.id, "product":pid,
            "quantity":completion.quantity, "location":location_id, "actor":operator_id,
            "layout_version":expected_layout_version}, sort_keys=True).encode()).hexdigest()
        previous = db.scalar(select(InventoryMovement).where(InventoryMovement.idempotency_key == idempotency_key))
        if previous is not None:
            lot = db.get(InventoryLot, previous.inventory_lot_id)
            detail = db.get(BomBodyInventoryDetail, previous.inventory_lot_id)
            if (previous.remarks != manifest or lot is None or lot.status != "active"
                    or lot.inventory_type != "assembly_body" or detail is None
                    or detail.production_completion_id != completion.id
                    or completion.inventory_lot_id != lot.id):
                raise SubkitError("本体入库标识已使用或来源已变化")
            return lot
        if completion.inventory_lot_id is not None or completion.quantity <= 0:
            raise SubkitError("该完工已入库或数量无效")
        _claim_inventory_destination(db, location_id, expected_layout_version=expected_layout_version)
        # Bodies occupy the same physical location/capacity as their product,
        # but remain a distinct inventory type excluded from finished picking.
        _location(db, location_id, "finished")
        now = utc_now_naive()
        lot = InventoryLot(lot_number=_number("BODY"), inventory_type="assembly_body",
            warehouse_location_id=location_id, quantity_available=completion.quantity,
            unit="boxes", status="active", source_type="production_completion",
            source_ref_type="production_completion", source_ref_id=completion.id,
            stock_date=beijing_today(), stock_date_accuracy="exact", last_movement_at=now,
            remarks="本体待装配", created_by=operator_id)
        db.add(lot)
        db.flush()
        db.add(BomBodyInventoryDetail(inventory_lot_id=lot.id, order_item_id=item.id,
            product_id=pid, production_completion_id=completion.id))
        _movement(db, lot=lot, movement_type="manual_in", quantity=completion.quantity,
            before={key:0 for key in _balances(lot)}, operator_id=operator_id,
            reason="收料完工进入待装配本体", remarks=manifest,
            idempotency_key=idempotency_key, related_order_item_id=item.id,
            related_order_id=order.id)
        completion.inventory_lot_id = lot.id
        db.flush()
        return lot
