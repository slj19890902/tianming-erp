"""Read an actual delivery's pick recipe from its immutable source identities."""
import json
from sqlalchemy import select, case, func

from app.models.delivery import DeliveryItem
from app.models.multilevel_bom import OrderBomGraph, OrderBomRuleRevision, OrderBomExecutionCutover, OrderBomCutoverSource
from app.models.order import OrderItem
from app.models.product_bom import SalesOrderItemBomComponent, BomComponentDirectDeliveryAllocation
from app.models.warehouse_inventory import DeliveryInventoryAllocation, InventoryReservation, InventoryLot
from app.services.multilevel_bom_plan import BomPlanError


def _completion_source_id(db, lot_id, order_item_id):
    """Old root reservations may omit a source; actual output evidence owns it."""
    from app.models.production import ProductionCompletion
    lot = db.get(InventoryLot, lot_id)
    if lot is None or lot.source_ref_type != "production_completion":
        return None
    completion = db.get(ProductionCompletion, lot.source_ref_id)
    original = db.get(InventoryLot, completion.inventory_lot_id) if completion else None
    if (completion is None or completion.order_item_id != order_item_id or original is None
            or original.source_ref_type != lot.source_ref_type or original.source_ref_id != lot.source_ref_id
            or lot.finished_detail is None or original.finished_detail is None
            or (lot.finished_detail.product_id, lot.finished_detail.owner_customer_id)
                != (original.finished_detail.product_id, original.finished_detail.owner_customer_id)):
        raise BomPlanError("历史父件库存与原完工身份不一致")
    try:
        detail = json.loads(original.cost_snapshot_detail_json or "{}")
        if detail != json.loads(lot.cost_snapshot_detail_json or "{}") or not isinstance(detail, dict):
            raise ValueError("output identity differs")
        sid = detail.get("bom_snapshot_id")
        if sid is None:
            return None
        source = db.get(SalesOrderItemBomComponent, sid) if type(sid) is int and sid > 0 else None
        if (source is None or source.sales_order_item_id != order_item_id
                or source.component_product_id != lot.finished_detail.product_id
                or detail.get("bom_material_product_id") != source.component_product_id):
            raise ValueError("source identity differs")
    except (ValueError, TypeError) as error:
        raise BomPlanError("历史父件完工缺少准确冻结来源") from error
    return sid


def root_reservation_source_expression(db, roots):
    """Resolve old unbound roots once; never assign them to every revision."""
    bound = InventoryReservation.sales_order_item_bom_component_id
    if not roots:
        return bound
    revised = set(db.scalars(select(OrderBomRuleRevision.order_item_id).where(
        OrderBomRuleRevision.order_item_id.in_(roots)).distinct()))
    proven = {}
    if revised:
        for reservation in db.scalars(select(InventoryReservation).where(
                InventoryReservation.order_item_id.in_(revised),
                bound.is_(None), InventoryReservation.reservation_type == "finished_order")):
            sid = _completion_source_id(db, reservation.inventory_lot_id, reservation.order_item_id)
            if sid is None:
                raise BomPlanError("跨版本父件预占缺少准确冻结来源，须先核实原批次依据")
            proven[reservation.id] = sid
    unchanged = {oid: sid for oid, sid in roots.items() if oid not in revised}
    fallback = case(unchanged, value=InventoryReservation.order_item_id) if unchanged else None
    if proven:
        fallback = case(proven, value=InventoryReservation.id, else_=fallback)
    return func.coalesce(bound, case(
        (InventoryReservation.reservation_type == "finished_order", fallback), else_=None))


def historical_delivery_component_demands(db, *, delivery_item_id, order_item_id):
    from app.services.composite_bom_workflow import effective_component_demands, project_graph_delivery_demands
    from app.services.multilevel_bom_orders import read_compiled_order_bom, read_order_bom_source_contract
    line = db.get(DeliveryItem, delivery_item_id)
    if line is None or line.order_item_id != order_item_id:
        raise BomPlanError("历史拿货明细与订单身份不一致")
    demands = effective_component_demands(db, order_item_id)
    if db.get(OrderBomGraph, order_item_id) is None:
        from app.services.legacy_accompany import frozen_demands
        legacy = frozen_demands(db, order_item_id, demands)
        if legacy is not None:
            # Actual allocation rows, not the current master, define new prints.
            allocated = set(db.scalars(select(InventoryReservation.sales_order_item_bom_component_id)
                .join(DeliveryInventoryAllocation, DeliveryInventoryAllocation.reservation_id == InventoryReservation.id)
                .where(DeliveryInventoryAllocation.delivery_item_id == delivery_item_id)))
            if any(d.snapshot_id in allocated for d in legacy):
                return legacy
        return demands
    # Retain reversed allocations too: cancellation does not change which
    # frozen recipe originally owned this delivery. No product master lookup.
    stock = list(db.execute(select(InventoryReservation.sales_order_item_bom_component_id,
        InventoryReservation.order_item_id, InventoryReservation.inventory_lot_id).join(DeliveryInventoryAllocation,
        DeliveryInventoryAllocation.reservation_id == InventoryReservation.id).where(
            DeliveryInventoryAllocation.delivery_item_id == line.id)))
    if any(owner != order_item_id for _, owner, _ in stock):
        raise BomPlanError("历史拿货预占不属于本订单")
    direct_ids = list(db.scalars(select(BomComponentDirectDeliveryAllocation.sales_order_item_bom_component_id).where(
        BomComponentDirectDeliveryAllocation.delivery_item_id == line.id)))
    source_ids = {sid for sid, _, _ in stock if sid is not None} | set(direct_ids)
    for sid, _, lot_id in stock:
        if sid is None:
            proven = _completion_source_id(db, lot_id, order_item_id)
            if proven is not None:
                source_ids.add(proven)
    item = db.get(OrderItem, order_item_id)
    if not source_ids:
        # A not-yet-allocated dispatch uses its current frozen rule. An actual
        # parent allocation without a source must not guess across revisions.
        if stock and (db.get(OrderBomExecutionCutover, order_item_id) is not None
                or db.scalar(select(OrderBomRuleRevision.id).where(
                    OrderBomRuleRevision.order_item_id == order_item_id).limit(1)) is not None):
            raise BomPlanError("历史父件拿货缺少准确BOM来源，不能按当前版本推测")
        compiled = read_compiled_order_bom(db, order_item_id)
    else:
        rows = list(db.scalars(select(SalesOrderItemBomComponent).where(SalesOrderItemBomComponent.id.in_(source_ids))))
        if len(rows) != len(source_ids) or any(row.sales_order_item_id != order_item_id for row in rows):
            raise BomPlanError("历史拿货BOM来源缺失或跨订单")
        graph_rows = [row for row in rows if row.snapshot_schema_version == 5]
        if not graph_rows:
            read_compiled_order_bom(db, order_item_id)  # validate the complete conversion/history manifest
            legacy_ids = set(db.scalars(select(OrderBomCutoverSource.snapshot_id).where(
                OrderBomCutoverSource.order_item_id == order_item_id, OrderBomCutoverSource.role == "history")))
            if not source_ids.issubset(legacy_ids) or any(not 1 <= (row.snapshot_schema_version or 0) <= 4 for row in rows):
                raise BomPlanError("旧拿货来源不属于已核验的历史转换")
            return [demand for demand in demands if demand.snapshot_id in source_ids]
        if len(graph_rows) != len(rows):
            raise BomPlanError("历史拿货混用了旧组件与真实BOM来源")
        compiled = read_order_bom_source_contract(db, order_item_id, graph_rows[0].id)
        if not source_ids.issubset({source.id for source in compiled.snapshots}):
            raise BomPlanError("同一送货明细混用了不同BOM规则版本")
    projected = project_graph_delivery_demands(compiled, item, demands)
    picked = {d.snapshot_id: d.component_product_id for d in projected}
    for sid, _, lot_id in stock:
        expected_product = picked.get(sid) if sid is not None else compiled.graph.root_id
        lot = db.get(InventoryLot, lot_id)
        if (expected_product is None or lot is None or lot.finished_detail is None
                or lot.finished_detail.owner_customer_id != compiled.graph.customer_id
                or lot.finished_detail.product_id != expected_product):
            raise BomPlanError("历史拿货批次与冻结交付产品或客户不一致")
    if not set(direct_ids).issubset(picked):
        raise BomPlanError("历史直接交付包含不属于拿货规则的来源")
    return projected
