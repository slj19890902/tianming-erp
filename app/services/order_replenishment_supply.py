"""FIFO coverage from explicit product replenishments, distinct from physical stock.

This is a read projection over the existing purchase/receipt/processing ledgers.
Every caller sees all active customer demand, even when rendering one order. A
purchase save recomputes it in its existing transaction; reads never reserve or
manufacture stock. Completed outputs use the existing physical reservation flow.
"""
from __future__ import annotations

import json
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

from sqlalchemy import select

from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.stock_replenishment import StockReplenishmentOrder, StockReplenishmentOrderItem

_reading = ContextVar("reading_replenishment_supply", default=False)
_projection = ContextVar("replenishment_supply_projection", default=None)


def reading_supply():
    return _reading.get()


def _number(value):
    if value is None or value == "":
        return None
    try:
        return format(Decimal(str(value)).normalize(), "f")
    except (InvalidOperation, ValueError, TypeError):
        return str(value).strip()


def _json(value):
    try:
        document = json.loads(value) if isinstance(value, str) else value
        return document if isinstance(document, dict) else {}
    except (ValueError, TypeError):
        return {}


def _signature(row):
    crease = str(row.get("crease_type") or "").strip()
    # Non-creased raw/net sheets have no active crease segments.
    segments = [row.get(k) for k in ("crease_left_mm", "crease_middle_mm", "crease_right_mm")] if crease == "压线" else [None] * 3
    return tuple(_number(v) for v in (
        row.get("customer_id"), row.get("product_id"), row.get("material_id"),
        str(row.get("material") or "").strip().upper(), row.get("supplier_name"),
        row.get("layer_count"), str(row.get("flute_type") or "").strip().upper(),
        row.get("report_length_mm"), row.get("report_width_mm"), crease, *segments,
        row.get("component_type"), row.get("pieces_per_box"), row.get("yield_per_sheet"),
    ))


def _unmeasured_die_contract(identity):
    """Prove a descriptive SKU using frozen tooling, never today's master."""
    fields = identity.get("fields") or {}
    from app.services.sheet_cutting_settings import component_settings
    from app.services.sheet_cutting_contract import SheetCuttingContractError
    if any(_number(fields.get(key)) not in (None, "0")
            for key in ("length_mm", "width_mm", "height_mm")):
        return None
    try:
        setting = component_settings(fields.get("sheet_cutting_settings"))
        if not setting or not setting.is_die_cut or not fields.get("mold_tool_id"):
            return None
        return dict(product_id=identity["product_id"], mold_tool_id=str(fields["mold_tool_id"]),
            product_code=str(fields.get("product_code") or "").strip(),
            mold_count=setting.mold_count, length_parts=setting.length_parts,
            width_parts=setting.width_parts)
    except (SheetCuttingContractError, KeyError, TypeError, ValueError):
        return None


def _supply_basis_matches(source, demand):
    from app.services.finished_stock_identity import matches_stock_identity
    if matches_stock_identity(source["basis"], demand["basis"]):
        return True
    contract = source.get("unmeasured_die_contract")
    if not contract or contract != demand.get("unmeasured_die_contract"):
        return False
    # A generated warehouse caption is not a measured finished dimension.
    # The independently frozen mold, imposition and every other physical fact
    # must still agree. Genuine/custom specifications cannot enter this path.
    import re
    from app.services.product_specification import strip_unregistered_size_note
    documents = [_json(row["basis"]).copy() for row in (source, demand)]
    for doc in documents:
        if (doc.get("product_id") != contract["product_id"]
                or str(doc.get("mold_tool_id")) != contract["mold_tool_id"]
                or doc.get("assembly")):
            return False
        spec = strip_unregistered_size_note(str(doc.get("spec") or "")).strip()
        pattern = rf"模切成品 {re.escape(contract['product_code'])}；模具 .+；{contract['mold_count']}模"
        if spec and not re.fullmatch(pattern, spec):
            return False
        doc["spec"] = None
    return matches_stock_identity(*(json.dumps(doc, sort_keys=True) for doc in documents))


def _ordinary_row(db, item, order, component, requirement):
    from app.api.requisition import _component_crease, _resolved_sheet_cutting
    cutting = _resolved_sheet_cutting(item, component)
    base = component == "base"
    length = item.snapshot_base_report_length_mm if base else item.snapshot_report_length_mm
    width = item.snapshot_base_report_width_mm if base else item.snapshot_report_width_mm
    if cutting:
        length, width = cutting.supplier_size_mm
    crease = _component_crease(item, component)
    return dict(requirement, order_item_id=item.id, snapshot_id=None,
        product_id=item.product_id, customer_id=order.customer_id,
        material_id=item.material_id, material=item.snapshot_material,
        supplier_name=item.snapshot_supplier_name, layer_count=item.layer_count,
        flute_type=item.flute_type, report_length_mm=length, report_width_mm=width,
        crease_type=crease[0], crease_left_mm=crease[1], crease_middle_mm=crease[2],
        crease_right_mm=crease[3], yield_per_sheet=requirement["cutting_factor"],
        pieces_per_box=requirement["pieces_per_box"], sheet_cutting_snapshot=cutting.to_snapshot() if cutting else None)


def _ordinary_basis(db, item, row):
    # Reuse the append-only master history contract used by old replenishments.
    # No current-master fallback is allowed after a subsequent master edit.
    from app.services.stock_purchase_identity import resolve
    from app.services.warehouse_inventory import WarehouseInventoryError
    surrogate = SimpleNamespace(reference_product_id=item.product_id, product_id=item.product_id,
        production_snapshot_json=None, created_at=item.created_at,
        customer_id=row["customer_id"], material_code_snapshot=row["material"],
        layer_count=row["layer_count"], flute_type=row["flute_type"],
        report_length_mm=row["report_length_mm"], report_width_mm=row["report_width_mm"],
        crease_type=row["crease_type"], crease_left_mm=row["crease_left_mm"],
        crease_middle_mm=row["crease_middle_mm"], crease_right_mm=row["crease_right_mm"],
        component_type=row["component_type"], pieces_per_box=row["pieces_per_box"],
        stock_yield_per_sheet=row["yield_per_sheet"])
    try:
        identity = resolve(db, surrogate)
        basis = _json(identity.get("physical_basis"))
        # The order's own frozen spec/material must also agree with that history.
        from app.services.product_specification import normalized_specification_text
        spec = normalized_specification_text(item.snapshot_spec)
        contract = _unmeasured_die_contract(identity)
        fields = identity.get("fields") or {}
        aliases = {None, normalized_specification_text(fields.get("product_name")),
            normalized_specification_text(fields.get("product_code"))}
        descriptive_order = contract and spec in aliases and not basis.get("assembly")
        if ((basis.get("spec") != spec and not descriptive_order)
                or str(basis.get("material") or "").strip().upper() != str(item.snapshot_material or "").strip().upper()
                or str(basis.get("flute") or "").strip().upper() != str(item.flute_type or "").strip().upper()):
            return None
        if descriptive_order:
            row["unmeasured_die_contract"] = contract
        return identity["physical_basis"]
    except (WarehouseInventoryError, ValueError, TypeError, KeyError, AttributeError):
        return None


def _demands(db, supplies):
    from app.api import requisition as req
    from fastapi import HTTPException
    from app.services.multilevel_bom_plan import BomPlanError
    from app.services.composite_bom_execution import CompositeBOMExecutionError
    from app.models.product_bom import SalesOrderItemBomComponent
    from app.services.finished_stock_identity import order_product_basis, snapshot_basis
    from app.services.raw_purchase_plans import active_order_items
    from sqlalchemy import or_
    product_ids = {row["product_id"] for row in supplies}
    customer_ids = {row["customer_id"] for row in supplies}
    oldest = min((row["created_at"] for row in supplies if row["created_at"]), default=None)
    candidate = select(SalesOrderItemBomComponent.sales_order_item_id).where(
        SalesOrderItemBomComponent.component_product_id.in_(product_ids))
    rows = db.execute(select(OrderItem, Order).join(Order, Order.id == OrderItem.order_id)
        .where(Order.status.in_(req.ORDER_ITEM_ACTIVE_ORDER_STATUSES),
            Order.customer_id.in_(customer_ids), or_(OrderItem.product_id.in_(product_ids), OrderItem.id.in_(candidate)),
            Order.created_at >= oldest if oldest else True,
            OrderItem.is_force_closed.is_(False), OrderItem.delivered_quantity < OrderItem.quantity,
            OrderItem.material_status == "pending", OrderItem.supply_mode_snapshot != "external_purchase",
            OrderItem.requisition_status.in_(("未报料", "已报料")),
            ~OrderItem.id.in_(active_order_items()))
        .order_by(Order.created_at, Order.id, OrderItem.item_sequence, OrderItem.id)).all()
    result = []
    for item, order in rows:
        try:
            snapshots = req._bom_snapshots_for_order_item(db, item.id)
            manufactured = [s for s in snapshots if s.component_product_id in product_ids and req._bom_snapshot_inventory_source(db, s) == "manufactured"]
            demands = []
            for snapshot in manufactured:
                for component in req._bom_snapshot_component_types(snapshot):
                    row = req._bom_snapshot_requirements(db, snapshot, component_type=component)
                    row.update(order_item_id=item.id, customer_id=order.customer_id,
                        pieces_per_box=row["physical_pieces_per_component"])
                    row["already_purchased_sheets"] = req._bom_snapshot_active_order_purpose_sheet_qty(db, snapshot.id, component_type=component)
                    row["basis"] = order_product_basis(db, item.id, snapshot.component_product_id)
                    if row["basis"] is None:
                        product = db.get(Product, snapshot.component_product_id)
                        row["basis"] = snapshot_basis(snapshot, product.unit) if product else None
                    demands.append(row)
            if item.product_id in product_ids and not req._composite_parent_requisition_is_suppressed(item, snapshots):
                summary = req._current_requisition_summary(db, item)
                for requirement in summary["component_requirements"]:
                    component = requirement["component_type"]
                    row = _ordinary_row(db, item, order, component, requirement)
                    row["already_purchased_sheets"] = req._active_supplier_requisition_facts(db, item=item, component_type=component)["quantity"]
                    row["basis"] = order_product_basis(db, item.id, item.product_id) or _ordinary_basis(db, item, row)
                    demands.append(row)
            for row in demands:
                quantity = max(int(row["remaining_required_piece_qty"]) - int(row["already_purchased_sheets"]) * int(row["yield_per_sheet"]), 0)
                if not quantity or not row["basis"]:
                    continue
                row.update(item=item, order=order, quantity=quantity,
                    parent_basis=order_product_basis(db, item.id, item.product_id),
                    key=(item.id, row.get("snapshot_id"), row["component_type"]))
                result.append(row)
        except (HTTPException, BomPlanError, CompositeBOMExecutionError):
            # A broken historical demand has no provable compatible supply.
            # Its own operation retains the original business error.
            continue
    return result


def _target_scope(db, order_item_ids=None, source_item_ids=None):
    from app.models.product_bom import SalesOrderItemBomComponent
    product_ids, customer_ids = set(), set()
    if order_item_ids:
        rows = db.execute(select(OrderItem.product_id, Order.customer_id).join(Order, Order.id == OrderItem.order_id)
            .where(OrderItem.id.in_(order_item_ids))).all()
        product_ids.update(pid for pid, _ in rows)
        customer_ids.update(cid for _, cid in rows)
        product_ids.update(db.scalars(select(SalesOrderItemBomComponent.component_product_id)
            .where(SalesOrderItemBomComponent.sales_order_item_id.in_(order_item_ids))))
    if source_item_ids:
        rows = db.execute(select(StockReplenishmentOrderItem.reference_product_id, StockReplenishmentOrderItem.customer_id)
            .where(StockReplenishmentOrderItem.id.in_(source_item_ids))).all()
        product_ids.update(pid for pid, _ in rows if pid is not None)
        customer_ids.update(cid for _, cid in rows if cid is not None)
    return product_ids, customer_ids


def _supplies(db, *, order_item_ids=None, source_item_ids=None):
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.stock_preparation import StockPreparationJob
    from app.models.raw_purchase_plan import RawPurchasePlan
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation
    from app.services.replenishment_receipt_progress import short_closed_clause
    from app.services.stock_purchase_identity import resolve
    from app.services.warehouse_inventory import WarehouseInventoryError
    from sqlalchemy import or_, and_
    from sqlalchemy.orm import aliased
    raw = aliased(InventoryLot)
    out = aliased(InventoryLot)
    receipt = IncomingReceiptItem
    jobs = StockPreparationJob
    raw_available = select(receipt.id).join(raw, and_(raw.source_ref_type == "stock_replenishment_receipt", raw.source_ref_id == receipt.id))
    raw_available = raw_available.where(receipt.stock_replenishment_item_id == StockReplenishmentOrderItem.id,
        receipt.status == "posted", raw.status == "active", raw.quantity_available > 0).exists()
    pending_job = select(jobs.id).join(receipt, receipt.id == jobs.receipt_item_id).where(
        receipt.stock_replenishment_item_id == StockReplenishmentOrderItem.id, receipt.status == "posted", jobs.status == "pending").exists()
    output_available = select(jobs.id).join(receipt, receipt.id == jobs.receipt_item_id).join(out,
        and_(out.source_ref_type == "stock_preparation", out.source_ref_id == jobs.id)).where(
        receipt.stock_replenishment_item_id == StockReplenishmentOrderItem.id, receipt.status == "posted",
        jobs.status == "completed", out.status == "active", out.quantity_available > 0).exists()
    item_query = select(StockReplenishmentOrderItem).join(StockReplenishmentOrder,
        StockReplenishmentOrder.id == StockReplenishmentOrderItem.replenishment_order_id)
    item_query = item_query.where(StockReplenishmentOrder.source_type.in_(("stock_warning", "customer_request")),
            StockReplenishmentOrder.status != "voided", StockReplenishmentOrder.request_hash.is_not(None),
            StockReplenishmentOrderItem.procurement_route_snapshot == "paperboard",
            StockReplenishmentOrderItem.customer_id.is_not(None),
            StockReplenishmentOrderItem.reference_product_id.is_not(None),
            ~StockReplenishmentOrderItem.id.in_(select(RawPurchasePlan.stock_item_id)),
            or_(and_(StockReplenishmentOrder.status.in_(("draft", "confirmed", "partially_stocked")),
                StockReplenishmentOrderItem.quantity > StockReplenishmentOrderItem.stocked_quantity,
                ~short_closed_clause(StockReplenishmentOrderItem.id)), raw_available, pending_job, output_available))
    if order_item_ids is not None or source_item_ids is not None:
        product_ids, customer_ids = _target_scope(db, order_item_ids, source_item_ids)
        if not product_ids or not customer_ids:
            return []
        item_query = item_query.where(StockReplenishmentOrderItem.reference_product_id.in_(product_ids),
            StockReplenishmentOrderItem.customer_id.in_(customer_ids))
    items = db.scalars(item_query.order_by(StockReplenishmentOrder.created_at, StockReplenishmentOrderItem.id)).all()
    result = []
    from app.services.finished_stock_identity import matches_lot_stock_identity
    from app.services.processed_component_stock import eligible_output

    def outputs(source, original, job=None):
        family = [original]
        if original.source_ref_type and original.source_ref_id:
            family = db.scalars(select(InventoryLot).where(
                InventoryLot.source_ref_type == original.source_ref_type,
                InventoryLot.source_ref_id == original.source_ref_id,
                InventoryLot.inventory_type == original.inventory_type,
                InventoryLot.status == "active", InventoryLot.quantity_available > 0)
                .order_by(InventoryLot.id)).all()
        for output in family:
            if output.status != "active" or not output.quantity_available:
                continue
            if output.finished_detail:
                detail = output.finished_detail
                from app.services.fixed_shelf_staging import staging_owner
                from app.services.bom_subkits import active_subkit_order
                safe = (not detail.is_general and detail.owner_customer_id == source["customer_id"]
                    and detail.product_id == source["product_id"]
                    and matches_lot_stock_identity(output, source["basis"])
                    and staging_owner(db, output.id) is None and active_subkit_order(db, output) is None)
            else:
                snapshot = _json(job.product_snapshot) if job else {}
                from app.services.finished_stock_identity import matches_stock_identity
                safe = bool(job and job.status == "completed" and job.actual_output > 0
                    and snapshot.get("product_id") == source["product_id"]
                    and snapshot.get("pieces_per_box") == source["pieces_per_box"]
                    and snapshot.get("factor") == source["yield_per_sheet"]
                    and job.expected_output == job.input_quantity * source["yield_per_sheet"] // source["pieces_per_box"]
                    and matches_stock_identity(snapshot.get("physical_basis"), source["basis"])
                    and sum(l.quantity_available + l.quantity_reserved for l in family) <= job.actual_output
                    and eligible_output(db, output, product_id=source["product_id"],
                        customer_id=source["customer_id"], expected_basis=source["basis"]))
            if safe:
                # A completed job records products, not uncut supplier sheets.
                # Its frozen formula proves PPB; no paper yield is reapplied.
                stock_pieces_per_unit = source["pieces_per_box"]
                yield dict(source, stage="finished", quantity=output.quantity_available * stock_pieces_per_unit,
                    lot_id=output.id, stock_pieces_per_unit=stock_pieces_per_unit)
    for item in items:
        try:
            identity = resolve(db, item)
        except (WarehouseInventoryError, ValueError, TypeError, AttributeError):
            continue
        if not identity.get("physical_basis"):
            continue
        parent_contract = _json(item.quantity_contract_json)
        child = next((c for c in parent_contract.get("components", []) if c.get("product_id") == item.reference_product_id), {})
        supplier = ((child.get("defaults") or {}).get("material_supplier_name")
            if parent_contract.get("parent_product_id") else item.order.supplier_name)
        if not supplier:
            # A BOM component's missing frozen supplier must not silently
            # become its parent's supplier, especially after a master edit.
            continue
        row = dict(source=item, source_id=item.id, supplier_name=supplier,
            product_id=item.reference_product_id, customer_id=item.customer_id, material_id=item.material_id,
            material=item.material_code_snapshot, layer_count=item.layer_count, flute_type=item.flute_type,
            report_length_mm=item.report_length_mm, report_width_mm=item.report_width_mm,
            crease_type=item.crease_type, crease_left_mm=item.crease_left_mm,
            crease_middle_mm=item.crease_middle_mm, crease_right_mm=item.crease_right_mm,
            component_type=item.component_type, pieces_per_box=item.pieces_per_box,
            yield_per_sheet=item.stock_yield_per_sheet, basis=identity["physical_basis"],
            parent_product_id=parent_contract.get("parent_product_id"), parent_basis=identity.get("bom_parent_basis"),
            created_at=item.order.created_at, unmeasured_die_contract=_unmeasured_die_contract(identity))
        closed = bool(db.scalar(select(short_closed_clause(item.id))))
        from app.models.procurement_source import ProcurementSourceLink
        from app.models.supplier_requisition_order import SupplierRequisitionOrder, SupplierRequisitionOrderItem
        purchased = bool(db.scalar(select(ProcurementSourceLink.id).join(SupplierRequisitionOrderItem,
            SupplierRequisitionOrderItem.id == ProcurementSourceLink.supplier_item_id)
            .join(SupplierRequisitionOrder, SupplierRequisitionOrder.id == SupplierRequisitionOrderItem.supplier_order_id)
            .where(ProcurementSourceLink.stock_replenishment_item_id == item.id,
                ProcurementSourceLink.status == "active", SupplierRequisitionOrderItem.status == "active",
                SupplierRequisitionOrder.status == "confirmed").limit(1)))
        outstanding = 0 if closed or item.order.status not in {"draft", "confirmed", "partially_stocked"} else max(item.quantity - item.stocked_quantity, 0)
        if outstanding:
            stage = "pending_incoming" if purchased else "pending_material"
            multiplier = item.pieces_per_box if item.target_inventory_type == "finished" else item.stock_yield_per_sheet
            pieces = outstanding * multiplier
            pieces -= pieces % max(item.pieces_per_box, 1)
            if pieces:
                result.append(dict(row, stage=stage, quantity=pieces, lot_id=None))
        receipts = db.scalars(select(IncomingReceiptItem).where(IncomingReceiptItem.stock_replenishment_item_id == item.id,
            IncomingReceiptItem.status == "posted").order_by(IncomingReceiptItem.id)).all()
        if not purchased:
            continue
        for receipt in receipts:
            lot = db.get(InventoryLot, receipt.received_inventory_lot_id) if receipt.received_inventory_lot_id else None
            if not lot:
                continue
            if lot.inventory_type == "finished":
                result.extend(outputs(row, lot))
                continue
            if lot.status == "active" and lot.semi_finished_detail and lot.quantity_available:
                # The receipt's own raw quantity is counted once. Its processing
                # reservations are excluded here and counted through jobs below.
                pieces = lot.quantity_available * item.stock_yield_per_sheet
                pieces -= pieces % max(item.pieces_per_box, 1)
                if pieces:
                    result.append(dict(row, stage="pending_processing", quantity=pieces, lot_id=None))
            for job in db.scalars(select(StockPreparationJob).where(StockPreparationJob.receipt_item_id == receipt.id)):
                reservation = db.get(InventoryReservation, job.reservation_id)
                if job.status == "pending" and reservation and reservation.status == "active":
                    result.append(dict(row, stage="pending_processing", quantity=job.expected_output * item.pieces_per_box, lot_id=None))
                elif job.status == "completed" and job.output_lot_id:
                    output = db.get(InventoryLot, job.output_lot_id)
                    if output:
                        # Actual production replaces its theoretical quantity.
                        result.extend(outputs(row, output, job))
    return result


def allocations(db, *, order_item_ids=None, source_item_ids=None):
    if db is None or reading_supply():
        return {}
    cached = _projection.get()
    if cached is not None and cached[0] is db:
        return cached[1]
    token = _reading.set(True)
    try:
        supplies = _supplies(db, order_item_ids=order_item_ids, source_item_ids=source_item_ids)
        if not supplies:
            return {}
        from app.services.finished_stock_identity import matches_stock_identity
        supplies.sort(key=lambda row: ({"finished": 0, "pending_processing": 1, "pending_incoming": 2, "pending_material": 3}[row["stage"]],
            row["created_at"], row["source_id"], row["lot_id"] or 0))
        result = defaultdict(list)
        for demand in _demands(db, supplies):
            remaining = demand["quantity"]
            for source in supplies:
                if (not remaining or not source["quantity"] or _signature(source) != _signature(demand)
                        or not _supply_basis_matches(source, demand)
                        or (source["created_at"] and demand["order"].created_at and source["created_at"] > demand["order"].created_at)
                        or (source["parent_product_id"] is not None and (source["parent_product_id"] != demand["item"].product_id
                            or not demand["parent_basis"] or not matches_stock_identity(source["parent_basis"], demand["parent_basis"])))):
                    continue
                take = min(remaining, source["quantity"])
                # Physical outputs use whole product units; a fractional last
                # product cannot become a sendable reservation.
                take -= take % max(int(source["pieces_per_box"]), 1)
                if not take:
                    continue
                source["quantity"] -= take
                remaining -= take
                result[demand["key"]].append(dict(source_id=source["source_id"],
                    order_number=source["source"].order.order_number, stage=source["stage"],
                    covered_piece_quantity=take, lot_id=source["lot_id"],
                    product_id=demand["product_id"], pieces_per_box=demand["pieces_per_box"],
                    stock_pieces_per_unit=source.get("stock_pieces_per_unit", 1),
                    source_stock_quantity=(take // source["stock_pieces_per_unit"] if source.get("stock_pieces_per_unit") else None),
                    source_stock_unit=_json(source["basis"]).get("unit"),
                    product_code=source["source"].product_code_snapshot,
                    product_name=source["source"].product_name_snapshot))
        return dict(result)
    finally:
        _reading.reset(token)


@contextmanager
def projected(db):
    """Batch reads share one snapshot; never use this across a mutation."""
    value = allocations(db)
    token = _projection.set((db, value))
    try:
        yield value
    finally:
        _projection.reset(token)


def coverage(db, order_item_id, snapshot_id=None, component="whole", *, projection=None):
    rows = (projection if projection is not None else allocations(db, order_item_ids=[order_item_id])).get((order_item_id, snapshot_id, component), [])
    return dict(replenishment_covered_piece_qty=sum(r["covered_piece_quantity"] for r in rows),
        replenishment_sources=rows)


def summary(db, order_item_id, *, projection=None):
    value = projection if projection is not None else allocations(db, order_item_ids=[order_item_id])
    rows = [dict(row, snapshot_id=key[1], component_type=key[2])
        for key, group in value.items() if key[0] == order_item_id for row in group]
    result = dict(covered_piece_quantity=sum(r["covered_piece_quantity"] for r in rows), sources=rows,
        pending_material_piece_quantity=sum(r["covered_piece_quantity"] for r in rows if r["stage"] == "pending_material"),
        incoming_piece_quantity=sum(r["covered_piece_quantity"] for r in rows if r["stage"] == "pending_incoming"),
        processing_piece_quantity=sum(r["covered_piece_quantity"] for r in rows if r["stage"] == "pending_processing"),
        physical_output_piece_quantity=sum(r["covered_piece_quantity"] for r in rows if r["stage"] == "finished"))
    result.update(covered_finished_quantity=0, remaining_purchase_finished_quantity=None,
        remaining_purchase_sheet_quantity=None, total_coverable_finished_quantity=None, unit_label="只")
    if not rows or db is None:
        return result
    from app.api import requisition as req
    from app.services.product_unit_labels import order_unit_label
    from app.services.multilevel_bom_requirements import read_graph_requirements
    from app.services.multilevel_bom_plan import plan_bom
    item = db.get(OrderItem, order_item_id)
    result["unit_label"] = order_unit_label(item, db.get(Product, item.product_id))
    token = _projection.set((db, value))
    try:
        snapshots = req._bom_snapshots_for_order_item(db, item.id)
        components = req._bom_pending_component_requirements(db, item, snapshots=snapshots)
        graph = read_graph_requirements(db, item.id) if snapshots else None
        multipliers = {p.product_id: p.required_units for p in plan_bom(graph.compiled.graph, 1).products} if graph else {}
        frozen_by_id = {s.id: s for s in snapshots}
        capacities, before_capacities, shortages = [], [], []
        remaining_sheets = 0
        for row in components:
            multiplier = multipliers.get(row["product_id"], int(frozen_by_id[row["snapshot_id"]].quantity_per_set))
            per_finished = max(multiplier * int(row["physical_pieces_per_component"]), 1)
            own = int(row["already_requisitioned"]) * int(row["yield_per_sheet"])
            before = int(row["inventory_covered_piece_qty"]) + int(row.get("carried_material_piece_qty", 0)) + own
            planned = int(row.get("replenishment_covered_piece_qty", 0))
            before_capacities.append(before // per_finished)
            capacities.append((before + planned) // per_finished)
            missing = max(int(row["remaining_required_piece_qty"]) - own, 0)
            shortages.append((missing + per_finished - 1) // per_finished)
            remaining_sheets += int(row["requisition_qty"])
        if not req._composite_parent_requisition_is_suppressed(item, snapshots):
            ordinary = req._remaining_supplier_requisition_summary(db, item)
            for row in ordinary["component_requirements"]:
                per_finished = max(int(row["pieces_per_box"]), 1)
                own = int(row["already_requisitioned_qty"]) * int(row["cutting_factor"])
                before = int(row["semi_finished_reserved_piece_qty"]) + own
                before_capacities.append(before // per_finished)
                capacities.append((before + int(row.get("replenishment_covered_piece_qty", 0))) // per_finished)
                missing = max(int(row["remaining_required_piece_qty"]) - own, 0)
                shortages.append((missing + per_finished - 1) // per_finished)
                remaining_sheets += int(row["requisition_qty"])
        if graph:
            # A paper route does not claim that an unprovided purchased BOM
            # component is present. The dedicated external flow remains intact.
            sources = {n.product_id: n.source for n in graph.compiled.graph.nodes}
            for product in graph.plan.products:
                if sources[product.product_id] == "purchased" and product.make_units:
                    per_finished = max(multipliers[product.product_id], 1)
                    shortages.append((product.make_units + per_finished - 1) // per_finished)
                    capacities.append(0)
                    before_capacities.append(0)
        result["covered_finished_quantity"] = min(int(item.quantity), max(min(capacities, default=0) - min(before_capacities, default=0), 0))
        result["remaining_purchase_finished_quantity"] = min(int(item.quantity), max(shortages, default=0))
        result["total_coverable_finished_quantity"] = max(int(item.quantity) - result["remaining_purchase_finished_quantity"], 0)
        result["remaining_purchase_sheet_quantity"] = remaining_sheets
    finally:
        _projection.reset(token)
    return result


def lock_supply(db, *, order_item_ids=None, source_item_ids=None):
    """Claim source headers before a mutation reads the global FIFO projection.

    SQLite obtains its real writer lock here; PostgreSQL takes ordered row
    locks through UPDATE. Business fields and historical snapshots are unchanged.
    """
    from sqlalchemy import update
    candidates = _supplies(db, order_item_ids=order_item_ids, source_item_ids=source_item_ids)
    ids = sorted({row["source"].replenishment_order_id for row in candidates})
    for source_id in ids:
        db.execute(update(StockReplenishmentOrder).where(StockReplenishmentOrder.id == source_id)
            .values(status=StockReplenishmentOrder.status).execution_options(synchronize_session=False))
    if ids:
        db.flush()
        for row in candidates:
            db.expire(row["source"])
        for source_id in ids:
            source = db.get(StockReplenishmentOrder, source_id)
            if source is not None:
                db.expire(source)


def authorize_output(db, *, source_id, order_item_id, snapshot_id, lot_id, quantity):
    """Internal grant for actual output; this is never a request payload flag."""
    from app.services.warehouse_inventory import WarehouseInventoryError
    rows = coverage(db, order_item_id, snapshot_id)["replenishment_sources"]
    allowed = sum(r["covered_piece_quantity"] // max(r.get("stock_pieces_per_unit", 1), 1)
        for r in rows if r["source_id"] == source_id and r["stage"] == "finished" and r["lot_id"] == lot_id)
    if type(quantity) is not int or quantity <= 0 or quantity > allowed:
        raise WarehouseInventoryError("实际补库产出与本单FIFO供应分配不一致，请刷新核对", 409)


def source_ids_for_order_reservations(db, order_item_ids):
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation
    from app.models.stock_preparation import StockPreparationJob
    from app.models.incoming_receipt import IncomingReceiptItem
    sources = set()
    lots = db.scalars(select(InventoryLot).join(InventoryReservation, InventoryReservation.inventory_lot_id == InventoryLot.id)
        .where(InventoryReservation.order_item_id.in_(order_item_ids)).distinct()).all()
    for lot in lots:
        if lot.source_ref_type == "stock_preparation":
            job = db.get(StockPreparationJob, lot.source_ref_id)
            receipt = db.get(IncomingReceiptItem, job.receipt_item_id) if job else None
        elif lot.source_ref_type == "stock_replenishment_receipt":
            receipt = db.get(IncomingReceiptItem, lot.source_ref_id)
        else:
            receipt = None
        if receipt and receipt.stock_replenishment_item_id:
            sources.add(receipt.stock_replenishment_item_id)
    return sorted(sources)


def output_free_for_order(db, *, lot, order_item_id):
    """Do not let generic BOM allocation take another order's FIFO supply.

    This read claims no inventory. Unassigned physical stock and stock outside
    this explicit replenishment contract retain their existing behavior.
    """
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.stock_preparation import StockPreparationJob
    receipt = None
    if lot.source_ref_type == "stock_preparation":
        job = db.get(StockPreparationJob, lot.source_ref_id)
        receipt = db.get(IncomingReceiptItem, job.receipt_item_id) if job else None
    elif lot.source_ref_type == "stock_replenishment_receipt":
        receipt = db.get(IncomingReceiptItem, lot.source_ref_id)
    if not receipt or not receipt.stock_replenishment_item_id:
        return lot.quantity_available
    value = allocations(db, source_item_ids=[receipt.stock_replenishment_item_id])
    held = sum(row["covered_piece_quantity"] // max(row.get("stock_pieces_per_unit", 1), 1)
        for key, rows in value.items() if key[0] != order_item_id
        for row in rows if row["lot_id"] == lot.id)
    return max(lot.quantity_available - held, 0)


def reconcile_outputs(db, *, operator_id, order_item_ids=None, source_item_ids=None):
    """Convert only proven physical outputs, within the caller's transaction."""
    from app.models.warehouse_inventory import InventoryLot
    from app.services.warehouse_inventory import reserve_finished_inventory, reserve_finished_inventory_for_bom_component
    from app.services.multilevel_bom_orders import read_compiled_order_bom
    from app.services.processed_component_stock import reserve_output
    from app.services.audit_log import append_audit_event
    from app.models.user import User
    if order_item_ids is None and source_item_ids is None:
        return []
    lock_supply(db, order_item_ids=order_item_ids, source_item_ids=source_item_ids)
    # Recompute after every reservation: its real credit removes demand and its
    # physical claim removes supply. No request-local cache crosses these writes.
    reserved = []
    skipped = set()
    while True:
        value = allocations(db, order_item_ids=order_item_ids, source_item_ids=source_item_ids)
        choice = next(((key, row) for key, rows in value.items() for row in rows
            if row["stage"] == "finished" and row["lot_id"] and (key, row["lot_id"]) not in skipped
            and (order_item_ids is None or key[0] in order_item_ids)
            and (source_item_ids is None or row["source_id"] in source_item_ids)), None)
        if choice is None:
            break
        (item_id, snapshot_id, component), row = choice
        item = db.get(OrderItem, item_id)
        from app.services.production_workflow import has_production_completion_facts
        if item.delivered_quantity > 0 or has_production_completion_facts(db, [item_id]):
            skipped.add(((item_id, snapshot_id, component), row["lot_id"]))
            continue
        if component != "whole":
            # Cover/base outputs require their own physical processing route.
            # The current explicit-product replenishment entry only emits whole.
            skipped.add(((item_id, snapshot_id, component), row["lot_id"]))
            continue
        quantity = row["covered_piece_quantity"] // max(row.get("stock_pieces_per_unit", 1), 1)
        lot = db.get(InventoryLot, row["lot_id"])
        key = f"replenishment-output:{row['source_id']}:{item_id}:{snapshot_id or 0}:{lot.id}:{lot.version}"
        if lot.finished_detail:
            args = dict(order_item_id=item_id, inventory_lot_id=lot.id, quantity=quantity,
                expected_version=lot.version, operator_id=operator_id, idempotency_key=key,
                warning_acknowledged_codes=[], replenishment_source_id=row["source_id"])
            reservation = (reserve_finished_inventory_for_bom_component(db, bom_snapshot_id=snapshot_id, **args)
                if snapshot_id else reserve_finished_inventory(db, **args))
        else:
            compiled = read_compiled_order_bom(db, item_id)
            assembly_children = {edge.child_id for edge in compiled.graph.edges if edge.relation == "assembly"} if compiled else set()
            if compiled is not None and snapshot_id is not None and row["product_id"] in assembly_children:
                reservation = reserve_output(db, compiled=compiled, order_item_id=item_id,
                    snapshot_id=snapshot_id, lot=lot, quantity=quantity, expected_version=lot.version,
                    operator_id=operator_id, key=key)
            else:
                reservation = reserve_material_output(db, source_id=row["source_id"], item_id=item_id,
                    snapshot_id=snapshot_id, lot=lot, quantity=quantity, expected_version=lot.version,
                    operator_id=operator_id, key=key)
        reserved.append(reservation.id)
        append_audit_event(db, event_category="business", result="success", source="system", module_code="warehouse",
            action_code="replenishment_supply.output_reserved", resource="inventory_reservation",
            actor=db.get(User, operator_id), entity_type="inventory_reservation", entity_id=reservation.id,
            customer_id=db.get(OrderItem, item_id).order.customer_id,
            details=dict(source_id=row["source_id"], order_item_id=item_id, snapshot_id=snapshot_id,
                lot_id=lot.id, quantity=quantity, allocation_rule="global_fifo_v1"))
        db.flush()
    return reserved


def reserve_material_output(db, *, source_id, item_id, snapshot_id, lot, quantity, expected_version, operator_id, key):
    """A completed physical product kept as semi stock still needs final posting.

    Its product-unit quantity is credited once against the frozen material
    requirement; source-board dimensions never multiply its yield again.
    """
    from sqlalchemy import update
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation, OrderItemSemiRequirement
    from app.services.semi_finished_inventory import save_order_item_semi_requirement
    from app.services.warehouse_inventory import _balances, _movement, _number as inventory_number, WarehouseInventoryError
    from app.services.production_workflow import lock_order_rows_for_production_transition, refresh_existing_production_task
    from app.services.finished_stock_identity import order_product_basis
    from app.api import requisition as req
    item = db.get(OrderItem, item_id)
    lock_order_rows_for_production_transition(db, [item.order_id])
    authorize_output(db, source_id=source_id, order_item_id=item_id, snapshot_id=snapshot_id, lot_id=lot.id, quantity=quantity)
    token = _reading.set(True)
    try:
        if snapshot_id:
            from app.models.product_bom import SalesOrderItemBomComponent
            snapshot = db.get(SalesOrderItemBomComponent, snapshot_id)
            need = req._bom_snapshot_requirements(db, snapshot)
            row = dict(need, customer_id=item.order.customer_id, pieces_per_box=need["physical_pieces_per_component"])
            basis = order_product_basis(db, item_id, row["product_id"])
        else:
            need = req._current_requisition_requirements(db, item)
            row = _ordinary_row(db, item, item.order, "whole", need)
            basis = order_product_basis(db, item_id, item.product_id) or _ordinary_basis(db, item, row)
    finally:
        _reading.reset(token)
    from app.services.processed_component_stock import eligible_output
    if (not basis or not eligible_output(db, lot, product_id=row["product_id"], customer_id=row["customer_id"], expected_basis=basis)
            or lot.version != expected_version or lot.quantity_available < quantity):
        raise WarehouseInventoryError("补库加工实物身份、数量或版本已变化", 409)
    multiplier = max(int(row["pieces_per_box"]), 1)
    required = int(need.get("required_piece_quantity", need.get("required_piece_qty")))
    requirement = db.scalar(select(OrderItemSemiRequirement).where(OrderItemSemiRequirement.order_item_id == item_id,
        OrderItemSemiRequirement.sales_order_item_bom_component_id == snapshot_id,
        OrderItemSemiRequirement.component_type == "whole"))
    if requirement is None:
        requirement = save_order_item_semi_requirement(db, order_item_id=item_id, component_type="whole",
            board_length_mm=row["report_length_mm"], board_width_mm=row["report_width_mm"],
            material_code=row["material"], flute_type=row["flute_type"], pieces_per_box=multiplier,
            stock_yield_per_sheet=int(row["yield_per_sheet"]), required_piece_quantity=required,
            operator_id=operator_id, sales_order_item_bom_component_id=snapshot_id)
    else:
        from app.services.warehouse_inventory import normalize_material_code
        expected = (row["customer_id"], _number(row["report_length_mm"]), _number(row["report_width_mm"]),
            normalize_material_code(row["material"]), row["flute_type"], multiplier, int(row["yield_per_sheet"]))
        actual = (requirement.customer_id, _number(requirement.board_length_mm), _number(requirement.board_width_mm),
            requirement.normalized_material_code, requirement.flute_type, requirement.pieces_per_box, requirement.stock_yield_per_sheet)
        if actual != expected:
            raise WarehouseInventoryError("已有片料需求冻结规格与补库产出不一致，请核对原需求", 409)
        # Finished coverage changes current demand, never this historical row's
        # original required quantity. Authorize_output bounds the fresh credit.
    detail = lot.semi_finished_detail
    from app.services.inventory_valuation import require_inherited_entry_cost
    require_inherited_entry_cost(db, lot)
    proof = dict(method="completed_replenishment_output", source_id=source_id, lot_id=lot.id,
        product_id=row["product_id"], physical_basis=basis, yield_factor=multiplier,
        pieces_per_box=multiplier,
        source_length_mm=detail.board_length_mm, source_width_mm=detail.board_width_mm,
        target_length_mm=requirement.board_length_mm, target_width_mm=requirement.board_width_mm,
        normalized_material_code=requirement.normalized_material_code, flute_type=requirement.flute_type,
        stock_yield_per_sheet=requirement.stock_yield_per_sheet,
        requires_production=True)
    before = _balances(lot)
    changed = db.execute(update(InventoryLot).where(InventoryLot.id == lot.id, InventoryLot.version == expected_version,
        InventoryLot.status == "active", InventoryLot.quantity_available >= quantity).values(
        quantity_available=InventoryLot.quantity_available - quantity, quantity_reserved=InventoryLot.quantity_reserved + quantity,
        version=InventoryLot.version + 1))
    if changed.rowcount != 1:
        raise WarehouseInventoryError("补库加工实物已被其他操作使用", 409)
    from app.core.time_contract import utc_now_naive
    reservation = InventoryReservation(reservation_number=inventory_number("RPO"), inventory_lot_id=lot.id,
        order_id=item.order_id, order_item_id=item.id, semi_requirement_id=requirement.id,
        sales_order_item_bom_component_id=snapshot_id, reservation_type="semi_order",
        reserved_stock_quantity=quantity, credited_requirement_quantity=quantity * multiplier,
        yield_factor=multiplier, status="active", idempotency_key=key,
        reservation_group_key=key, reservation_group_requested_quantity=quantity * multiplier,
        reserved_by=operator_id, reserved_at=utc_now_naive(), cut_plan_json=json.dumps(proof, ensure_ascii=False, sort_keys=True))
    db.add(reservation)
    db.flush()
    db.refresh(lot)
    _movement(db, lot=lot, movement_type="reserve", quantity=quantity, before=before, operator_id=operator_id,
        reason="已加工补库实物按订单需求分配，仍需确认成品", idempotency_key=key,
        reservation_id=reservation.id, related_order_id=item.order_id, related_order_item_id=item.id)
    refresh_existing_production_task(db, item.id)
    return reservation


def validate_material_output_reservation(db, reservation, lot, requirement, *, delivery=False):
    proof = _json(reservation.cut_plan_json)
    if proof.get("method") != "completed_replenishment_output":
        return False
    from app.services.warehouse_inventory import WarehouseInventoryError
    from app.models.stock_preparation import StockPreparationJob
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.services.processed_component_stock import eligible_output
    item = db.get(OrderItem, reservation.order_item_id)
    job = db.get(StockPreparationJob, lot.source_ref_id) if lot.source_ref_type == "stock_preparation" else None
    receipt = db.get(IncomingReceiptItem, job.receipt_item_id) if job else None
    frozen_job = _json(job.product_snapshot) if job else {}
    if (delivery or not item or not job or job.status != "completed" or not receipt or receipt.status != "posted"
            or receipt.stock_replenishment_item_id != proof.get("source_id")
            or requirement.order_item_id != item.id or requirement.customer_id != item.order.customer_id
            or requirement.sales_order_item_bom_component_id != reservation.sales_order_item_bom_component_id
            or requirement.pieces_per_box != proof.get("pieces_per_box")
            or requirement.normalized_material_code != proof.get("normalized_material_code")
            or requirement.flute_type != proof.get("flute_type")
            or requirement.stock_yield_per_sheet != proof.get("stock_yield_per_sheet")
            or frozen_job.get("pieces_per_box") != reservation.yield_factor
            or frozen_job.get("factor") != requirement.stock_yield_per_sheet
            or frozen_job.get("product_id") != proof.get("product_id")
            or job.expected_output != job.input_quantity * frozen_job.get("factor", 0) // max(frozen_job.get("pieces_per_box", 0), 1)
            or reservation.yield_factor != requirement.pieces_per_box
            or reservation.credited_requirement_quantity != reservation.reserved_stock_quantity * reservation.yield_factor
            or not eligible_output(db, lot, product_id=proof.get("product_id"), customer_id=item.order.customer_id,
                expected_basis=proof.get("physical_basis"))):
        raise WarehouseInventoryError("已加工补库实物的冻结来源或数量关系不一致，不能消耗或直接送货", 409)
    from app.services.finished_stock_identity import order_product_basis
    basis = order_product_basis(db, item.id, proof["product_id"])
    if basis is None:
        from app.api.requisition import _current_requisition_requirements
        token = _reading.set(True)
        try:
            row = _ordinary_row(db, item, item.order, "whole", _current_requisition_requirements(db, item))
            basis = _ordinary_basis(db, item, row)
        finally:
            _reading.reset(token)
    from app.services.finished_stock_identity import matches_stock_identity
    if not basis or not matches_stock_identity(proof["physical_basis"], basis):
        raise WarehouseInventoryError("已加工补库实物与订单冻结工艺不一致", 409)
    return True
