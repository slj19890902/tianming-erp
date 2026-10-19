"""Read-only finished-stock provenance. Transfers retain their original source."""
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import joinedload

from app.core.time_contract import utc_naive_to_api, beijing_naive_to_api
from app.models.warehouse_inventory import (
    InventoryLot, InventoryLotTransfer, InventoryMovement, FinishedGoodsInventoryDetail,
)
from app.services.warehouse_display_units import lot_display_unit
from app.services.warehouse_location_address import employee_location_name

LABELS = {"stocktake": "盘点入库", "replenishment": "补库生产入库",
          "order": "订单生产入库", "other": "其他／来源待核"}
PRODUCTION_REFS = ("production_completion", "stock_preparation", "stock_preparation_finish",
                   "preparation_assembly", "bom_assembly", "stock_replenishment_receipt")


def production_lot_ids():
    """SQL ancestry keeps counts/pagination correct, including moved stock."""
    ancestry = select(InventoryLot.id.label("leaf"), InventoryLot.id.label("ancestor")).where(
        InventoryLot.inventory_type == "finished").cte("finished_production_ancestors", recursive=True)
    ancestry = ancestry.union(select(ancestry.c.leaf, InventoryLotTransfer.source_lot_id).join(
        InventoryLotTransfer, InventoryLotTransfer.target_lot_id == ancestry.c.ancestor))
    return select(ancestry.c.leaf).join(InventoryLot, InventoryLot.id == ancestry.c.ancestor).where(
        InventoryLot.inventory_type == "finished",
        InventoryLot.source_type.notin_(("stocktake", "manual", "delivery_return")),
        or_(InventoryLot.source_ref_type.in_(PRODUCTION_REFS),
            InventoryLot.source_type.in_(("production_completion", "production_surplus"))))


class Origins:
    def __init__(self, db):
        self.db = db
        self.cache = {}
        self.lineage = {}

    def roots(self, lot):
        if lot.id in self.cache:
            return self.cache[lot.id]
        seen, pending, roots, family = set(), [lot], [], []
        while pending:
            current = pending.pop()
            if current.id in seen:
                continue
            seen.add(current.id)
            family.append(current)
            parents = list(self.db.scalars(select(InventoryLot).join(InventoryLotTransfer,
                InventoryLotTransfer.source_lot_id == InventoryLot.id).where(
                InventoryLotTransfer.target_lot_id == current.id,
                InventoryLot.inventory_type == "finished")))
            if parents:
                pending.extend(parents)
            else:
                roots.append(current)
        self.cache[lot.id] = roots or [lot]
        self.lineage[lot.id] = family
        return self.cache[lot.id]

    def describe(self, lot):
        roots = self.roots(lot)
        keys = set()
        for root in roots:
            if root.source_type == "stocktake":
                keys.add("stocktake")
            elif root.source_type in ("manual", "delivery_return"):
                keys.add("other")
            elif root.source_ref_type in ("stock_preparation", "stock_preparation_finish",
                                           "preparation_assembly", "stock_replenishment_receipt"):
                keys.add("replenishment")
            elif root.source_ref_type in ("production_completion", "bom_assembly") or root.source_type in (
                    "production_completion", "production_surplus"):
                keys.add("order")
            else:
                keys.add("other")
        kind = next(iter(keys)) if len(keys) == 1 else "other"
        times = [r.created_at for r in roots if r.created_at]
        return {"source_kind": kind, "source_label": LABELS[kind],
                "entered_at": utc_naive_to_api(min(times)) if times else None,
                "stock_date": str(min(r.stock_date for r in roots if r.stock_date))
                              if any(r.stock_date for r in roots) else None}


def visible_lots(db, product, *, include_history=False):
    from app.services.product_workbench import _shared_finished_lots
    query = select(InventoryLot).join(FinishedGoodsInventoryDetail).options(
        joinedload(InventoryLot.finished_detail), joinedload(InventoryLot.location)).where(
        InventoryLot.inventory_type == "finished",
        FinishedGoodsInventoryDetail.product_id == product.id,
        FinishedGoodsInventoryDetail.owner_customer_id == product.customer_id,
        FinishedGoodsInventoryDetail.is_general.is_(False))
    if not include_history:
        query = query.where(InventoryLot.status.in_(("active", "frozen")),
            InventoryLot.quantity_available + InventoryLot.quantity_reserved + InventoryLot.quantity_damaged > 0)
    lots = {lot.id: lot for lot in db.scalars(query)}
    for lot in _shared_finished_lots(db, product):
        lots.setdefault(lot.id, lot)
    return list(lots.values())


def stock_row(lot, origins, scope):
    visible = scope is None or lot.finished_detail.owner_customer_id in scope
    live = lot.status in ("active", "frozen")
    physical = lot.quantity_available + lot.quantity_reserved + lot.quantity_damaged if live else 0
    return dict(lot_id=lot.id, lot_number=lot.lot_number if visible else None,
        **origins.describe(lot), actual=physical,
        available=lot.quantity_available if lot.status == "active" else 0,
        reserved=lot.quantity_reserved if live else 0,
        restricted=(lot.quantity_damaged + (lot.quantity_available if lot.status == "frozen" else 0)) if live else 0,
        consumed=lot.quantity_consumed if visible else None, status=lot.status, unit=lot_display_unit(lot),
        location_label=employee_location_name(lot.location), location_id=lot.warehouse_location_id,
        detail_allowed=visible, shared_confirmed=not visible)


def sources(db, product, *, scope, include_history=False, source_kind="all", page=1, page_size=20):
    origins = Origins(db)
    rows, shared = [], []
    for lot in visible_lots(db, product, include_history=include_history):
        row = stock_row(lot, origins, scope)
        (rows if lot.finished_detail.product_id == product.id and
         lot.finished_detail.owner_customer_id == product.customer_id else shared).append(row)
    totals = {key: {"actual": 0, "available": 0, "reserved": 0, "restricted": 0, "label": label}
              for key, label in LABELS.items()}
    for row in rows:
        for field in ("actual", "available", "reserved", "restricted"):
            totals[row["source_kind"]][field] += row[field]
    selected = [row for row in rows if source_kind == "all" or row["source_kind"] == source_kind]
    selected.sort(key=lambda r: (r["entered_at"] or "", r["lot_id"]), reverse=True)
    from app.services.product_unit_labels import product_unit_label
    return dict(items=selected[(page-1)*page_size:page*page_size], total=len(selected), page=page,
        page_size=page_size, has_more=page*page_size < len(selected), sources=totals,
        summary={field: sum(t[field] for t in totals.values()) for field in ("actual", "available", "reserved", "restricted")},
        shared_items=shared[:20], shared_total=len(shared),
        shared_available=sum(row["available"] for row in shared),
        unit=product_unit_label(product) or product.unit, read_only=True)


def batch_details(db, product, lot, *, permissions, scope):
    from app.models.production import ProductionCompletion
    from app.models.stock_preparation import StockPreparationJob
    from app.models.incoming_receipt import IncomingReceiptItem, IncomingReceipt
    from app.models.stock_replenishment import StockReplenishmentOrderItem, StockReplenishmentOrder
    from app.models.order import Order, OrderItem
    from app.models.supplier_requisition_order import SupplierRequisitionOrderItem, SupplierRequisitionOrder
    from app.models.requisition import RequisitionItem, Requisition
    from app.models.user import User
    origins = Origins(db)
    roots = origins.roots(lot)
    row = stock_row(lot, origins, scope)
    events, order_ids, receipt_ids, stock_ids = [], set(), set(), set()

    def add(key, label, time, *, quantity=None, unit=None, document=None, material=None,
            specification=None, location=None, note=None, wall_time=False):
        events.append(dict(key=key, label=label,
            at=(beijing_naive_to_api(time) if wall_time else utc_naive_to_api(time)) if time else None,
            quantity=quantity, unit=unit, document=document, material=material,
            specification=specification, location=location, note=note))

    # Only same-owner ancestors can contribute private order/material facts.
    roots = [r for r in roots if r.finished_detail and
             r.finished_detail.owner_customer_id == lot.finished_detail.owner_customer_id]
    root_ids = {r.id for r in origins.lineage[lot.id] if r.finished_detail and
                r.finished_detail.owner_customer_id == lot.finished_detail.owner_customer_id} | {lot.id}
    completions = list(db.scalars(select(ProductionCompletion).where(or_(
        ProductionCompletion.inventory_lot_id.in_(root_ids),
        ProductionCompletion.id.in_([r.source_ref_id for r in roots if r.source_ref_type == "production_completion"])))))
    order_ids.update(c.order_item_id for c in completions if c.order_item_id)
    jobs = list(db.scalars(select(StockPreparationJob).where(or_(
        StockPreparationJob.output_lot_id.in_(root_ids),
        StockPreparationJob.id.in_([r.source_ref_id for r in roots if r.source_ref_type == "stock_preparation"])))))
    finish_inputs = [r.source_ref_id for r in roots if r.source_ref_type == "stock_preparation_finish"]
    if finish_inputs:
        jobs.extend(db.scalars(select(StockPreparationJob).where(StockPreparationJob.output_lot_id.in_(finish_inputs))))
    from app.models.multilevel_bom import BomAssembly
    order_ids.update(db.scalars(select(BomAssembly.order_item_id).where(BomAssembly.output_lot_id.in_(root_ids))))
    receipt_ids.update(j.receipt_item_id for j in jobs)
    receipt_ids.update(r.source_ref_id for r in roots if r.source_ref_type == "stock_replenishment_receipt")
    movements = list(db.scalars(select(InventoryMovement).where(
        InventoryMovement.inventory_lot_id.in_(root_ids)).order_by(InventoryMovement.id)))
    order_ids.update(m.related_order_item_id for m in movements if m.related_order_item_id)
    receipts = list(db.scalars(select(IncomingReceiptItem).where(or_(
        IncomingReceiptItem.id.in_(receipt_ids),
        IncomingReceiptItem.received_inventory_lot_id.in_(root_ids),
        IncomingReceiptItem.order_item_id.in_(order_ids)))))
    stock_ids.update(r.stock_replenishment_item_id for r in receipts if r.stock_replenishment_item_id)
    order_ids.update(r.order_item_id for r in receipts if r.order_item_id)
    orders = list(db.execute(select(OrderItem, Order).join(Order).where(OrderItem.id.in_(order_ids))))
    orders = [(i, o) for i, o in orders if scope is None or o.customer_id in scope]
    allowed_order_ids = {i.id for i, _ in orders}
    if permissions["orders"]:
        for item, order in orders:
            add(f"order:{item.id}", "订单录入", order.created_at, quantity=item.quantity,
                unit=item.sales_unit_snapshot or row["unit"], document=order.order_number,
                note=f"客户单号：{order.customer_po or '未填写'}；订单状态：{order.status}",
                material=item.snapshot_material, specification=item.snapshot_spec)
    if permissions["requisition"]:
        for item, head in db.execute(select(SupplierRequisitionOrderItem, SupplierRequisitionOrder).join(
                SupplierRequisitionOrder).where(SupplierRequisitionOrderItem.order_item_id.in_(allowed_order_ids))):
            add(f"supplier:{item.id}", "订单报料采购", head.created_at,
                quantity=item.requisition_qty, unit="张", document=head.order_number,
                material=item.material_code_snapshot,
                specification=f"{item.report_length_mm}×{item.report_width_mm} mm", note=f"状态：{item.status}")
        for item, head in db.execute(select(RequisitionItem, Requisition).join(Requisition).where(
                RequisitionItem.order_item_id.in_(allowed_order_ids))):
            add(f"requisition:{item.id}", "订单报料", item.created_at, quantity=item.requisition_qty,
                unit="张", document=head.requisition_number, material=item.material_snapshot,
                specification=f"{item.cardboard_len}×{item.cardboard_width} mm", note=f"状态：{item.status}")
        for item, head in db.execute(select(StockReplenishmentOrderItem, StockReplenishmentOrder).join(
                StockReplenishmentOrder).where(StockReplenishmentOrderItem.id.in_(stock_ids))):
            if scope is not None and item.customer_id not in scope:
                continue
            add(f"stock:{item.id}", "库存补库报料", item.created_at, quantity=item.quantity, unit="张",
                document=head.order_number, material=item.material_code_snapshot,
                specification=f"{item.report_length_mm}×{item.report_width_mm} mm", note=f"状态：{head.status}")
    if permissions["incoming"]:
        for receipt in receipts:
            if receipt.order_item_id and receipt.order_item_id not in allowed_order_ids:
                continue
            material_lot = db.get(InventoryLot, receipt.received_inventory_lot_id) if receipt.received_inventory_lot_id else None
            raw = material_lot.semi_finished_detail if material_lot else None
            head = db.get(IncomingReceipt, receipt.receipt_id)
            add(f"receipt:{receipt.id}", "实收材料", head.received_at if head else receipt.created_at,
                quantity=receipt.received_quantity, unit="张", document=head.receipt_number if head else None,
                material=raw.material_code_snapshot if raw else None,
                specification=f"{raw.board_length_mm}×{raw.board_width_mm} mm" if raw else None,
                note=f"状态：{receipt.status}；实际材质规格以原收料批次为准")
    for root in roots:
        initial = next((m for m in movements if m.inventory_lot_id == root.id and
                        m.movement_type in ("manual_in", "production_in", "receive", "stock_in")), None)
        if initial is None:
            initial = next((m for m in movements if m.inventory_lot_id == root.id and
                            m.after_available + m.after_reserved > m.before_available + m.before_reserved), None)
        if row["source_kind"] == "stocktake" or permissions["production"] or row["source_kind"] == "other":
            operator = db.get(User, root.created_by) if root.created_by else None
            add(f"entry:{root.id}", origins.describe(root)["source_label"], root.created_at,
                quantity=initial.quantity if initial else None, unit=lot_display_unit(root), document=root.lot_number,
                material=root.finished_detail.material_code_snapshot,
                location=employee_location_name(root.location),
                note=f"登记账号：{(operator.display_name or operator.real_name or operator.username) if operator else '未记录'}；{root.remarks or ''}")
    movement_labels = {"reserve": "订单占用库存", "release_reserve": "解除库存占用",
        "consume": "成品出库", "transfer_out": "移出原位置", "transfer_in": "移入新位置",
        "location_transfer": "库存移位", "adjust": "盘点调整",
        "return_in": "退货入库", "reverse_consume": "撤销出库",
        "adjust_in": "盘点增加", "adjust_out": "盘点减少"}
    for movement in movements:
        if movement.movement_type not in movement_labels:
            continue
        if movement.related_order_item_id and not permissions["orders"]:
            continue
        if movement.related_order_item_id and movement.related_order_item_id not in allowed_order_ids:
            continue
        if movement.related_delivery_id and not permissions["deliveries"]:
            continue
        add(f"movement:{movement.id}", movement_labels[movement.movement_type], movement.created_at,
            quantity=movement.quantity, unit=row["unit"], document=movement.movement_number,
            note=f"{movement.reason or ''}；可用 {movement.before_available} → {movement.after_available}；占用 {movement.before_reserved} → {movement.after_reserved}")
    # Administrative dispositions are explicit facts, rather than guessing from
    # Order.updated_at (whose legacy timezone is not uniform).
    if permissions["orders"] and orders:
        from app.models.audit import OperationLog
        for audit in db.scalars(select(OperationLog).where(OperationLog.entity_type == "order",
                OperationLog.entity_id.in_([o.id for _, o in orders]), OperationLog.action == "order.admin_disposition")):
            add(f"audit:{audit.id}", audit.description or "订单处理", audit.created_at,
                note="关联订单的撤回／作废审计保留；原库存来源不改变")
    events.sort(key=lambda e: (e["at"] or "", e["key"]))
    return dict(batch=row, events=events,
        source_note="本批次为盘点登记成品，不要求存在报料、收料或生产完工。" if row["source_kind"] == "stocktake" else
                    "按原始单据和批次追溯；未记录的节点及规格显示为未记录。",
        hidden_sections=[key for key, allowed in permissions.items() if not allowed], read_only=True)
