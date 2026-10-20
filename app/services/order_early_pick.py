"""Read-only picking projection from an imported order, before any delivery."""
from collections import defaultdict
from datetime import datetime, timezone
from fractions import Fraction
import json
import re

from fastapi import HTTPException
from sqlalchemy import select

from app.models.customer import Customer
from app.models.order import OrderItem
from app.models.order_import_source import OrderImportSource, OrderImportSourceLine
from app.models.warehouse_inventory import InventoryLot, WarehouseLocation, WarehouseLocationAlias
from app.services.excel_document_import import PICK_LIST_TEMPLATE


def _document(raw):
    try:
        result = json.loads(raw or "{}")
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def source_for_order(db, order_id):
    source = db.scalar(select(OrderImportSource).where(OrderImportSource.order_id == order_id))
    if source and _document(source.confirmation_summary_json).get("source_template") == PICK_LIST_TEMPLATE:
        return source
    return None


def _label(value):
    return re.sub(r"\s+", "", str(value or "")).upper().replace("－", "-")


def location_index(db):
    index = defaultdict(set)
    locations = {row.id: row for row in db.scalars(select(WarehouseLocation))}
    for location in locations.values():
        # Inactive aliases are included in uniqueness checks, but are never pickable.
        for value in (location.location_code, location.location_name):
            if _label(value):
                index[_label(value)].add(location.id)
        rack = re.sub(r"^(货架|货|架)", "", _label(location.rack_display_name))
        if rack and location.level_no:
            index[f"{rack}-{location.level_no}"].add(location.id)
    for alias in db.scalars(select(WarehouseLocationAlias)):
        index[_label(alias.alias_text)].add(alias.location_id)
    return index, locations


def resolve_location(raw, index, locations):
    key = _label(raw)
    if not key or key == "板":
        return None, "位置待核对"
    matches = index.get(key, set())
    if len(matches) != 1:
        return None, "位置待核对（未唯一匹配）"
    location = locations.get(next(iter(matches)))
    if location is None or not location.is_active:
        return None, "指定位置已停用，请核对"
    return location.id, None


def build_early_pick(db, order):
    from app.api.deliveries import _inventory_sources_for_order_item, _delivery_location_metadata
    from app.services.pre_delivery_readiness import order_readiness
    from app.services.warehouse_inventory import finished_inventory_candidates, WarehouseInventoryError
    from app.services.direct_external_finished import eligible
    from app.services.delivery_quantities import (
        order_basis, available_customer_quantity, physical_for, QuantityContractError,
    )

    source = source_for_order(db, order.id)
    if source is None:
        raise HTTPException(404, "此订单没有位置清单来源")
    if order.status in {"cancelled", "closed", "已作废", "已关闭"}:
        raise HTTPException(409, "订单已作废或关闭，不能生成提前备货单")
    lines = list(db.scalars(select(OrderImportSourceLine).where(
        OrderImportSourceLine.source_id == source.id).order_by(OrderImportSourceLine.source_position)))
    index, locations = location_index(db)
    items = {item.id: item for item in db.scalars(select(OrderItem).where(OrderItem.order_id == order.id))}
    free_used = defaultdict(int)
    reserved_used = defaultdict(int)
    rows = []
    for line in lines:
        saved = _document(line.confirmation_summary_json).get("pick_list") or {}
        item = items.get(line.order_item_id)
        raw_location = saved.get("location") or ""
        row = dict(sequence=line.source_position, source_row=line.source_row,
                   source_sheet=line.source_sheet, requested_location=raw_location,
                   source_stock_reference=saved.get("stock_reference"),
                   product_code=saved.get("original_code"), product_name="",
                   order_quantity=saved.get("original_quantity"), delivered_quantity=0,
                   pending_quantity=None, available_quantity=None, shortage_quantity=None,
                   unit="", locations=[], notes=[])
        rows.append(row)
        if item is None:
            row["notes"].append("原订单明细已变更或移除，请核对")
            continue
        row.update(product_code=item.snapshot_product_code or saved.get("original_code"),
                   product_name=item.snapshot_product_name, order_quantity=item.quantity,
                   delivered_quantity=int(item.delivered_quantity or 0),
                   unit=item.sales_unit_snapshot or "单位待核对")
        pending = max(int(item.quantity) - int(item.delivered_quantity or 0), 0)
        row["pending_quantity"] = pending
        if item.is_force_closed:
            row.update(pending_quantity=0, available_quantity=0, shortage_quantity=0)
            row["notes"].append("此明细已关闭，不再备货")
            continue
        location_id, issue = resolve_location(raw_location, index, locations)
        if issue:
            row["notes"].append(issue)
        # Readiness uses frozen order/BOM and receipt contracts. No material or
        # unassembled component is converted to finished inventory here.
        facts = order_readiness(db, item)
        row["notes"].extend(facts["review_reasons"])
        if facts["pending_processing"]:
            row["notes"].append(f"待加工 {facts['pending_processing']} {row['unit']}")
        if facts["effective_inbound"]:
            row["notes"].append(f"材料在途对应 {facts['effective_inbound']} {row['unit']}")
        if pending == 0:
            row.update(available_quantity=0, shortage_quantity=0)
            row["notes"].append("已送足")
            continue
        if location_id is None:
            continue
        if not _delivery_location_metadata(db, locations[location_id], finished=True)["location_operational"]:
            row["notes"].append("指定位置当前不可拿货，请核对地图位置")
            continue
        sources = _inventory_sources_for_order_item(
            db, order_item=item, planned_delivery_quantity=pending, dispatched=False)
        available = 0
        other_location_stock = False
        for entry in sources:
            if entry.get("source_type") != "finished":
                if entry.get("source_type") == "component_stock":
                    row["notes"].append("子件已备，未作为成套成品计数，请按齐套情况核对")
                continue
            credit = Fraction(str(entry.get("quantity_to_pick_requirement") or 0))
            if credit.denominator != 1 or credit <= 0:
                continue
            lot = db.get(InventoryLot, entry.get("lot_id"))
            if lot is None or lot.status != "active" or not entry.get("location_operational"):
                continue
            if entry.get("location_id") != location_id:
                other_location_stock = True
                continue
            physical = int(entry.get("quantity_to_pick_stock") or 0)
            remaining_physical = max(int(lot.quantity_reserved or 0) - reserved_used[lot.id], 0)
            if physical <= 0 or physical > remaining_physical:
                row["notes"].append("已预占实物数量待核对")
                continue
            amount = min(int(credit), pending - available)
            if amount != int(credit):
                row["notes"].append("预占数量与当前需求待核对")
                continue
            available += amount
            reserved_used[lot.id] += physical
            row["locations"].append(dict(location=entry.get("location_name") or raw_location,
                                         quantity=amount, physical_quantity=physical, kind="本单已预留"))
        try:
            candidates = finished_inventory_candidates(db, item.id) if available < pending else []
        except WarehouseInventoryError:
            # Once requisition/production has started, only the order's own
            # reservations may be picked; other stock is not silently adopted.
            candidates = []
        try:
            basis = order_basis(item, order.customer_id) if eligible(db, item) else None
            for lot in candidates:
                if lot.warehouse_location_id != location_id:
                    other_location_stock = True
                    continue
                physical = max(int(lot.quantity_available or 0) - free_used[lot.id], 0)
                capacity = available_customer_quantity(basis, physical) if basis else physical
                amount = min(capacity, pending - available)
                if basis:
                    # Keep complete frozen customer/physical groups.
                    group = int(basis["customer_basis"])
                    amount = amount // group * group
                if amount <= 0:
                    continue
                taken = physical_for(basis, amount) if basis else amount
                free_used[lot.id] += taken
                available += amount
                row["locations"].append(dict(location=locations[location_id].location_name,
                    quantity=amount, physical_quantity=taken, kind="可用成品参考，尚未预留"))
                if available >= pending:
                    break
        except QuantityContractError:
            row["notes"].append("实物单位换算待核对")
        row.update(available_quantity=available, shortage_quantity=max(pending - available, 0))
        if available < pending:
            row["notes"].append("指定位置不足，待补货/生产")
        if other_location_stock:
            row["notes"].append("其他位置有成品，未计入指定位置可拿数量")
        row["notes"] = list(dict.fromkeys(row["notes"]))
    customer = db.get(Customer, order.customer_id)
    return dict(order_id=order.id, order_number=order.order_number,
                customer_name=customer.name if customer else "", customer_po=order.customer_po,
                delivery_date=order.delivery_date, source_name=source.source_name_snapshot,
                generated_at=datetime.now(timezone.utc).isoformat(), rows=rows,
                notice="提前备货参考，不扣库存、不代表发货。按指定位置核对后填写实拿；出库仍需原送货流程。")
