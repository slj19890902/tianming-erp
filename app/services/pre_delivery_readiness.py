"""Read-only pre-delivery coverage from effective purchase and receipt facts.

Quantities returned here are customer finished units, never raw sheet counts.
Missing source contracts remain review items; status text is not quantity evidence.
"""
from fractions import Fraction

from sqlalchemy import exists, select

from app.models.order import Order, OrderItem
from app.models.production import ProductionCompletion
from app.models.requisition import RequisitionItem
from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot
from app.services.incoming_receipts import (
    IncomingReceiptError, _target, current_supplier_order_items, source_summary,
)
from app.services.production_workflow import (
    cutting_output_factor, normalized_completion_output, production_pieces_per_box,
    production_output_quantity,
)
from app.services.receipt_purpose_distribution import receipt_purpose_source_totals


def _outstanding(summary):
    if summary["resolution_action"] == "accept_short" and summary["resolution_status"] == "resolved":
        return 0
    return max(int(summary["remaining_quantity"]), 0)


def _paper_capacity(db, item):
    """Return received and received+inbound capacity, with unresolved reasons."""
    from app.models.product_bom import SalesOrderItemBomComponent
    # Component requirements must be evaluated as complete kits, not added as boxes.
    if db.scalar(select(SalesOrderItemBomComponent.id).where(
        SalesOrderItemBomComponent.sales_order_item_id == item.id).limit(1)):
        return 0, 0, ["组合产品需按组件齐套核对，未将单个组件算作整套在途"]
    supplier = current_supplier_order_items(db, [item.id])
    requisitions = list(db.scalars(select(RequisitionItem).where(
        RequisitionItem.order_item_id == item.id)))
    # Receiving uses supplier lines for ordinary products and requisition lines
    # for cover/base or BOM. Do not count both representations of a purchase.
    from app.services.incoming_receipts import _component_kind
    component_rows = [r for r in requisitions if _component_kind(r.product_name_snapshot) in {"cover", "base"}]
    keys = ([f"r{r.id}" for r in component_rows] if component_rows else
            [f"so{r.id}" for r in supplier] if supplier else
            [f"r{r.id}" for r in requisitions])
    if not keys and int(item.requisition_qty or 0) > 0 and item.requisition_status in {"已报料", "供应商已排单"}:
        keys = [str(item.id)]
    received_by_component, future_by_component, reasons = {}, {}, []
    for key in keys:
        try:
            target = _target(db, key, allow_closed=True)
        except IncomingReceiptError:
            continue  # cancelled, voided or superseded source
        summary = source_summary(db, target)
        source = target.supplier_order_item or target.requisition_item
        component = target.component_type
        if not summary["latest_receipt_item_id"] and (
            (target.requisition_item is not None and target.requisition_item.status == "已入库")
            or item.material_status == "received"
        ):
            reasons.append("已收料状态缺少有效实收记录，需核对原采购")
            continue
        if target.bom_snapshot is not None:
            reasons.append("组合产品采购需按组件齐套核对")
            continue
        snapshots = list(db.scalars(select(PurchasePurposeSourceSnapshot).where(
            PurchasePurposeSourceSnapshot.supplier_requisition_order_item_id == source.id
            if target.supplier_order_item else
            PurchasePurposeSourceSnapshot.material_requisition_item_id == source.id
        ))) if source is not None else []
        if snapshots:
            for snapshot in snapshots:
                if snapshot.source_order_item_id != item.id or snapshot.source_bom_requisition_source_id is not None:
                    reasons.append("采购用途来源需核对")
                    continue
                if snapshot.yield_per_sheet_snapshot <= 0 or snapshot.pieces_per_finished_snapshot <= 0:
                    reasons.append("缺少有效冻结换算比例")
                    continue
                total_received, received_order, _ = receipt_purpose_source_totals(db, snapshot.id)
                if total_received != summary["cumulative_received_quantity"]:
                    reasons.append("实收与用途分配数量不一致，需核对收料")
                    continue
                # Reserve-purpose sheets must never cover this sales order.
                pending_order = min(_outstanding(summary), max(snapshot.order_purpose_sheet_qty - received_order, 0))
                ratio = Fraction(snapshot.yield_per_sheet_snapshot, snapshot.pieces_per_finished_snapshot)
                k = snapshot.component_type
                received_by_component[k] = received_by_component.get(k, Fraction(0)) + received_order * ratio
                future_by_component[k] = future_by_component.get(k, Fraction(0)) + (received_order + pending_order) * ratio
        elif source is not None and source.purpose_contract_status == "frozen":
            reasons.append("采购用途已冻结但缺少来源快照")
        else:
            # Legacy rows retain their own frozen cut/piece basis. Never use the
            # live common-box definition or fall back to the sales quantity.
            factor = cutting_output_factor(source.cutting_mode if target.supplier_order_item else
                                           source.special_process if source else item.special_process)
            pieces = int(source.pieces_per_box or 0) if source else production_pieces_per_box(item)
            if pieces <= 0:
                pieces = production_pieces_per_box(item)
            ratio = Fraction(factor, pieces)
            received = min(summary["cumulative_received_quantity"], summary["planned_quantity"])
            if summary["resolution_action"] == "all_to_production":
                received = summary["cumulative_received_quantity"]
            received_by_component[component] = received_by_component.get(component, Fraction(0)) + received * ratio
            future_by_component[component] = future_by_component.get(component, Fraction(0)) + (received + _outstanding(summary)) * ratio
    if {"cover", "base"}.intersection(future_by_component):
        # An absent half is zero, not an implicit complete set.
        received = min(received_by_component.get(k, 0) for k in ("cover", "base"))
        future = min(future_by_component.get(k, 0) for k in ("cover", "base"))
    else:
        received, future = sum(received_by_component.values()), sum(future_by_component.values())
    if not keys and item.requisition_status in {"已报料", "供应商已排单"}:
        reasons.append("只有报料状态，缺少有效采购数量")
    return int(received), int(future), reasons


def _external_inbound(db, item):
    from app.models.external_packaging_purchase import (
        ExternalPackagingPurchaseItem as Purchase, ExternalPackagingPurchaseOrder as Header,
        ExternalPackagingPurchaseCancellation as Cancellation, ExternalPackagingReceiptItem as Receipt,
    )
    from app.models.order_external_packaging import SalesOrderItemExternalComponent
    from app.services.direct_external_finished import eligible
    from app.services.external_receipt_state import active_receipt_item
    if not eligible(db, item):
        return 0, ["外购组件需核对齐套关系，未直接折算整套在途"]
    a, b = item.external_packaging_order_quantity_basis_snapshot, item.external_packaging_purchase_quantity_basis_snapshot
    if not a or not b or a <= 0 or b <= 0:
        return 0, ["外购订单缺少冻结换算比例"]
    ratio, inbound, reasons = Fraction(a) / Fraction(b), 0, []
    purchases = list(db.scalars(select(Purchase).join(Header, Header.id == Purchase.purchase_order_id).where(
        Purchase.sales_order_item_id == item.id, Header.status == "confirmed",
        ~exists(select(Cancellation.id).where(Cancellation.purchase_order_id == Header.id)))))
    for purchase in purchases:
        component = db.get(SalesOrderItemExternalComponent, purchase.order_component_id)
        if component is None or component.source_kind != "direct_product" or purchase.purchase_unit != item.external_packaging_purchase_unit_snapshot:
            reasons.append("外购采购来源或单位需核对")
            continue
        received = sum((Fraction(q) for q in db.scalars(select(Receipt.received_quantity).where(
            Receipt.purchase_item_id == purchase.id, active_receipt_item()))), Fraction(0))
        # Difference of cumulative floors preserves fractional-unit remainders.
        inbound += max(int(Fraction(purchase.purchase_quantity) * ratio) - int(received * ratio), 0)
    if not purchases and item.requisition_status == "外购包材已采购":
        reasons.append("未找到有效外购采购明细")
    return inbound, reasons


def order_readiness(db, item):
    from app.services.tianhua_pre_delivery import _finished_inventory_quantity
    pending = max(int(item.quantity) - int(item.delivered_quantity or 0), 0)
    finished = min(_finished_inventory_quantity(db, item), pending)
    if item.supply_mode_snapshot == "external_purchase":
        inbound, reasons = _external_inbound(db, item)
        processing = 0  # direct receipts are already finished stock
    else:
        received, future, reasons = _paper_capacity(db, item)
        completed = sum(max(normalized_completion_output(item, row), production_output_quantity(
            row.material_input_quantity, cutting_output_factor(item.special_process), production_pieces_per_box(item)))
            for row in db.scalars(
            select(ProductionCompletion).where(ProductionCompletion.order_item_id == item.id,
                                              ProductionCompletion.status == "posted")))
        processing = max(received - completed, 0)
        inbound = max(future - max(received, completed), 0)
    processing = min(processing, pending - finished)
    inbound = min(inbound, pending - finished - processing)
    return dict(finished_available=finished, pending_processing=processing,
                effective_inbound=inbound, pending_quantity=pending, review_reasons=list(dict.fromkeys(reasons)))


def allocate_readiness(requested, selections, facts, used):
    """Allocate a batch once per order, including repeated rows of the same code."""
    result = dict(finished_available=0, pending_processing=0, effective_inbound=0,
                  new_purchase_shortage=0, unresolved_quantity=0,
                  pending_review=False, review_reasons=[], generic_material_auto_applied=False)
    fields = ("finished_available", "pending_processing", "effective_inbound")
    remaining = max(int(requested), 0)
    if sum(q for _, q in selections) != remaining:
        result["review_reasons"].append("订单分配数量与预送数量不一致")
    for oid, quantity in selections:
        need = min(max(int(quantity), 0), remaining)
        remaining -= need
        value = facts.get(oid)
        if value is None:
            result["unresolved_quantity"] += need
            result["review_reasons"].append("请核对订单来源")
            continue
        claimed = used.setdefault(oid, {k: 0 for k in fields})
        for field in fields:
            take = min(need, max(value[field] - claimed[field], 0))
            claimed[field] += take
            result[field] += take
            need -= take
        reasons = value["review_reasons"] if need else []
        result["unresolved_quantity" if reasons else "new_purchase_shortage"] += need
        result["review_reasons"].extend(reasons)
    result["unresolved_quantity"] += remaining
    result["review_reasons"] = list(dict.fromkeys(result["review_reasons"]))
    result["pending_review"] = bool(result["review_reasons"] or result["unresolved_quantity"])
    return result


def _refresh_bound_match_projection(db, batch, row):
    """Refresh display fields with the original matcher, never rebind or persist."""
    from app.services.tianhua_pre_delivery import (
        GENERATABLE, STATUS_LABELS, RecognizedRow, preprocess_row,
    )
    if row.get("status") not in GENERATABLE or not row.get("order_item_id"):
        return
    fresh = preprocess_row(db, RecognizedRow(
        row["row_no"], row.get("raw_text") or "", row.get("stock_code"),
        row.get("image_qty"), row.get("image_order_no"),
    ), batch.pre_delivery_date, batch.customer_id)
    # A newly preferred candidate is not permission to change the saved choice.
    if (fresh.get("order_item_id") != row["order_item_id"]
            or fresh.get("product_id") != row.get("product_id")):
        return
    for key in ("status", "warning", "available_qty", "system_pending_qty"):
        row[key] = fresh[key]
    row["status_label"] = STATUS_LABELS.get(row["status"], row["status"])
    # Workbook quantity/format issues are immutable source warnings.
    issues = (row.get("source_payload") or {}).get("issues") or []
    row["warning"] = "；".join(dict.fromkeys(filter(None, [row["warning"], *issues])))


def refresh_excel_readiness(db, batch, payload, overrides=None):
    """Recompute existing uploads without persisting a diagnostic or selections."""
    from app.models.tianhua_pre_delivery import PreDeliverySourceAllocation
    from app.services.order_status_policy import DELIVERY_CANDIDATE_ORDER_STATUSES
    overrides = overrides or {}
    facts, used = {}, {}
    saved = {}
    ids = [r["item_id"] for r in payload["items"]]
    for allocation in db.scalars(select(PreDeliverySourceAllocation).where(
        PreDeliverySourceAllocation.import_item_id.in_(ids))):
        saved.setdefault(allocation.import_item_id, []).append((allocation.order_item_id, allocation.allocated_qty))
    for row in payload["items"]:
        source_payload = row.setdefault("source_payload", {})
        candidates = []
        for item, order in db.execute(select(OrderItem, Order).join(Order, Order.id == OrderItem.order_id).where(
            Order.customer_id == batch.customer_id, OrderItem.product_id == row.get("product_id"),
            OrderItem.delivered_quantity < OrderItem.quantity, OrderItem.is_force_closed.is_(False),
            Order.status.in_(DELIVERY_CANDIDATE_ORDER_STATUSES),
        ).order_by(Order.delivery_date, Order.order_date, OrderItem.id)):
            if item.id not in facts:
                facts[item.id] = order_readiness(db, item)
            candidates.append(dict(order_item_id=item.id, order_id=order.id,
                order_number=order.order_number, customer_order_no=order.customer_po,
                pending_quantity=facts[item.id]["pending_quantity"], finished_available=facts[item.id]["finished_available"],
                delivery_date=order.delivery_date.isoformat() if order.delivery_date else None))
        change = overrides.get(row["item_id"])
        # Before a draft exists, the initial suggested delivery may be only the
        # finished subset. Diagnose the file's full demand, not that suggestion.
        quantity = (change.get("final_delivery_qty") if change else
                    row.get("final_delivery_qty") if payload.get("draft") else row.get("image_qty"))
        requested = int(quantity or row.get("image_qty") or 0)
        # Multiple orders are alternatives until explicitly allocated, never
        # cumulative coverage of whichever order happened to match first.
        selections = ([(x["order_item_id"], x["quantity"]) for x in change.get("allocations", [])]
                      if change is not None else saved.get(row["item_id"], []))
        if not selections:
            bound = change.get("order_item_id") if change else row.get("order_item_id")
            selections = [(bound, requested)] if bound else []
        allowed = {x["order_item_id"] for x in candidates}
        scoped_facts = {oid: facts[oid] for oid in allowed}
        diagnostic = allocate_readiness(requested, selections, scoped_facts, used)
        if change is not None or len(selections) > 1:
            # Re-read positions only for the unchanged, scoped single binding.
            # Alternative/multi-order choices still cannot inherit old positions.
            row["pick_locations"] = []
            if (len(selections) == 1 and selections[0][0] in allowed
                    and selections[0][0] == row.get("order_item_id")
                    and diagnostic["finished_available"] > 0):
                from app.api.deliveries import _inventory_sources_for_order_item
                sources = _inventory_sources_for_order_item(
                    db, order_item=db.get(OrderItem, selections[0][0]),
                    planned_delivery_quantity=min(requested, diagnostic["finished_available"]),
                    delivery_item_id=row.get("delivery_item_id"), dispatched=False,
                )
                row["pick_locations"] = [
                    {"location_id": source.get("location_id"),
                     "location_code": source.get("location_code"),
                     "location_name": source.get("location_name"),
                     "quantity": int(source.get("quantity_to_pick_stock") or 0),
                     "source_type": source.get("source_type")}
                    for source in sources
                    if source.get("source_type") == "finished"
                    and int(source.get("quantity_to_pick_stock") or 0) > 0
                ]
        for candidate in candidates:
            candidate["_selected"] = any(oid == candidate["order_item_id"] for oid, _ in selections) if saved.get(row["item_id"]) else False
            candidate["_qty"] = next((q for oid, q in selections if oid == candidate["order_item_id"]), None)
        # Only refresh the original single binding; preview allocations must
        # not silently replace its match, quantities, warnings or selections.
        if len(selections) == 1 and selections[0][0] == row.get("order_item_id"):
            _refresh_bound_match_projection(db, batch, row)
        source_payload.update(candidates=candidates, shortage_diagnostic=diagnostic)
        if diagnostic["pending_review"]:
            status, label = "needs_review", "采购数量/订单分配待核对"
        elif diagnostic["new_purchase_shortage"]:
            status, label = "purchase_required", "仍有缺口，需订材料"
        elif diagnostic["effective_inbound"]:
            status, label = "incoming", "部分材料在途/待收料"
        elif diagnostic["pending_processing"]:
            status, label = "pending_production", "材料已收，待加工"
        else:
            status, label = "ready", "仓库可拿" if requested else "未分配送货数量"
        row.update(fulfillment_status=status, fulfillment_label=label,
                   finished_available_qty=diagnostic["finished_available"],
                   shortage_qty=max(requested - diagnostic["finished_available"], 0))
    return payload
