"""Purchase document links; quantities remain in the existing source ledgers.

This module never creates inventory, reservations or sales orders. Its caller owns
the transaction, including creation of ordinary/BOM purchase facts.
"""
from __future__ import annotations

import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import select, update

from app.models.customer import Customer
from app.models.procurement_source import ProcurementSourceLink
from app.models.stock_replenishment import StockReplenishmentOrder, StockReplenishmentOrderItem
from app.models.supplier_requisition_order import SupplierRequisitionOrderItem
from app.core.time_contract import utc_now_naive
from app.services.requisition_quantities import DEFAULT_CUTTING_MODE


def supplier_line_customer_expression():
    """Correlated expression used by both detail and list customer gates."""
    from sqlalchemy import func
    from app.models.order import Order, OrderItem
    from app.models.requisition import RequisitionItem
    line = SupplierRequisitionOrderItem
    ordinary = (select(Order.customer_id).join(OrderItem, OrderItem.order_id == Order.id)
        .where(OrderItem.id == line.order_item_id).correlate(line).scalar_subquery())
    stock = (select(func.coalesce(StockReplenishmentOrderItem.customer_id, StockReplenishmentOrder.customer_id))
        .select_from(ProcurementSourceLink)
        .join(StockReplenishmentOrderItem, StockReplenishmentOrderItem.id == ProcurementSourceLink.stock_replenishment_item_id)
        .join(StockReplenishmentOrder, StockReplenishmentOrder.id == StockReplenishmentOrderItem.replenishment_order_id)
        .where(ProcurementSourceLink.supplier_item_id == line.id).correlate(line).scalar_subquery())
    bom = (select(Order.customer_id).select_from(ProcurementSourceLink)
        .join(RequisitionItem, RequisitionItem.id == ProcurementSourceLink.material_requisition_item_id)
        .join(OrderItem, OrderItem.id == RequisitionItem.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(ProcurementSourceLink.supplier_item_id == line.id).correlate(line).scalar_subquery())
    return func.coalesce(ordinary, stock, bom)


def active_stock_purchase_clause():
    from sqlalchemy import or_
    from app.models.supplier_requisition_order import SupplierRequisitionOrder
    linked = (select(ProcurementSourceLink.id).join(SupplierRequisitionOrderItem,
        SupplierRequisitionOrderItem.id == ProcurementSourceLink.supplier_item_id)
        .join(SupplierRequisitionOrder, SupplierRequisitionOrder.id == SupplierRequisitionOrderItem.supplier_order_id)
        .where(ProcurementSourceLink.stock_replenishment_item_id == StockReplenishmentOrderItem.id,
               ProcurementSourceLink.status == "active", SupplierRequisitionOrder.status == "confirmed",
               SupplierRequisitionOrderItem.status == "active").exists())
    return or_(StockReplenishmentOrder.request_hash.is_(None), linked)


def internal_material_batch_clause():
    from app.models.requisition import Requisition, RequisitionItem
    return select(ProcurementSourceLink.id).join(RequisitionItem,
        RequisitionItem.id == ProcurementSourceLink.material_requisition_item_id).where(
            RequisitionItem.requisition_id == Requisition.id).exists()


def supplier_incoming_statuses(db, order_ids):
    """Project effective receipt facts through the existing typed source links."""
    if not order_ids:
        return {}
    from sqlalchemy import and_, case, func, or_
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.supplier_requisition_order import SupplierRequisitionOrder
    order, line, link, fact = (SupplierRequisitionOrder, SupplierRequisitionOrderItem,
                               ProcurementSourceLink, IncomingReceiptItem)
    # Each supplier line has at most one typed source link. OR matching counts a
    # receipt once even when both its direct and source identities match.
    rows = db.execute(select(
        order.id, order.status, line.id, line.requisition_qty,
        func.coalesce(func.sum(fact.received_quantity), 0),
        func.max(case((and_(fact.resolution_action == 'accept_short',
                            fact.resolution_status == 'resolved'), 1), else_=0)),
    ).select_from(order).outerjoin(line, and_(line.supplier_order_id == order.id, line.status == 'active'))
      .outerjoin(link, and_(link.supplier_item_id == line.id, link.status == 'active'))
      .outerjoin(fact, and_(fact.status == 'posted', or_(
          fact.supplier_order_item_id == line.id,
          fact.stock_replenishment_item_id == link.stock_replenishment_item_id,
          fact.requisition_item_id == link.material_requisition_item_id)))
      .where(order.id.in_(order_ids))
      .group_by(order.id, order.status, line.id, line.requisition_qty))
    progress = {}
    for order_id, status, line_id, quantity, received, closed in rows:
        state = progress.setdefault(order_id, {'status': status, 'complete': [], 'received': False})
        if line_id is not None:
            state['complete'].append(bool(closed or received >= quantity))
            state['received'] |= received > 0
    return {order_id: ('已作废' if state['status'] == 'voided' else
                      '已入库' if state['complete'] and all(state['complete']) else
                      '部分入库' if state['received'] else '待入库')
            for order_id, state in progress.items()}


def decorate_incoming_purchase_numbers(db, rows):
    """Keep the r/sr receipt identity, expose the actual unified supplier PO."""
    from app.models.supplier_requisition_order import SupplierRequisitionOrder
    stock_ids, material_ids = [], []
    for row in rows:
        key = str(row.get("item_id") or "")
        if key.startswith("sr") and key[2:].isdigit():
            stock_ids.append(int(key[2:]))
        elif key.startswith("r") and key[1:].isdigit():
            material_ids.append(int(key[1:]))
    from sqlalchemy import or_
    mapped = {}
    for link, order in db.execute(select(ProcurementSourceLink, SupplierRequisitionOrder)
        .join(SupplierRequisitionOrderItem, SupplierRequisitionOrderItem.id == ProcurementSourceLink.supplier_item_id)
        .join(SupplierRequisitionOrder, SupplierRequisitionOrder.id == SupplierRequisitionOrderItem.supplier_order_id)
        .where(ProcurementSourceLink.status == "active", or_(
            ProcurementSourceLink.stock_replenishment_item_id.in_(stock_ids),
            ProcurementSourceLink.material_requisition_item_id.in_(material_ids)))):
        key = f"sr{link.stock_replenishment_item_id}" if link.stock_replenishment_item_id else f"r{link.material_requisition_item_id}"
        mapped[key] = order
    for row in rows:
        purchase = mapped.get(str(row.get("item_id")))
        if purchase:
            row["source_document_number"] = row.get("supplier_order_number")
            row["supplier_order_number"] = purchase.order_number
            row["unified_supplier_order_id"] = purchase.id
    return rows


def stock_snapshot(item, order):
    fields = (
        "id", "replenishment_order_id", "target_inventory_type", "product_id",
        "reference_product_id", "customer_id", "material_id", "material_code_snapshot",
        "product_code_snapshot", "product_name_snapshot", "layer_count", "flute_type",
        "report_length_mm", "report_width_mm", "crease_type", "crease_left_mm",
        "crease_middle_mm", "crease_right_mm", "sheet_type", "component_type",
        "pieces_per_box", "stock_yield_per_sheet", "quantity", "procurement_route_snapshot",
    )
    quantity_contract = ({'quantity_contract_json': item.quantity_contract_json}
                        if item.quantity_contract_json is not None else {})
    return {**{key: getattr(item, key) for key in fields}, **quantity_contract,
            "source_type": order.source_type, "source_number": order.order_number,
            "source_customer_id": item.customer_id or order.customer_id,
            "supplier_name": order.supplier_name}


def snapshot_text(snapshot):
    return json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint(snapshot):
    return hashlib.sha256(snapshot_text(snapshot).encode("utf-8")).hexdigest()


def typed_purchase_snapshots(db, supplier_order_ids):
    return {link.supplier_item_id: json.loads(link.source_snapshot_json)
        for link in db.scalars(select(ProcurementSourceLink).join(SupplierRequisitionOrderItem,
            SupplierRequisitionOrderItem.id == ProcurementSourceLink.supplier_item_id)
            .where(SupplierRequisitionOrderItem.supplier_order_id.in_(supplier_order_ids))).all()}


def pending_stock_rows(db, user):
    from app.api.requisition import _require_stock_replenishment_order_access
    from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem, ExternalPackagingPurchaseCancellation
    rows = db.execute(select(StockReplenishmentOrderItem, StockReplenishmentOrder)
        .join(StockReplenishmentOrder, StockReplenishmentOrder.id == StockReplenishmentOrderItem.replenishment_order_id)
        .where(StockReplenishmentOrder.status.in_(("draft", "confirmed", "partially_stocked")),
               StockReplenishmentOrder.request_hash.is_not(None),
               ~select(ExternalPackagingPurchaseItem.id).where(
                   ExternalPackagingPurchaseItem.stock_replenishment_item_id == StockReplenishmentOrderItem.id,
                   ~select(ExternalPackagingPurchaseCancellation.id).where(
                       ExternalPackagingPurchaseCancellation.purchase_order_id == ExternalPackagingPurchaseItem.purchase_order_id
                   ).exists()).exists(),
               ~select(ProcurementSourceLink.id).where(
                   ProcurementSourceLink.stock_replenishment_item_id == StockReplenishmentOrderItem.id,
                   ProcurementSourceLink.status == "active").exists())
        .order_by(StockReplenishmentOrder.created_at, StockReplenishmentOrderItem.id)).all()
    result = []
    for item, order in rows:
        try:
            _require_stock_replenishment_order_access(db, order, user)
        except HTTPException as error:
            if error.status_code != 403:
                raise
            continue
        snapshot = stock_snapshot(item, order)
        customer = db.get(Customer, snapshot["source_customer_id"]) if snapshot["source_customer_id"] else None
        result.append({
            "source_type": "stock_replenishment", "stock_replenishment_item_id": item.id,
            "item_id": f"sr{item.id}", "id": f"sr{item.id}", "source_id": order.id,
            "source_fingerprint": fingerprint(snapshot), "is_merge_group": False,
            "item_order_number": order.order_number, "order_number": order.order_number,
            "customer_id": snapshot["source_customer_id"], "customer_name": customer.name if customer else "通用备料",
            "product_code": item.product_code_snapshot, "product_name": item.product_name_snapshot or item.internal_name,
            "quantity": item.quantity, "production_required_qty": item.quantity,
            "required_piece_qty": item.quantity * item.stock_yield_per_sheet,
            "remaining_required_piece_qty": item.quantity * item.stock_yield_per_sheet,
            "crease_type": item.crease_type, "crease_left_mm": item.crease_left_mm,
            "crease_middle_mm": item.crease_middle_mm, "crease_right_mm": item.crease_right_mm,
            "inventory_deducted_qty": 0, "requisition_qty": item.quantity,
            "report_length_mm": item.report_length_mm, "report_width_mm": item.report_width_mm,
            "material_id": item.material_id, "material": item.material_code_snapshot,
            "flute_type": item.flute_type, "layer_count": item.layer_count,
            "supplier_name": order.supplier_name, "snapshot_supplier_name": order.supplier_name,
            "source_snapshot": snapshot, "requisition_status": "未报料",
        })
        if item.quantity_contract_json is not None:
            from app.services.stock_warning_drafts import physical_demand_contract
            contract = physical_demand_contract(item)
            result[-1].update(procurement_mode='external_purchase',
                quantity_basis='physical', unit=contract['physical_unit'],
                external_purchase_quantity=str(item.quantity),
                external_purchase_unit=contract['physical_unit'],
                quantity_contract=contract)
    return result


def attach_stock_sources(db, *, purchase, selections, user):
    from app.api.requisition import _require_stock_replenishment_order_access, _require_active_material_supplier
    from app.models.material import Material
    seen = set()
    for selection in selections:
        source_id = selection.stock_replenishment_item_id
        if source_id in seen:
            raise HTTPException(409, "补库来源重复，不能重复采购")
        seen.add(source_id)
        item = db.get(StockReplenishmentOrderItem, source_id)
        if item is None:
            raise HTTPException(404, "补库明细不存在")
        order = db.get(StockReplenishmentOrder, item.replenishment_order_id)
        _require_stock_replenishment_order_access(db, order, user)
        snapshot = stock_snapshot(item, order)
        if fingerprint(snapshot) != selection.source_fingerprint:
            raise HTTPException(409, "补库需求已变化，请刷新采购草稿")
        if order.status not in {"draft", "confirmed", "partially_stocked"} or not order.request_hash:
            raise HTTPException(409, "补库需求不在统一待报料池")
        if item.stocked_quantity or item.procurement_route_snapshot == "external_packaging":
            raise HTTPException(409, "该来源已收货或属于外购包材，不能作为纸板需求重复采购")
        if order.supplier_name != purchase.supplier_name:
            raise HTTPException(409, "补库来源与采购供应商不一致")
        _require_active_material_supplier(db, db.get(Material, item.material_id))
        if db.scalar(select(ProcurementSourceLink.id).where(
            ProcurementSourceLink.stock_replenishment_item_id == item.id,
            ProcurementSourceLink.status == "active")):
            raise HTTPException(409, "该补库明细已生成采购单")
        # Claim before attaching. Multi-line source orders may be purchased in
        # separate groups; each remaining line is protected by the unique link.
        claim = db.execute(update(StockReplenishmentOrder).where(
            StockReplenishmentOrder.id == order.id,
            StockReplenishmentOrder.status == order.status)
            .values(status="confirmed" if order.status == "draft" else order.status,
                    confirmed_by=user.id, confirmed_at=order.confirmed_at or utc_now_naive()))
        if claim.rowcount != 1:
            raise HTTPException(409, "补库需求状态已变化")
        customer = db.get(Customer, snapshot["source_customer_id"]) if snapshot["source_customer_id"] else None
        line = SupplierRequisitionOrderItem(
            supplier_order_id=purchase.id, order_item_id=None, source_key=f"stock_replenishment:{item.id}",
            product_id=item.reference_product_id or item.product_id, material_id=item.material_id,
            material_code_snapshot=item.material_code_snapshot, supplier_name_snapshot=order.supplier_name,
            layer_count_snapshot=item.layer_count, flute_type_snapshot=item.flute_type,
            order_number=order.order_number, product_code=item.product_code_snapshot,
            product_name=item.product_name_snapshot or item.internal_name,
            report_length_mm=item.report_length_mm, report_width_mm=item.report_width_mm,
            quantity=item.quantity, stock_deduction_qty=0, requisition_qty=item.quantity,
            cutting_mode=DEFAULT_CUTTING_MODE, pieces_per_box=item.pieces_per_box,
            required_piece_qty=item.quantity * item.stock_yield_per_sheet,
            customer_name=customer.name if customer else "通用备料",
        )
        db.add(line)
        db.flush()
        db.add(ProcurementSourceLink(supplier_item_id=line.id,
            stock_replenishment_item_id=item.id, material_requisition_item_id=None,
            source_quantity=item.quantity, source_snapshot_json=snapshot_text(snapshot), created_by=user.id))
        purchase.total_quantity += item.quantity
        purchase.requisition_qty += item.quantity
        purchase.required_piece_qty = (purchase.required_piece_qty or 0) + line.required_piece_qty
    db.flush()


def release_stock_sources(db, purchase, user):
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.requisition import RequisitionItem
    from app.api.requisition import _void_composite_requisition_batch, CancelPayload
    links = db.scalars(select(ProcurementSourceLink).join(SupplierRequisitionOrderItem,
        SupplierRequisitionOrderItem.id == ProcurementSourceLink.supplier_item_id)
        .where(SupplierRequisitionOrderItem.supplier_order_id == purchase.id,
               ProcurementSourceLink.status == "active")).all()
    bom_groups = {}
    for link in links:
        if link.material_requisition_item_id:
            source = db.get(RequisitionItem, link.material_requisition_item_id)
            bom_groups.setdefault((source.requisition_id, source.order_item_id), []).append(link)
    for (batch_id, parent_id), group in bom_groups.items():
        _void_composite_requisition_batch(db=db, user=user, batch_id=batch_id, order_item_id=parent_id,
            payload=CancelPayload(reason=f"撤销统一采购单 {purchase.order_number}"), commit=False)
        for link in group:
            link.status = "voided"
    for link in links:
        if link.material_requisition_item_id:
            continue
        item = db.get(StockReplenishmentOrderItem, link.stock_replenishment_item_id)
        order = db.get(StockReplenishmentOrder, item.replenishment_order_id)
        claim = db.execute(update(StockReplenishmentOrder).where(
            StockReplenishmentOrder.id == order.id,
            StockReplenishmentOrder.status.in_(("confirmed", "partially_stocked")))
            .values(status=StockReplenishmentOrder.status))
        if claim.rowcount != 1:
            raise HTTPException(409, "补库来源状态已变化，请刷新")
        db.refresh(item)
        receipt = db.scalar(select(IncomingReceiptItem.id).where(
            IncomingReceiptItem.stock_replenishment_item_id == item.id,
            IncomingReceiptItem.status == "posted").limit(1))
        if receipt or item.stocked_quantity or item.inventory_lot_id:
            raise HTTPException(409, "补库来源已有实际收货或库存事实，请先撤销对应来料")
        from app.services.raw_purchase_plans import void_unreceived_plan
        void_unreceived_plan(db,item.id,user)
        link.status = "voided"
    db.flush()


def attach_bom_sources(db, *, purchase, lines, user):
    from app.api.requisition import (RequisitionBatchCreate, _create_batch_locked,
        _bom_snapshot_crease, _component_crease, _requisition_item_component)
    from app.models.requisition import Requisition, RequisitionItem
    from app.models.product_bom import RequisitionItemBomSource, SalesOrderItemBomComponent
    from app.models.order import Order, OrderItem
    if not lines:
        return
    result = _create_batch_locked(db=db, user=user, commit=False,
        payload=RequisitionBatchCreate(supplier_name=purchase.supplier_name,
            request_key=hashlib.sha256((purchase.request_key + ":bom").encode()).hexdigest(), items=lines))
    batch = db.get(Requisition, result["id"])
    for source in db.scalars(select(RequisitionItem).where(RequisitionItem.requisition_id == batch.id)).all():
        order_item = db.get(OrderItem, source.order_item_id)
        sales_order = db.get(Order, order_item.order_id)
        customer = db.get(Customer, sales_order.customer_id)
        bom_source = db.scalar(select(RequisitionItemBomSource).where(RequisitionItemBomSource.requisition_item_id == source.id))
        bom = db.get(SalesOrderItemBomComponent, bom_source.sales_order_item_bom_component_id) if bom_source else None
        component = bom_source.component_type if bom_source else _requisition_item_component(source)
        crease = _bom_snapshot_crease(bom, component) if bom else _component_crease(order_item, component)
        snapshot = {
            "source_type": "bom_component", "material_requisition_item_id": source.id,
            "source_id": batch.id, "source_number": batch.requisition_number,
            "source_customer_id": customer.id, "order_item_id": order_item.id,
            "bom_snapshot_id": bom.id if bom else None, "component_type": component,
            "crease_type": crease[0], "crease_left_mm": crease[1], "crease_middle_mm": crease[2], "crease_right_mm": crease[3],
            "quantity": source.requisition_qty, "direction_note": bom_source.direction_note if bom_source else None,
        }
        line = SupplierRequisitionOrderItem(supplier_order_id=purchase.id,
            order_item_id=None, source_key=f"material_requisition:{source.id}:{component}",
            material_id=bom.snapshot_component_material_id if bom else order_item.material_id,
            material_code_snapshot=source.material_snapshot,
            supplier_name_snapshot=purchase.supplier_name,
            layer_count_snapshot=bom.snapshot_component_layer_count if bom else order_item.layer_count,
            flute_type_snapshot=bom.snapshot_component_flute_type if bom else order_item.flute_type,
            order_number=order_item.item_order_number, product_code=source.product_code_snapshot,
            product_name=source.product_name_snapshot, report_length_mm=int(source.cardboard_len),
            report_width_mm=int(source.cardboard_width), quantity=source.required_piece_qty,
            requisition_qty=source.requisition_qty, stock_deduction_qty=source.inventory_deducted_qty,
            cutting_mode=source.special_process, pieces_per_box=source.pieces_per_box,
            required_piece_qty=source.required_piece_qty, customer_name=customer.name,
            delivery_date=sales_order.delivery_date)
        db.add(line)
        db.flush()
        db.add(ProcurementSourceLink(supplier_item_id=line.id, material_requisition_item_id=source.id,
            stock_replenishment_item_id=None, source_quantity=source.requisition_qty,
            source_snapshot_json=snapshot_text(snapshot), created_by=user.id))
        purchase.total_quantity += int(source.required_piece_qty)
        purchase.requisition_qty += int(source.requisition_qty)
        purchase.required_piece_qty = (purchase.required_piece_qty or 0) + int(source.required_piece_qty)
    db.flush()
