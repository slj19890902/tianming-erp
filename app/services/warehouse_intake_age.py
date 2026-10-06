"""Read-only, customer-scoped latest genuine inbound dates for map colouring.

Lot stock_date remains the batch fact. Closed/consumed inbound lots participate;
location transfers, returns and quantity corrections never create a new date.
"""
from __future__ import annotations

from datetime import date
import hashlib
import json

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models.warehouse_inventory import (
    FinishedGoodsInventoryDetail, InventoryLot, InventoryLotTransfer, InventoryMovement,
)


def intake_bucket(days: int | None) -> str:
    if days is None:
        return "unknown"
    for upper, key in ((30, "0_30"), (90, "31_90"), (180, "91_180"), (364, "181_364")):
        if days <= upper:
            return key
    return "365_plus"


def intake_identity(lot: InventoryLot, *, body_product_id: int | None = None) -> str:
    detail = lot.finished_detail
    if detail is not None:
        try:
            basis = json.loads(detail.physical_basis_json or "null")
        except (ValueError, TypeError):
            basis = None
        # Real product identity, never inventory-code/name text. Frozen body and
        # assembled stock remain different even when they share a product ID.
        # Old stock may have no JSON while a newer production has a full frozen
        # document. Same product/stage must still refresh; quantity allocation
        # continues using the stricter frozen identity in its existing service.
        stage = basis.get("inventory_stage", "complete") if isinstance(basis, dict) else "complete"
        identity = [lot.inventory_type, lot.unit, detail.product_id, stage]
        if detail.product_id is None:
            identity += ["unassigned", lot.id]
    elif lot.semi_finished_detail is not None:
        detail = lot.semi_finished_detail
        identity = [lot.inventory_type, lot.unit, detail.owner_customer_id]
        identity += [getattr(detail, key) for key in (
            "internal_name", "material_code_snapshot", "normalized_material_code", "layer_count", "flute_type",
            "board_length_mm", "board_width_mm", "component_type", "pieces_per_box",
            "stock_yield_per_sheet", "sheet_type", "crease_type", "crease_left_mm",
            "crease_middle_mm", "crease_right_mm",
        )]
        identity.append(sorted(binding.product_id for binding in lot.allowed_products))
    elif lot.inventory_type == "assembly_body" and body_product_id is not None:
        identity = [lot.inventory_type, lot.unit, body_product_id, "body"]
    else:
        identity = ["unidentified", lot.id]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True,
                                     default=str).encode()).hexdigest()


def build_intake_age_projection(
    db: Session, lots: list[InventoryLot], *, visible_customer_ids: set[int] | None,
    as_of: date,
) -> dict[int, dict]:
    """Batch-load history outside map floor/search/page/current-balance filters."""
    if not lots:
        return {}
    from app.api.warehouse import _visible_lot_condition
    from app.models.production import ProductionCompletion
    from app.models.stock_preparation import StockPreparationJob
    from app.models.bom_subkit import SubkitConversion, SubkitReceiptOutput
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.inventory_onboarding import InventoryOnboardingLine
    from app.models.multilevel_bom import BomAssembly, BomBodyInventoryDetail
    from app.models.external_packaging_purchase import ExternalPackagingReceiptItem, ExternalPackagingReceiptReversal
    from app.core.time_contract import utc_naive_to_beijing_date

    product_ids = {lot.finished_detail.product_id for lot in lots
                   if lot.finished_detail is not None and lot.finished_detail.product_id is not None}
    conditions = [InventoryLot.id.in_([lot.id for lot in lots])]
    if product_ids:
        conditions.append(InventoryLot.id.in_(select(FinishedGoodsInventoryDetail.inventory_lot_id)
            .where(FinishedGoodsInventoryDetail.product_id.in_(product_ids))))
    if any(lot.semi_finished_detail is not None for lot in lots):
        conditions.append(InventoryLot.inventory_type == "semi_finished")
    if any(lot.inventory_type == "assembly_body" for lot in lots):
        conditions.append(InventoryLot.inventory_type == "assembly_body")
    selected_ids = select(InventoryLot.id).where(or_(*conditions))
    ancestors = select(InventoryLotTransfer.source_lot_id).where(
        InventoryLotTransfer.target_lot_id.in_(selected_ids)).cte("intake_ancestors", recursive=True)
    ancestors = ancestors.union(select(InventoryLotTransfer.source_lot_id).join(
        ancestors, InventoryLotTransfer.target_lot_id == ancestors.c.source_lot_id))
    query = select(InventoryLot).where(or_(*conditions,
        InventoryLot.id.in_(select(ancestors.c.source_lot_id)))).options(
        joinedload(InventoryLot.finished_detail), joinedload(InventoryLot.semi_finished_detail),
        selectinload(InventoryLot.allowed_products))
    # Filter BEFORE deriving identities or latest dates. An invisible inbound
    # event must never change a visible item's colour.
    if visible_customer_ids is not None:
        query = query.where(_visible_lot_condition(visible_customer_ids))
    history = list(db.scalars(query).unique())
    ids = [lot.id for lot in history]
    movements = list(db.scalars(select(InventoryMovement).where(
        InventoryMovement.inventory_lot_id.in_(ids),
        or_(InventoryMovement.movement_type.in_(("manual_in", "adjust")),
            InventoryMovement.reversal_of_movement_id.is_not(None)))))
    def is_real_inbound(movement: InventoryMovement) -> bool:
        return (movement.movement_type == "manual_in" and movement.quantity > 0
                and movement.after_available + movement.after_reserved + movement.after_damaged
                > movement.before_available + movement.before_reserved + movement.before_damaged)

    inbound = {m.inventory_lot_id for m in movements if is_real_inbound(m)}
    first_inbound = {}
    for movement in sorted(movements, key=lambda m: m.id):
        if is_real_inbound(movement):
            first_inbound.setdefault(movement.inventory_lot_id, movement)
    reversed_inbounds = {m.reversal_of_movement_id for m in movements if m.reversal_of_movement_id}
    void_lots = {m.inventory_lot_id for m in movements if (
        m.movement_type == "manual_in" and m.id in reversed_inbounds) or (
        m.movement_type == "adjust" and (str(m.reason or "").startswith("作废误录")
        or str(m.idempotency_key or "").startswith("unassemble-out:")))}
    references: dict[str, set[int]] = {}
    for lot in history:
        if lot.source_ref_type and lot.source_ref_id is not None:
            references.setdefault(lot.source_ref_type, set()).add(lot.source_ref_id)

    def status_map(model, ref, status="status", key="id"):
        values = references.get(ref, set())
        if not values:
            return {}
        return dict(db.execute(select(getattr(model, key), getattr(model, status))
            .where(getattr(model, key).in_(values))).all())

    completions = status_map(ProductionCompletion, "production_completion")
    preparations = status_map(StockPreparationJob, "stock_preparation")
    assemblies = status_map(BomAssembly, "bom_assembly")
    conversions = status_map(SubkitConversion, "subkit_conversion")
    subkit_receipts = status_map(SubkitReceiptOutput, "subkit_receipt", "reversed", "allocation_id")
    receipts = {**status_map(IncomingReceiptItem, "incoming_receipt_item"),
                **status_map(IncomingReceiptItem, "stock_replenishment_receipt")}
    onboarding_dates = status_map(InventoryOnboardingLine, "inventory_onboarding_line", "stocktake_date")
    external_ids = set().union(*(references.get(ref, set()) for ref in (
        "external_packaging_receipt_item", "direct_external_receipt", "bom_external_receipt")))
    external_receipts = set(db.scalars(select(ExternalPackagingReceiptItem.id).where(
        ExternalPackagingReceiptItem.id.in_(external_ids),
        ~ExternalPackagingReceiptItem.receipt_id.in_(select(ExternalPackagingReceiptReversal.receipt_id))))) if external_ids else set()
    latest: dict[str, date] = {}
    event_dates: dict[int, date] = {}
    body_products = dict(db.execute(select(BomBodyInventoryDetail.inventory_lot_id,
        BomBodyInventoryDetail.product_id).where(BomBodyInventoryDetail.inventory_lot_id.in_(ids)))) if any(
            lot.inventory_type == "assembly_body" for lot in history) else {}
    identities = {lot.id: intake_identity(lot, body_product_id=body_products.get(lot.id)) for lot in history}
    for lot in history:
        if lot.id in void_lots:
            continue
        ref, ref_id = lot.source_ref_type, lot.source_ref_id
        genuine = lot.source_type in {"manual", "stocktake"}
        movement = first_inbound.get(lot.id)
        event_date = utc_naive_to_beijing_date(movement.created_at) if movement is not None else None
        if genuine:
            event_date = (onboarding_dates.get(ref_id) or event_date) if ref == "inventory_onboarding_line" else event_date
        if ref == "production_completion":
            genuine = completions.get(ref_id) == "posted" and lot.id in inbound
        elif ref == "stock_preparation":
            genuine = preparations.get(ref_id) == "completed" and lot.id in inbound
        elif ref == "bom_assembly":
            genuine = assemblies.get(ref_id) == "posted" and lot.id in inbound
        elif ref == "subkit_conversion":
            genuine = conversions.get(ref_id) == "posted" and lot.id in inbound
        elif ref == "preparation_assembly":
            genuine = lot.id in inbound and lot.id not in void_lots
        elif ref == "subkit_receipt":
            genuine = subkit_receipts.get(ref_id) is False and lot.id in inbound
        elif ref in {"external_packaging_receipt_item", "direct_external_receipt", "bom_external_receipt"}:
            genuine = ref_id in external_receipts and lot.id in inbound
        elif lot.source_type in {
            "purchase_reserve", "purchase_surplus", "replenishment"
        }:
            genuine = ref in {"incoming_receipt_item", "stock_replenishment_receipt"} and receipts.get(ref_id) == "posted" and lot.id in inbound
            # Paper receipt cannot refresh a finished product. Finished
            # replenishment is included only when it really posted finished stock.
            if lot.inventory_type == "finished" and ref == "incoming_receipt_item":
                genuine = False
        if genuine and event_date is not None and event_date <= as_of:
            identity = identities[lot.id]
            latest[identity] = max(latest.get(identity, event_date), event_date)
            event_dates[lot.id] = event_date
    # Identity corrections may split into a different real product ID. Recover
    # their original inbound through proven transfer facts, never created_at on
    # the descendant. Every ancestor has already passed the customer scope.
    parents: dict[int, set[int]] = {}
    for source_id, target_id in db.execute(select(InventoryLotTransfer.source_lot_id,
        InventoryLotTransfer.target_lot_id).where(InventoryLotTransfer.target_lot_id.in_(ids))):
        if source_id in identities and source_id != target_id:
            parents.setdefault(target_id, set()).add(source_id)
    for lot in history:
        if lot.source_type != "transfer" or lot.id in void_lots:
            continue
        pending, seen, inherited = list(parents.get(lot.id, ())), {lot.id}, []
        while pending:
            source_id = pending.pop()
            if source_id in seen:
                continue
            seen.add(source_id)
            if source_id in event_dates:
                inherited.append(event_dates[source_id])
            pending.extend(parents.get(source_id, ()))
        if inherited:
            identity, inherited_date = identities[lot.id], max(inherited)
            latest[identity] = max(latest.get(identity, inherited_date), inherited_date)
    result = {}
    for lot in lots:
        # Do not return metadata for a seed outside this account's actual scope.
        if lot.id not in identities:
            continue
        identity = identities[lot.id]
        baseline = latest.get(identity)
        days = max(0, (as_of - baseline).days) if baseline is not None else None
        result[lot.id] = dict(intake_identity_key=identity,
            intake_date=baseline.isoformat() if baseline else None,
            intake_age_days=days, intake_age_bucket=intake_bucket(days))
    return result
