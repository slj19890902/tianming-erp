"""Explicit rule handoff when existing reserved finished stock covers the remainder."""
from collections import defaultdict
from dataclasses import asdict, dataclass
from types import SimpleNamespace
import hashlib
import json

from sqlalchemy import select, update

from app.models.order import Order, OrderItem
from app.models.delivery import Delivery, DeliveryItem
from app.models.production import ProductionTask, ProductionCompletion
from app.models.requisition import RequisitionItem
from app.models.product_bom import SalesOrderItemBomComponent
from app.models.multilevel_bom import OrderBomRuleRevision, OrderBomExecutionCutover, BomAssembly
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot, InventoryReservation, InventoryMovement
from app.models.external_packaging_purchase import (
    ExternalPackagingPurchaseItem, ExternalPackagingPurchaseOrder,
    ExternalPackagingReceiptItem, ExternalPackagingPurchaseCancellation,
)
from app.services.multilevel_bom_rule_impact import review_current_rule_requirements
from app.services.multilevel_bom_cutover_review import _row
from app.services.multilevel_bom_plan import BomPlanError, plan_bom, plan_assembly
from app.services.finished_stock_identity import compiled_product_bases


@dataclass
class StockedHandoffReview:
    rule: object
    active: list
    lots: dict
    retain: dict
    assembly: object
    document: str
    preview: dict
    purchase_sources: tuple = ()
    material_sources: tuple = ()


def _closed_procurement(db, item):
    """No supplier obligation is silently abandoned by a stock handoff."""
    from app.services.external_receipt_state import active_receipt_item
    from app.services.multilevel_bom_external_identity import read_external_source_contract
    paper = list(db.scalars(select(RequisitionItem).where(RequisitionItem.order_item_id == item.id)
                            .order_by(RequisitionItem.id)))
    for line in paper:
        if line.status != "已入库":
            raise BomPlanError(f"纸板报料行#{line.id}仍为{line.status}，须先明确未收采购的处理，不能仅凭成品覆盖关闭它")
    external = []
    for line in db.scalars(select(ExternalPackagingPurchaseItem).where(
            ExternalPackagingPurchaseItem.sales_order_item_id == item.id).order_by(ExternalPackagingPurchaseItem.id)):
        header = db.get(ExternalPackagingPurchaseOrder, line.purchase_order_id)
        if header is None or line.sales_order_id != item.order_id or header.status != "confirmed":
            raise BomPlanError(f"外购采购行#{line.id}缺少原订单确认身份")
        link, _ = read_external_source_contract(db, line.order_component_id)
        if link.order_item_id != item.id:
            raise BomPlanError("外购采购与本订单冻结来源不一致")
        cancellation = db.scalar(select(ExternalPackagingPurchaseCancellation).where(
            ExternalPackagingPurchaseCancellation.purchase_order_id == header.id))
        receipts = list(db.scalars(select(ExternalPackagingReceiptItem).where(
            ExternalPackagingReceiptItem.purchase_item_id == line.id).order_by(ExternalPackagingReceiptItem.id)))
        active_ids = set(db.scalars(select(ExternalPackagingReceiptItem.id).where(
            ExternalPackagingReceiptItem.purchase_item_id == line.id, active_receipt_item())))
        received = sum(row.received_quantity for row in receipts if row.id in active_ids)
        if cancellation is None and received < line.purchase_quantity:
            raise BomPlanError(f"外购采购{header.purchase_number}行#{line.id}尚欠收{line.purchase_quantity-received}{line.purchase_unit}，须先交接采购余量")
        if cancellation is not None and (cancellation.cancelled_by is None or not cancellation.reason.strip()):
            raise BomPlanError("原采购撤销缺少管理员及原因")
        external.append(dict(line=_row(line), header=_row(header),
            cancellation=_row(cancellation) if cancellation else None,
            receipts=[_row(row) for row in receipts], active_receipt_ids=sorted(active_ids)))
    return dict(paper=[_row(row) for row in paper], external=external)


def review_stocked_handoff(db, *, order_item_id, customer_id, target_locations, carry_purchases=False, carry_materials=False):
    carry_purchases = carry_purchases or carry_materials
    rule = review_current_rule_requirements(db, order_item_id=order_item_id, customer_id=customer_id)
    item = db.get(OrderItem, order_item_id)
    order = db.get(Order, item.order_id)
    purchase_sources = ()
    material_sources = ()
    if carry_purchases:
        from app.services.multilevel_bom_procurement_impact import review_procurement_impact
        procurement = review_procurement_impact(db, order_item_id=item.id, customer_id=customer_id)
        for line in procurement["paper"]:
            if not carry_materials and line["line"]["status"] != "已入库":
                raise BomPlanError(f"纸板报料行#{line['line']['id']}尚未收齐，须交接材料及在制生产来源后切换")
            if carry_materials and (not line["mappings"] or any(
                    not mapping["material_conversion_compatible"] for mapping in line["mappings"])):
                raise BomPlanError(f"纸板报料行#{line['line']['id']}缺少兼容的原材料来源，请核对规格、工艺和开料换算")
        if carry_materials:
            material_sources = tuple(sorted({mapping["source"]["sales_order_item_bom_component_id"]
                for line in procurement["paper"] for mapping in line["mappings"]}))
        for line in procurement["external"]:
            if not line["mapping"]["purchase_conversion_compatible"]:
                raise BomPlanError(f"外购采购行#{line['line']['id']}原来源未交接或新规则实物/采购换算不同，请先核对原合同处理")
        purchase_sources = tuple(sorted({line["mapping"]["source_snapshot_id"] for line in procurement["external"]}))
        if not purchase_sources and not material_sources:
            raise BomPlanError("没有需要保留交接的原外购采购，请使用库存交接或未开始订单入口")
    else:
        procurement = _closed_procurement(db, item)
    deliveries = list(db.execute(select(DeliveryItem, Delivery).join(Delivery, Delivery.id == DeliveryItem.delivery_id)
        .where(DeliveryItem.order_item_id == item.id).order_by(DeliveryItem.id)))
    if any(document.status not in {"dispatched", "voided"} for _, document in deliveries):
        raise BomPlanError("订单还有未执行或集货中的送货单，请先明确处理后重新预览")
    reservations = list(db.scalars(select(InventoryReservation).where(InventoryReservation.order_item_id == item.id)
                                  .order_by(InventoryReservation.id)))
    active = [row for row in reservations
              if row.reserved_stock_quantity > row.consumed_stock_quantity + row.released_stock_quantity]
    semi_rows, semi_lots = [], {}
    if carry_materials:
        from app.services.multilevel_bom_carried_material import source_semi_reservations
        semi_rows = source_semi_reservations(db, item.id, material_sources, require_pending=True)
        allowed_semi = {row.id for row, _ in semi_rows}
        active = [row for row in active if row.id not in allowed_semi]
        semi_lots = {row.inventory_lot_id: db.get(InventoryLot, row.inventory_lot_id) for row, _ in semi_rows}
    from app.services.multilevel_bom_output_history import current_finished_reservation_condition
    current_ids = set(db.scalars(select(InventoryReservation.id).where(
        current_finished_reservation_condition(db, rule.previous))))
    if not active and not carry_purchases:
        raise BomPlanError("没有可交接的剩余预占；请先核对本订单的实际库存与未完成采购")
    old_bases = compiled_product_bases(rule.previous)
    new_bases = compiled_product_bases(rule.proposed)
    old_nodes = {node.product_id: node for node in rule.previous.graph.nodes}
    new_nodes = {node.product_id: node for node in rule.proposed.graph.nodes}
    lots, eligible, quantities = {}, defaultdict(int), defaultdict(int)
    from app.services.fixed_shelf_staging import staging_owner
    for row in active:
        if (row.id not in current_ids or row.reservation_type != "finished_order"
                or row.status not in {"active", "partial"} or row.yield_factor != 1
                or row.reserved_stock_quantity != row.credited_requirement_quantity
                or row.consumed_stock_quantity != row.consumed_requirement_quantity
                or row.released_stock_quantity != row.released_requirement_quantity):
            raise BomPlanError(f"预占#{row.id}不是当前版本一对一成品来源；半成品或其他历史版本须先逐项交接")
        lot = db.get(InventoryLot, row.inventory_lot_id)
        if (lot is None or lot.status != "active" or lot.inventory_type != "finished"
                or lot.finished_detail is None or lot.finished_detail.is_general
                or lot.finished_detail.owner_customer_id != customer_id):
            raise BomPlanError(f"预占#{row.id}的批次或客户身份不完整")
        pid = lot.finished_detail.product_id
        if pid not in old_bases or lot.finished_detail.physical_basis_json != old_bases[pid]:
            raise BomPlanError(f"批次{lot.lot_number}与原冻结规格工艺不一致，须先核实实物")
        if staging_owner(db, lot.id):
            raise BomPlanError(f"批次{lot.lot_number}仍在集货，请先还原集货")
        quantity = row.reserved_stock_quantity-row.consumed_stock_quantity-row.released_stock_quantity
        quantities[lot.id] += quantity
        lots[lot.id] = lot
        if pid in new_nodes and new_nodes[pid].source != "separate" and new_bases[pid] == old_bases[pid]:
            eligible[pid] += quantity
    if any(quantity > lots[lid].quantity_reserved for lid, quantity in quantities.items()):
        raise BomPlanError("本订单剩余预占超过批次实际预占余额")
    reserved_quantities = dict(quantities)
    if carry_purchases:
        from app.services.multilevel_bom_receipts import own_output_lots
        for lot in own_output_lots(db, item.id):
            if lot.quantity_available <= 0:
                continue
            if lot.finished_detail is None:
                raise BomPlanError(f"原订单批次{lot.lot_number}为{lot.inventory_type}，须明确待装配本体的库存交接，不能按成品预占处理")
            pid = lot.finished_detail.product_id
            if (lot.status != "active" or lot.finished_detail.owner_customer_id != customer_id
                    or lot.finished_detail.is_general or pid not in old_bases
                    or lot.finished_detail.physical_basis_json != old_bases[pid]):
                raise BomPlanError(f"原订单产出批次{lot.lot_number}的实物身份不完整")
            if staging_owner(db, lot.id):
                raise BomPlanError(f"原订单产出批次{lot.lot_number}仍在集货，请先还原集货")
            lots[lot.id] = lot
            quantities[lot.id] += lot.quantity_available
            if pid in new_nodes and new_nodes[pid].source != "separate" and new_bases[pid] == old_bases[pid]:
                eligible[pid] += lot.quantity_available
    remaining = item.quantity - (item.delivered_quantity or 0)
    plan = plan_bom(rule.proposed.graph, remaining, eligible_stock=eligible)
    shortage = [row for row in plan.products if row.make_units and new_nodes[row.product_id].source in {"manufactured", "purchased"}]
    blocking_shortage = [row for row in shortage if not carry_materials
        and (not carry_purchases or new_nodes[row.product_id].source != "purchased")]
    if blocking_shortage:
        row = blocking_shortage[0]
        raise BomPlanError(f"新规则尚缺{new_nodes[row.product_id].name} {row.make_units}{new_nodes[row.product_id].unit}的匹配成品；须继续生产/采购或明确原组装拆解方案")
    # Pending purchase capacity is not physical stock. Assembly waits for real
    # receipts; never manufacture a parent from a supplier obligation.
    assembly = (SimpleNamespace(steps=(), remaining_stock=tuple(eligible.items())) if shortage
        else plan_assembly(rule.proposed.graph, remaining, eligible_stock=eligible, body_stock={}))
    balances = dict(assembly.remaining_stock)
    if not shortage and any(balances[pid] < quantity for pid, quantity in plan.picking):
        raise BomPlanError("现有预占不能在新规则下配齐剩余交付，请核对需要拆解或缺少的实际子件")
    credit = {row.product_id: row.credited_units for row in plan.products}
    retain = {}
    for lid, lot in sorted(lots.items()):
        pid = lot.finished_detail.product_id
        take = min(quantities[lid], credit.get(pid, 0)) if new_bases.get(pid) == old_bases[pid] else 0
        retain[lid] = take
        credit[pid] = credit.get(pid, 0) - take
    assembled = {node.product_id for node in rule.proposed.graph.nodes if node.source == "assembled"
        or (node.source == "manufactured" and any(edge.parent_id == node.product_id and edge.relation == "assembly"
                                                  for edge in rule.proposed.graph.edges))} if assembly.steps else set()
    if (not isinstance(target_locations, dict) or set(target_locations)-assembled
            or any(type(pid) is not int or type(lid) is not int or lid <= 0 for pid, lid in target_locations.items())):
        raise BomPlanError("目标货位与本次实际组装产品不一致")
    from app.services.warehouse_inventory import _location
    locations = [_row(_location(db, lid, "finished")) for _, lid in sorted(target_locations.items())]
    from app.services.warehouse_twin_layout import resolve_warehouse_twin_layout_path
    map_hash = hashlib.sha256(resolve_warehouse_twin_layout_path().read_bytes()).hexdigest()
    from app.services.bom_subkit_costs import source_cost
    costs = []
    for lid, quantity in quantities.items():
        amount, lineage = source_cost(db, lots[lid], quantity)
        costs.append(dict(lot_id=lid, quantity=quantity, amount=str(amount), lineage=lineage,
                          unit=old_nodes[lots[lid].finished_detail.product_id].unit))
    def facts(model, condition):
        return [_row(row) for row in db.scalars(select(model).where(condition).order_by(model.id))]
    payload = dict(schema=1, rule=rule.document, order=_row(order), item=_row(item), procurement=procurement,
        semi_requirements=[_row(requirement) for _, requirement in semi_rows],
        semi_lots=[dict(lot=_row(lot), detail=_row(lot.semi_finished_detail)) for lot in semi_lots.values()],
        reservations=[_row(row) for row in reservations], lots=[_row(lot) for lot in lots.values()],
        finished=[_row(lot.finished_detail) for lot in lots.values()], costs=costs, retain=retain,
        tasks=facts(ProductionTask, ProductionTask.order_item_id == item.id),
        completions=facts(ProductionCompletion, ProductionCompletion.order_item_id == item.id),
        movements=facts(InventoryMovement, InventoryMovement.related_order_item_id == item.id),
        deliveries=[dict(line=_row(line), document=_row(document)) for line, document in deliveries],
        target_locations=target_locations, locations=locations, map_hash=map_hash)
    document = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    checksum = hashlib.sha256(document.encode()).hexdigest()
    steps = {step.product_id: step for step in assembly.steps}
    revision = db.scalar(select(OrderBomRuleRevision.revision).where(OrderBomRuleRevision.order_item_id == item.id)
        .order_by(OrderBomRuleRevision.revision.desc()).limit(1)) or 0
    preview = dict(scope=("保留原纸板及外购来源，交接匹配库存；按原材料换算继续收料生产" if carry_materials else
        "保留原外购合同及余量，交接匹配库存；缺少子件待实收后组装" if carry_purchases
        else "剩余需求已由当前版本真实成品预占完整覆盖的在制交接"), ready=set(target_locations)==assembled,
        order_item_id=item.id, quantity=item.quantity, execution_quantity=remaining,
        unit=new_nodes[rule.proposed.graph.root_id].unit,
        delivered_quantity=item.delivered_quantity or 0, preview_hash=checksum, reviewed_hash=checksum,
        rule_revision=revision, target_locations=target_locations,
        source_lot_versions={lid: lot.version for lid, lot in (lots | semi_lots).items()}, source_costs=costs,
        retained_semi=[dict(reservation_id=row.id, lot_id=row.inventory_lot_id,
            location_id=semi_lots[row.inventory_lot_id].warehouse_location_id,
            reserved_sheets=row.reserved_stock_quantity, consumed_sheets=row.consumed_stock_quantity,
            released_sheets=row.released_stock_quantity, physical_pieces=row.credited_requirement_quantity,
            estimated_unit_cost=str(semi_lots[row.inventory_lot_id].estimated_unit_cost_snapshot),
            cost_source=semi_lots[row.inventory_lot_id].cost_snapshot_source,
            yield_per_sheet=row.yield_factor) for row, _ in semi_rows],
        outputs=[dict(product_id=pid, name=new_nodes[pid].name, unit=new_nodes[pid].unit,
            quantity=steps[pid].produced_units if pid in steps else 0,
            consumed=[dict(name=new_nodes[cid].name, quantity=qty, unit=new_nodes[cid].unit)
                      for cid, qty in steps[pid].consumed] if pid in steps else []) for pid in sorted(assembled)],
        release_reservations=[dict(id=row.id, lot_id=row.inventory_lot_id,
            name=old_nodes[lots[row.inventory_lot_id].finished_detail.product_id].name,
            unit=old_nodes[lots[row.inventory_lot_id].finished_detail.product_id].unit,
            quantity=row.reserved_stock_quantity-row.consumed_stock_quantity-row.released_stock_quantity) for row in active],
        retained_locations=[dict(lot_id=lid, location_id=lot.warehouse_location_id,
            name=old_nodes[lot.finished_detail.product_id].name, unit=old_nodes[lot.finished_detail.product_id].unit,
            previous_available=lot.quantity_available,
            available_after_handoff=lot.quantity_available+reserved_quantities.get(lid, 0)-retain[lid],
            quantity=retain[lid], released_to_available=quantities[lid]-retain[lid]) for lid, lot in lots.items()],
        material_impact=("原报料、实收、完工及成本原样保留；后续报料扣除已交接材料后只报差额" if carry_materials
            else "剩余需求由已核验库存覆盖，不新增报料；原报料和完工事实保留"),
        procurement_impact=("原采购合同、价格和实收原样保留，未收余量交接新执行版本；不足部分在采购入口另行确认" if carry_purchases
            else "只接收已收齐或明确撤销的原采购，保留全部原合同和实收，不追加采购"),
        carried_purchases=procurement["external"] if carry_purchases else [],
        carried_materials=procurement["paper"] if carry_materials else [],
        inventory_impact="释放本订单剩余旧预占；所需数量绑定新来源，多余量留原批次可用；需组装时只消耗本次重新绑定的子件",
        cost_impact="沿用原批次成本来源，估算不升级实际；新组装只转移成本，旧已送成本不变",
        picking_impact="按新冻结交付规则与明确批次拿货；切换前已送不变",
        product_versions={node.product_id:node.version for node in rule.proposed.graph.nodes},
        new_modes=asdict(rule.proposed.graph.modes) if rule.proposed.graph.modes else None)
    return StockedHandoffReview(rule, active, lots, retain, assembly, document, preview, purchase_sources, material_sources)


def _result(db, row):
    from app.services.multilevel_bom_orders import read_compiled_order_bom
    read_compiled_order_bom(db, row.order_item_id)
    prefix = "graph:" + hashlib.sha256((row.idempotency_key+":assemble").encode()).hexdigest() + ":"
    assemblies = list(db.scalars(select(BomAssembly).where(BomAssembly.order_item_id == row.order_item_id,
        BomAssembly.idempotency_key.startswith(prefix)).order_by(BomAssembly.id)))
    return dict(order_item_id=row.order_item_id, rule_revision=row.revision, rule_revision_id=row.id,
        execution_quantity=row.order_quantity-row.delivered_before,
        assembly_ids=[assembly.id for assembly in assemblies],
        output_lot_ids=[assembly.output_lot_id for assembly in assemblies if assembly.output_lot_id])


def execute_stocked_handoff(db, *, order_item_id, customer_id, reviewed_hash, expected_revision,
                             target_locations, source_lot_versions, operation_key, actor, carry_purchases=False, carry_materials=False):
    if (type(expected_revision) is not int or expected_revision < 0
            or type(operation_key) is not str or not 1 <= len(operation_key.strip()) <= 64
            or type(reviewed_hash) is not str or len(reviewed_hash) != 64
            or any(char not in "0123456789abcdef" for char in reviewed_hash)):
        raise BomPlanError("在制交接版本、操作标识或预览摘要无效")
    if db.new or db.dirty or db.deleted:
        raise BomPlanError("请先保存或撤销未提交修改")
    request_hash = hashlib.sha256(json.dumps(dict(mode="material_graph" if carry_materials else
        "purchase_graph" if carry_purchases else "stocked_graph", item=order_item_id,
        customer=customer_id, review=reviewed_hash, revision=expected_revision, targets=target_locations,
        lot_versions=source_lot_versions, key=operation_key, actor=getattr(actor,"id",None)),
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    from app.services.bom_transactions import atomic_bom
    from app.services.audit_log import append_audit_event
    from app.services.warehouse_inventory import _balances, _movement, _number, _finished_reservation_status
    from app.core.time_contract import utc_now_naive
    with atomic_bom(db):
        actor = db.get(User, getattr(actor,"id",None), populate_existing=True)
        if actor is None or not actor.is_active or actor.role != "admin":
            raise BomPlanError("仅活动管理员可执行在制库存交接")
        existing = db.scalar(select(OrderBomRuleRevision).where(OrderBomRuleRevision.idempotency_key == operation_key))
        if existing is not None:
            if existing.request_hash != request_hash or existing.order_item_id != order_item_id:
                raise BomPlanError("同一交接标识的订单、版本或内容不一致")
            return _result(db, existing)
        if db.scalar(select(OrderBomExecutionCutover.order_item_id).where(
                OrderBomExecutionCutover.idempotency_key == operation_key)) is not None:
            raise BomPlanError("操作标识已用于旧单转换")
        item = db.get(OrderItem, order_item_id)
        if item is None:
            raise BomPlanError("订单明细不存在")
        db.execute(update(Order).where(Order.id == item.order_id).values(status=Order.status, updated_at=Order.updated_at))
        db.execute(update(OrderItem).where(OrderItem.id == item.id).values(quantity=OrderItem.quantity))
        review = review_stocked_handoff(db, order_item_id=order_item_id,
            customer_id=customer_id, target_locations=target_locations, carry_purchases=carry_purchases, carry_materials=carry_materials)
        if (not review.preview["ready"] or review.preview["reviewed_hash"] != reviewed_hash
                or review.preview["rule_revision"] != expected_revision
                or review.preview["source_lot_versions"] != source_lot_versions):
            raise BomPlanError("库存、订单、规则或目标地图已变化，请重新预览")
        for entry in review.preview["retained_semi"]:
            lid = entry["lot_id"]
            claimed = db.execute(update(InventoryLot).where(InventoryLot.id == lid,
                InventoryLot.version == source_lot_versions[lid]).values(version=InventoryLot.version))
            if claimed.rowcount != 1:
                raise BomPlanError("半成品预占批次已变化，请重新预览")
        previous = db.scalar(select(OrderBomRuleRevision).where(OrderBomRuleRevision.order_item_id == item.id)
            .order_by(OrderBomRuleRevision.revision.desc()))
        from app.services.multilevel_bom_rule_cutover import persist_reviewed_rule
        row = persist_reviewed_rule(db, review=review.rule, item=item, previous=previous,
            expected_revision=expected_revision, reviewed_hash=reviewed_hash, request_hash=request_hash,
            operation_key=operation_key, actor=actor)
        if review.purchase_sources or review.material_sources:
            from app.models.multilevel_bom import OrderBomSourceHandoff
            from app.services.multilevel_bom_execution_boundary import _source_identity
            targets = {source.component_product_id: source for source in review.rule.proposed.snapshots}
            for source_id in review.purchase_sources + review.material_sources:
                source = db.get(SalesOrderItemBomComponent, source_id)
                target = targets[source.component_product_id]
                db.add(OrderBomSourceHandoff(revision_id=row.id, source_snapshot_id=source.id,
                    target_snapshot_id=target.id, order_item_id=item.id, product_id=source.component_product_id,
                    source_kind="manufactured" if source_id in review.material_sources else "purchased",
                    source_basis_hash=_source_identity(source)["hash"],
                    target_basis_hash=_source_identity(target)["hash"]))
            db.flush()
            from app.services.multilevel_bom_orders import read_compiled_order_bom
            from app.services.multilevel_bom_source_handoffs import current_source_handoffs
            current_source_handoffs(db, read_compiled_order_bom(db, item.id))
            if review.material_sources:
                from app.services.multilevel_bom_carried_material import carried_material_pieces, carried_semi_pieces
                carried_material_pieces(db, read_compiled_order_bom(db, item.id))
                carried_semi_pieces(db, read_compiled_order_bom(db, item.id))
        order = db.get(Order, item.order_id)
        for reservation in review.active:
            lot = review.lots[reservation.inventory_lot_id]
            quantity = reservation.reserved_stock_quantity-reservation.consumed_stock_quantity-reservation.released_stock_quantity
            before = _balances(lot)
            changed = db.execute(update(InventoryLot).where(InventoryLot.id == lot.id,
                InventoryLot.version == lot.version, InventoryLot.quantity_reserved >= quantity).values(
                    quantity_reserved=InventoryLot.quantity_reserved-quantity,
                    quantity_available=InventoryLot.quantity_available+quantity,
                    version=InventoryLot.version+1, last_movement_at=utc_now_naive()))
            if changed.rowcount != 1:
                raise BomPlanError("释放原预占时库存已变化")
            reservation.released_stock_quantity += quantity
            reservation.released_requirement_quantity += quantity
            reservation.status = _finished_reservation_status(reservation)
            reservation.released_by = actor.id
            reservation.released_at = utc_now_naive()
            reservation.release_reason = "管理员核对在制BOM交接，旧消耗保留"
            db.flush()
            db.refresh(lot)
            _movement(db, lot=lot, movement_type="release_reserve", quantity=quantity, before=before,
                operator_id=actor.id, reason=reservation.release_reason,
                idempotency_key=f"{operation_key}:release:{reservation.id}", reservation_id=reservation.id,
                related_order_id=order.id, related_order_item_id=item.id)
        snapshots = {source.component_product_id:source for source in review.rule.proposed.snapshots}
        for lid, quantity in sorted(review.retain.items()):
            if not quantity:
                continue
            lot = review.lots[lid]
            before = _balances(lot)
            changed = db.execute(update(InventoryLot).where(InventoryLot.id == lid, InventoryLot.version == lot.version,
                InventoryLot.quantity_available >= quantity).values(quantity_available=InventoryLot.quantity_available-quantity,
                    quantity_reserved=InventoryLot.quantity_reserved+quantity, version=InventoryLot.version+1,
                    last_movement_at=utc_now_naive()))
            if changed.rowcount != 1:
                raise BomPlanError("绑定新版本预占时库存已变化")
            key = f"{operation_key}:retain:{lid}"
            reservation = InventoryReservation(reservation_number=_number("BHR"), inventory_lot_id=lid,
                reservation_type="finished_order", order_id=order.id, order_item_id=item.id,
                sales_order_item_bom_component_id=snapshots[lot.finished_detail.product_id].id,
                reserved_stock_quantity=quantity, credited_requirement_quantity=quantity,
                yield_factor=1, status="active", warning_codes="[]", reserved_by=actor.id,
                reserved_at=utc_now_naive(), idempotency_key=key)
            db.add(reservation)
            db.flush()
            db.refresh(lot)
            _movement(db, lot=lot, movement_type="reserve", quantity=quantity, before=before,
                operator_id=actor.id, reason="管理员在制BOM交接绑定原批次", idempotency_key=key,
                reservation_id=reservation.id, related_order_id=order.id, related_order_item_id=item.id)
        assemblies = ()
        if review.assembly.steps:
            from app.services.multilevel_bom_inventory import assemble_order_inventory
            assemblies = assemble_order_inventory(db, order_item_id=item.id,
                source_lot_versions={lid:review.lots[lid].version for lid, qty in review.retain.items() if qty},
                target_locations=target_locations, operation_key=operation_key+":assemble",
                operator_id=actor.id, available_lot_ids=[])
            from app.services.production_workflow import _reserve_component_completion_lot
            picking = dict(plan_bom(review.rule.proposed.graph, review.preview["execution_quantity"]).picking)
            for assembly in assemblies:
                if assembly.output_lot_id and assembly.output_product_id in picking:
                    lot = db.get(InventoryLot, assembly.output_lot_id)
                    _reserve_component_completion_lot(db, completion=assembly, order=order, item=item,
                        snapshot_id=snapshots[assembly.output_product_id].id, lot=lot, operator_id=actor.id,
                        idempotency_key=f"bom-output-reserve:{assembly.id}", reserve_quantity=lot.quantity_available,
                        reservation_number_prefix="BARS")
        if review.rule.proposed.graph.modes is not None:
            item.composite_fulfillment_mode_snapshot = (
                "component_delivery" if review.rule.proposed.graph.modes.delivery == "components" else "parent_delivery")
        # Fully covered stock does not create new material receipts or rewrite
        # completed production tasks. Dispatch reads the new inventory bindings.
        append_audit_event(db, event_category="business", result="success", source="web", module_code="orders",
            action_code="switch_material_graph_rule" if carry_materials else
                "switch_purchase_graph_rule" if carry_purchases else "switch_stocked_graph_rule", resource="order_bom_rule_revision", actor=actor,
            entity_type="order_item", entity_id=item.id, customer_id=customer_id,
            details=dict(rule_revision_id=row.id, review_hash=reviewed_hash, request_hash=request_hash,
                operation_key=operation_key, preview=review.preview, original_execution=json.loads(review.document),
                current_source_ids=[source.id for source in review.rule.proposed.snapshots],
                assembly_ids=[assembly.id for assembly in assemblies]))
        db.flush()
        return _result(db, row)
