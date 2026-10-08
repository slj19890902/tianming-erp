"""Atomic bottom-up orchestration over real finished component balances.

Receipt adapters must first post physical-piece completion and supply explicit
versioned lots/locations. This service never turns raw board into finished units
or searches unrelated customer stock. Each node (including zero output) stores
the same request manifest, so partial replay cannot repeat a receipt assembly.
"""
import hashlib
import json
from contextlib import nullcontext
from collections import defaultdict

from sqlalchemy import select, or_

from app.models.multilevel_bom import BomAssembly, BomAssemblyInput
from app.models.order import Order, OrderItem
from app.models.warehouse_inventory import InventoryLot, InventoryReservation
from app.services.bom_subkit_inventory import assemble_subkit_inventory, reverse_subkit_conversion, _hash
from app.services.bom_subkits import SubkitError, active_subkit_order
from app.services.bom_transactions import atomic_bom
from app.services.multilevel_bom_orders import read_compiled_order_bom
from app.services.multilevel_bom_plan import plan_assembly


def _node_keys(operation_key, product_ids):
    if not isinstance(operation_key, str) or not operation_key or len(operation_key) > 100:
        raise SubkitError("逐层组装操作标识无效", 400)
    prefix = hashlib.sha256(operation_key.encode()).hexdigest()
    return {pid: f"graph:{prefix}:{pid}" for pid in product_ids}


def assemble_order_inventory(db, *, order_item_id, source_lot_versions,
                             target_locations, operation_key, operator_id,
                             available_lot_ids, preview_only=False, expected_outputs=None):
    """All layers commit or roll back with the caller; no service commit.

    Free stock must be explicitly authorized through available_lot_ids. Other
    supplied lots contribute ONLY this order's active component reservations.
    target_locations is keyed by every frozen assembled Product ID.
    """
    with (nullcontext() if preview_only else atomic_bom(db)):
        compiled = read_compiled_order_bom(db, order_item_id)
        if compiled is None:
            raise SubkitError("订单缺少完整多级BOM快照")
        nodes, children, topo = compiled.graph.validated()
        from app.services.bom_inventory_contract import body_product_ids, is_body_lot
        body_ids = body_product_ids(compiled.graph)
        product_ids = [pid for pid in reversed(topo) if nodes[pid].source == "assembled" or pid in body_ids]
        keys = _node_keys(operation_key, product_ids)
        if not product_ids:
            raise SubkitError("订单没有需要逐层组装的产品")
        if not preview_only and (set(target_locations) != set(product_ids) or any(
            type(pid) is not int or type(lid) is not int or lid <= 0
            for pid, lid in target_locations.items()
        )):
            raise SubkitError("每个组装产品必须指定有效目标货位")
        if any(type(lid) is not int or lid <= 0 or type(v) is not int or v <= 0
               for lid, v in source_lot_versions.items()):
            raise SubkitError("组装来源货位版本无效")
        if available_lot_ids is None or any(type(lid) is not int for lid in available_lot_ids):
            raise SubkitError("必须明确本次可用的组装批次")
        free_ids = set(available_lot_ids)
        if len(free_ids) != len(available_lot_ids) or not free_ids.issubset(source_lot_versions):
            raise SubkitError("可用组装批次重复或不属于本次来源")
        manifest = _hash({"schema": 1, "order_item_id": order_item_id,
            "lots": sorted(source_lot_versions.items()), "targets": sorted(target_locations.items()),
            "available_lot_ids": sorted(free_ids), "operator_id": operator_id})
        if expected_outputs is not None:
            manifest = _hash({"manifest": manifest, "expected_outputs": sorted(expected_outputs.items())})
        legacy_operation = {"key": operation_key, "request_hash": manifest, "product_ids": product_ids}
        operation = {**legacy_operation, "source_ids": sorted(row.id for row in compiled.snapshots)}
        existing = [] if preview_only else list(db.scalars(select(BomAssembly).where(BomAssembly.idempotency_key.in_(keys.values()))))
        if existing:
            by_key = {r.idempotency_key: r for r in existing}
            if len(by_key) != len(keys) or any(
                r.status != "posted" or r.order_item_id != order_item_id
                or keys.get(r.output_product_id) != r.idempotency_key
                or (json.loads(r.cost_detail_json).get("graph_operation") != operation
                    and not (compiled.rule_revision_id is None
                             and json.loads(r.cost_detail_json).get("graph_operation") == legacy_operation))
                for r in existing
            ):
                raise SubkitError("逐层组装标识已使用、载荷不一致或已撤销")
            return tuple(by_key[keys[pid]] for pid in product_ids)

        item = db.get(OrderItem, order_item_id)
        order = db.get(Order, item.order_id)
        from app.services.production_workflow import lock_order_rows_for_production_transition
        if not preview_only:
            lock_order_rows_for_production_transition(db, [order.id])
        snapshot_ids = {r.id for r in compiled.snapshots}
        from app.services.composite_bom_workflow import _remaining_reservation_quantity
        reserved = defaultdict(int)
        from app.services.processed_component_stock import processed_reservations, eligible_output
        from app.services.finished_stock_identity import compiled_product_bases
        processed = processed_reservations(db,compiled,item.id)
        for r in processed:
            if r.inventory_lot_id in source_lot_versions:
                reserved[r.inventory_lot_id] += _remaining_reservation_quantity(r)
        for r in db.scalars(select(InventoryReservation).where(
            InventoryReservation.order_item_id == item.id,
            or_(InventoryReservation.sales_order_item_bom_component_id.in_(snapshot_ids),
                InventoryReservation.sales_order_item_bom_component_id.is_(None)),
            InventoryReservation.reservation_type == "finished_order",
            InventoryReservation.inventory_lot_id.in_(source_lot_versions),
            InventoryReservation.status.in_(("active", "partial")),
        )):
            reserved[r.inventory_lot_id] += _remaining_reservation_quantity(r)
        from app.services.fixed_shelf_staging import staging_owner
        lots, balances, eligible_by_lot = {}, defaultdict(int), {}
        body_balances = defaultdict(int)
        identities = {}
        for lid, version in source_lot_versions.items():
            lot = db.get(InventoryLot, lid)
            if lot is None or lot.version != version or lot.status != "active":
                raise SubkitError("逐层组装来源库存状态或版本已变化")
            from app.services.multilevel_bom_body_inventory import stock_product_identity
            pid, customer_id = stock_product_identity(db, lot)
            is_body = is_body_lot(lot)
            if is_body and lot.inventory_type == 'finished':
                from app.services.multilevel_bom_body_inventory import validate_body_execution
                validate_body_execution(db, compiled, lot)
            elif is_body:
                from app.models.multilevel_bom import BomBodyInventoryDetail
                body = db.get(BomBodyInventoryDetail, lid)
                if (pid not in body_ids or body.order_item_id != item.id
                        or lot.quantity_reserved or reserved[lid]):
                    raise SubkitError("组装本体与订单不一致或存在异常预占")
            owner = active_subkit_order(db, lot)
            if (pid not in nodes or customer_id != order.customer_id
                    or (not is_body and lot.finished_detail is not None and lot.finished_detail.is_general) or staging_owner(db, lid)
                    or (owner is not None and owner != item.id)):
                raise SubkitError("逐层组装来源产品、客户、订单或集货状态不匹配")
            if not is_body and lot.inventory_type=='semi_finished' and not eligible_output(db,lot,product_id=pid,
                    customer_id=order.customer_id,expected_basis=compiled_product_bases(compiled)[pid]):
                raise SubkitError('已加工子件与订单冻结身份不一致')
            if reserved[lid] > lot.quantity_reserved:
                raise SubkitError("组装预占余额不一致")
            eligible = (lot.quantity_available if lid in free_ids else 0) + reserved[lid]
            (body_balances if is_body else balances)[pid] += eligible
            identities[lid] = pid
            eligible_by_lot[lid] = eligible
            lots[lid] = lot

        # Outputs outside this input selection or already commercially used
        # satisfy demand, but cannot be physically consumed in the new plan.
        # Inner units used in a still-posted outer assembly are represented by
        # that outer stock, not by another historical inner credit.
        used_in_assembly = defaultdict(int)
        for source, conversion in db.execute(select(BomAssemblyInput, BomAssembly).join(
            BomAssembly, BomAssembly.id == BomAssemblyInput.conversion_id).where(
                BomAssembly.order_item_id == item.id, BomAssembly.status == "posted")):
            used_in_assembly[source.lot_id] += source.quantity
        fulfilled = defaultdict(int)
        for conversion, lot in db.execute(select(BomAssembly, InventoryLot).join(
            InventoryLot, InventoryLot.id == BomAssembly.output_lot_id).where(
                BomAssembly.order_item_id == item.id, BomAssembly.status == "posted")):
            credit = (lot.quantity_available + lot.quantity_reserved + lot.quantity_consumed
                      - used_in_assembly[lot.id] - eligible_by_lot.get(lot.id, 0))
            if credit < 0:
                raise SubkitError("组装来源抵扣与库存流水不一致")
            fulfilled[conversion.output_product_id] += credit
        execution_quantity = (compiled.execution_window.execution_quantity
                              if compiled.execution_window is not None else item.quantity)
        plan = plan_assembly(compiled.graph, execution_quantity,
                             eligible_stock=balances, fulfilled_stock=fulfilled,
                             body_stock=body_balances, production_limits=expected_outputs)
        steps = {s.product_id: s for s in plan.steps}
        quantities = {pid: steps[pid].produced_units if pid in steps else 0 for pid in product_ids}
        if preview_only:
            return quantities
        if expected_outputs is not None and quantities != expected_outputs:
            raise SubkitError("待组套数量已变化，请刷新后按实际完成数量确认")
        results = []
        for pid in product_ids:
            child_ids = {e.child_id for e in children[pid] if e.relation == "assembly"}
            inputs = {lid: lot.version for lid, lot in lots.items()
                      if (not is_body_lot(lot) and lot.inventory_type in {"finished","semi_finished"} and identities[lid] in child_ids)
                      or (is_body_lot(lot) and identities[lid] == pid)}
            expected = steps[pid].produced_units if pid in steps else 0
            result = assemble_subkit_inventory(db, order_item_id=item.id, graph_product_id=pid,
                source_lot_versions=inputs, target_location_id=target_locations[pid],
                operation_key=keys[pid], operator_id=operator_id,
                available_lot_ids=sorted(free_ids.intersection(inputs)), quantity_limit=expected)
            if result.quantity != expected:
                raise SubkitError("逐层组装计划与实际扣减不一致，请重试")
            detail = json.loads(result.cost_detail_json)
            detail["graph_operation"] = operation
            result.cost_detail_json = json.dumps(detail)
            results.append(result)
            if result.output_lot_id:
                output = db.get(InventoryLot, result.output_lot_id)
                lots[output.id] = output
                identities[output.id] = pid
                free_ids.add(output.id)
        db.flush()
        return tuple(results)


def reverse_order_assembly(db, *, order_item_id, operation_key, operator_id, source_snapshot_id=None):
    """Reverse the exact recorded layers in reverse order, atomically."""
    with atomic_bom(db):
        if source_snapshot_id is None:
            compiled = read_compiled_order_bom(db, order_item_id)
        else:
            from app.services.multilevel_bom_orders import read_order_bom_source_contract
            compiled = read_order_bom_source_contract(db, order_item_id, source_snapshot_id)
        if compiled is None:
            raise SubkitError("订单缺少完整多级BOM快照")
        nodes, children, topo = compiled.graph.validated()
        pids = [pid for pid in reversed(topo) if nodes[pid].source == "assembled"
                or (nodes[pid].source == "manufactured" and any(e.relation == "assembly" for e in children[pid]))]
        keys = _node_keys(operation_key, pids)
        rows = {r.output_product_id: r for r in db.scalars(select(BomAssembly).where(
            BomAssembly.idempotency_key.in_(keys.values()), BomAssembly.order_item_id == order_item_id))}
        if not rows:
            # New receipts explicitly record "waiting", unlike a missing old
            # assembly ledger. Downstream component-use reversal guards remain.
            from app.models.audit import OperationLog
            waiting = db.scalar(select(OperationLog).where(OperationLog.action_code=='bom.awaiting_assembly',
                OperationLog.entity_type=='order_item',OperationLog.entity_id==order_item_id,
                OperationLog.object_ref==operation_key))
            if waiting and json.loads(waiting.details).get('source_ids') == sorted(s.id for s in compiled.snapshots):
                return ()
        if not pids or set(rows) != set(pids):
            raise SubkitError("逐层组装流水不完整，不能撤销")
        for pid in reversed(pids):
            row = rows[pid]
            operation = json.loads(row.cost_detail_json).get("graph_operation", {})
            if (operation.get("key") != operation_key or operation.get("product_ids") != pids
                    or ("source_ids" in operation
                        and operation["source_ids"] != sorted(source.id for source in compiled.snapshots))):
                raise SubkitError("逐层组装流水来源不匹配")
            reverse_subkit_conversion(db, conversion_id=row.id, operator_id=operator_id, graph_assembly=True)
        return tuple(rows[pid] for pid in pids)
