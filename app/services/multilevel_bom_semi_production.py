"""Explicit reserved-material production; no synthetic incoming receipt facts."""
import hashlib
import json

from sqlalchemy import select

from app.models.order import OrderItem
from app.models.production import ProductionCompletion, ProductionCompletionBatch
from app.models.warehouse_inventory import InventoryLot, InventoryReservation
from app.services.bom_transactions import atomic_bom
from app.services.multilevel_bom_plan import BomPlanError
from app.services.multilevel_bom_receipts import plan_semi_only_production


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _map_hash():
    from app.services.warehouse_twin_layout import resolve_warehouse_twin_layout_path
    return hashlib.sha256(resolve_warehouse_twin_layout_path().read_bytes()).hexdigest()


def preview_semi_production(db, *, order_item_id, product_id):
    from app.services.multilevel_bom_cutover_review import _row
    context, plan = plan_semi_only_production(db, order_item_id=order_item_id, product_id=product_id)
    from app.services.production_workflow import _receipt_auto_finished_ground_target
    target_products = {node.product_id for node in context.compiled.graph.nodes
        if node.source == "assembled" or (node.source in ("manufactured", "purchased") and any(
            edge.parent_id == node.product_id and edge.relation == "assembly" for edge in context.compiled.graph.edges))}
    production_target = _receipt_auto_finished_ground_target(db, claim=False,
        customer_id=context.compiled.graph.customer_id, product_id=product_id)
    targets = {}
    excluded = set() if production_target.target_kind == "fixed_shelf" else {production_target.location.id}
    for pid in sorted(target_products):
        target = _receipt_auto_finished_ground_target(db, claim=False, excluded_location_ids=excluded,
            customer_id=context.compiled.graph.customer_id, product_id=pid)
        targets[pid] = target.location
        if target.target_kind != "fixed_shelf":
            excluded.add(target.location.id)
    inputs = plan["detail"]["bom_material_inputs"]
    reservations = [db.get(InventoryReservation, entry["id"]) for entry in inputs]
    lots = {row.inventory_lot_id: db.get(InventoryLot, row.inventory_lot_id) for row in reservations}
    document = dict(plan=plan, snapshot_id=context.snapshot.id, item=_row(db.get(OrderItem, order_item_id)),
        reservations=[_row(row) for row in reservations], lots=[_row(lot) for lot in lots.values()],
        production_location=_row(production_target.location),
        targets={pid: _row(location) for pid, location in targets.items()}, map_hash=_map_hash())
    return context, plan, dict(product_id=product_id, snapshot_id=context.snapshot.id,
        quantity=plan["after"]-plan["before"], unit=context.node.unit, estimated_cost=str(plan["total_cost"]),
        reviewed_hash=_hash(document), ready=plan["after"] > plan["before"],
        source_lot_versions={lid: lot.version for lid, lot in lots.items()},
        production_location=dict(id=production_target.location.id, name=production_target.location.location_name),
        target_locations={pid: location.id for pid, location in targets.items()},
        target_names={pid: location.location_name for pid, location in targets.items()},
        source_locations={lid: lot.warehouse_location_id for lid, lot in lots.items()}, map_hash=document["map_hash"])


def confirm_semi_production(db, *, order_item_id, product_id, reviewed_hash, operation_key, actor):
    from app.services.production_workflow import lock_order_rows_for_production_transition, post_automatic_receipt_completion, refresh_order_production_status
    from app.services.multilevel_bom_receipts import assemble_graph_order_receipt, refresh_graph_main_task
    from app.services.audit_log import append_audit_event
    if actor is None or not actor.is_active or actor.role != "admin":
        raise BomPlanError("仅活动管理员可确认半成品生产")
    request_hash = _hash(dict(order=order_item_id, product=product_id, review=reviewed_hash, key=operation_key, actor=actor.id))
    key = "bom-semi:"+hashlib.sha256(operation_key.encode()).hexdigest()
    with atomic_bom(db):
        item = db.get(OrderItem, order_item_id)
        if item is None:
            raise BomPlanError("订单不存在")
        lock_order_rows_for_production_transition(db, [item.order_id])
        prior = db.scalar(select(ProductionCompletion).join(ProductionCompletionBatch).where(
            ProductionCompletionBatch.idempotency_key == key))
        if prior is not None:
            lot = db.get(InventoryLot, prior.inventory_lot_id)
            detail = json.loads(lot.cost_snapshot_detail_json or "{}") if lot else {}
            if prior.status != "posted" or detail.get("semi_production_request_hash") != request_hash:
                raise BomPlanError("生产标识已使用、内容不同或已经撤销")
            return dict(completion_id=prior.id, lot_id=prior.inventory_lot_id, quantity=prior.quantity)
        context, plan, preview = preview_semi_production(db, order_item_id=order_item_id, product_id=product_id)
        if not preview["ready"] or preview["reviewed_hash"] != reviewed_hash:
            raise BomPlanError("预占、订单或地图已变化，请重新预览")
        inputs = plan["detail"]["bom_material_inputs"]
        completion = post_automatic_receipt_completion(db, order_item_id=order_item_id,
            previous_theoretical_quantity=plan["before"], new_theoretical_quantity=plan["after"],
            material_input_delta=sum(entry["stock_after"]-entry["stock_before"] for entry in inputs),
            material_input_cumulative=sum(entry["stock_after"] for entry in inputs), operator_id=actor.id,
            idempotency_key=key, capitalized_material_cost=plan["total_cost"],
            cost_detail={**plan["detail"], "semi_production_request_hash": request_hash},
            bom_snapshot_id=context.snapshot.id, semi_only=True)
        assemblies = assemble_graph_order_receipt(db, compiled=context.compiled, order_item_id=order_item_id,
            operation_key=f"bom-semi-assembly:{completion.id}", operator_id=actor.id)
        if completion.warehouse_location_id != preview["production_location"]["id"] or any(
                db.get(InventoryLot, row.output_lot_id).warehouse_location_id != preview["target_locations"][row.output_product_id]
                for row in assemblies if row.output_lot_id):
            raise BomPlanError("实际生产或组装位置与预览不一致")
        refresh_graph_main_task(db, item, create_if_missing=True)
        refresh_order_production_status(db, item.order_id)
        if _map_hash() != preview["map_hash"]:
            raise BomPlanError("生产过程中地图版本已变化，请重新核对")
        append_audit_event(db, event_category="business", result="success", source="web", module_code="orders",
            action_code="confirm_bom_semi_production", resource="production_completion", actor=actor,
            entity_type="production_completion", entity_id=completion.id, customer_id=context.compiled.graph.customer_id,
            details=dict(request_hash=request_hash, preview=preview, input_plan=plan))
        return dict(completion_id=completion.id, lot_id=completion.inventory_lot_id, quantity=completion.quantity)


def reverse_semi_production(db, *, order_item_id, completion_id, actor):
    from app.services.production_workflow import reverse_production_completion, refresh_order_production_status
    from app.services.multilevel_bom_inventory import reverse_order_assembly
    from app.services.audit_log import append_audit_event
    if actor is None or not actor.is_active or actor.role != "admin":
        raise BomPlanError("仅活动管理员可撤销半成品生产")
    with atomic_bom(db):
        completion = db.get(ProductionCompletion, completion_id)
        lot = db.get(InventoryLot, completion.inventory_lot_id) if completion else None
        detail = json.loads(lot.cost_snapshot_detail_json or "{}") if lot else {}
        if (completion is None or completion.order_item_id != order_item_id or completion.origin != "manual"
                or not detail.get("bom_semi_confirmation") or not detail.get("semi_production_request_hash")):
            raise BomPlanError("该完工不是本订单明确确认的半成品生产")
        if completion.status == "reversed":
            return dict(completion_id=completion.id, status="reversed")
        reverse_order_assembly(db, order_item_id=order_item_id,
            operation_key=f"bom-semi-assembly:{completion.id}", operator_id=actor.id,
            source_snapshot_id=detail["bom_snapshot_id"])
        reverse_production_completion(db, completion_id=completion.id, operator_id=actor.id, reason="撤销半成品预占生产确认")
        from app.services.multilevel_bom_receipts import refresh_graph_main_task
        refresh_graph_main_task(db, db.get(OrderItem, order_item_id), create_if_missing=False)
        refresh_order_production_status(db, db.get(OrderItem, order_item_id).order_id)
        from app.models.order import Order
        order = db.get(Order, db.get(OrderItem, order_item_id).order_id)
        append_audit_event(db, event_category="business", result="success", source="web", module_code="orders",
            action_code="reverse_bom_semi_production", resource="production_completion", actor=actor,
            entity_type="production_completion", entity_id=completion.id, customer_id=order.customer_id,
            details=dict(original_request_hash=detail["semi_production_request_hash"]))
        return dict(completion_id=completion.id, status="reversed")
