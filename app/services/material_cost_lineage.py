"""Freeze and report purchase-receipt based direct-material delivery cost.

Operational delivery is never blocked merely because accounting evidence is
missing.  Instead, only a fully traceable purchase receipt may create an
immutable cost fact, and month close remains fail-closed while gaps exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryItem
from app.models.material_cost import FinanceDeliveryMaterialCostFact
from app.models.product_bom import BomComponentDirectDeliveryAllocation
from app.models.production import ProductionCompletion
from app.models.purchase_receipt import (
    IncomingReceiptPurposeAllocation,
    IncomingReceiptPurposeReversal,
    PurchaseReceiptFact,
)
from app.models.warehouse_inventory import (
    DeliveryInventoryAllocation,
    InventoryLot,
    InventoryReservation,
    OrderedFinishedReceiptReturn,
    UnorderedFinishedDeliveryAllocation,
)


COST_QUANTUM = Decimal("0.000001")
MONEY_QUANTUM = Decimal("0.01")
RATE_QUANTUM = Decimal("0.0001")
ACTUAL_COST_SOURCE = "purchase_receipt_actual"


@dataclass(frozen=True)
class ResolvedActualMaterialCost:
    purpose_allocation: IncomingReceiptPurposeAllocation
    purchase_fact: PurchaseReceiptFact
    unit_material_cost: Decimal
    production_completion_id: int | None


def _positive_decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not result.is_finite() or result <= 0:
        return None
    return result.quantize(COST_QUANTUM, rounding=ROUND_HALF_UP)


def _month_bounds(month: str) -> tuple[date, date]:
    year_text, month_text = month.split("-", maxsplit=1)
    year = int(year_text)
    month_number = int(month_text)
    if month_number < 1 or month_number > 12:
        raise ValueError("月份必须是 YYYY-MM")
    start = date(year, month_number, 1)
    end = date(
        year + 1 if month_number == 12 else year,
        1 if month_number == 12 else month_number + 1,
        1,
    )
    return start, end


def _is_active_purpose_allocation(
    db: Session, allocation: IncomingReceiptPurposeAllocation | None
) -> bool:
    if (
        allocation is None
        or allocation.status != "posted"
        or allocation.purpose_contract_status_snapshot != "frozen"
        or allocation.purchase_receipt_fact_id is None
    ):
        return False
    reversal_id = db.scalar(
        select(IncomingReceiptPurposeReversal.id)
        .where(
            IncomingReceiptPurposeReversal.incoming_receipt_purpose_allocation_id
            == allocation.id
        )
        .limit(1)
    )
    return reversal_id is None


def _purpose_allocation_for_completion(
    db: Session, completion_id: int
) -> IncomingReceiptPurposeAllocation | None:
    rows = list(
        db.scalars(
            select(IncomingReceiptPurposeAllocation)
            .where(
                IncomingReceiptPurposeAllocation.production_completion_id
                == completion_id
            )
            .order_by(IncomingReceiptPurposeAllocation.id.desc())
        ).all()
    )
    active = [row for row in rows if _is_active_purpose_allocation(db, row)]
    return active[0] if len(active) == 1 else None


def _purpose_allocation_for_lot(
    db: Session,
    lot: InventoryLot,
    *,
    visited_lot_ids: set[int] | None = None,
) -> IncomingReceiptPurposeAllocation | None:
    visited = visited_lot_ids or set()
    if int(lot.id) in visited:
        return None
    visited.add(int(lot.id))

    if lot.source_ref_type == "subkit_receipt" and lot.source_ref_id:
        allocation = db.get(IncomingReceiptPurposeAllocation, lot.source_ref_id)
        if allocation and _is_active_purpose_allocation(db, allocation):
            return allocation

    direct_rows = list(
        db.scalars(
            select(IncomingReceiptPurposeAllocation)
            .where(
                or_(
                    IncomingReceiptPurposeAllocation.finished_inventory_lot_id
                    == lot.id,
                    IncomingReceiptPurposeAllocation.semi_finished_inventory_lot_id
                    == lot.id,
                )
            )
            .order_by(IncomingReceiptPurposeAllocation.id.desc())
        ).all()
    )
    direct_active = [
        row for row in direct_rows if _is_active_purpose_allocation(db, row)
    ]
    if len(direct_active) == 1:
        return direct_active[0]

    if lot.source_ref_type == "production_completion" and lot.source_ref_id:
        allocation = _purpose_allocation_for_completion(db, int(lot.source_ref_id))
        if allocation is not None:
            return allocation

    if lot.source_ref_type == "incoming_receipt_item" and lot.source_ref_id:
        rows = list(
            db.scalars(
                select(IncomingReceiptPurposeAllocation)
                .where(
                    IncomingReceiptPurposeAllocation.incoming_receipt_item_id
                    == int(lot.source_ref_id)
                )
                .order_by(IncomingReceiptPurposeAllocation.id.desc())
            ).all()
        )
        active = [row for row in rows if _is_active_purpose_allocation(db, row)]
        if len(active) == 1:
            return active[0]

    returned = db.scalar(
        select(OrderedFinishedReceiptReturn)
        .where(OrderedFinishedReceiptReturn.return_inventory_lot_id == lot.id)
        .order_by(OrderedFinishedReceiptReturn.id.desc())
        .limit(1)
    )
    if returned is not None:
        source_lot = db.get(InventoryLot, returned.source_inventory_lot_id)
        if source_lot is not None:
            return _purpose_allocation_for_lot(
                db, source_lot, visited_lot_ids=visited
            )
    return None


def resolve_lot_actual_material_cost(
    db: Session, lot: InventoryLot
) -> ResolvedActualMaterialCost | None:
    if getattr(lot, "cost_snapshot_source", None) != ACTUAL_COST_SOURCE:
        return None
    unit_cost = _positive_decimal(lot.estimated_unit_cost_snapshot)
    if unit_cost is None:
        return None
    allocation = _purpose_allocation_for_lot(db, lot)
    if allocation is None:
        return None
    purchase_fact = db.get(PurchaseReceiptFact, allocation.purchase_receipt_fact_id)
    if purchase_fact is None:
        return None

    try:
        detail = json.loads(lot.cost_snapshot_detail_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    detail_fact_id = detail.get("purchase_receipt_fact_id")
    if detail_fact_id is None or int(detail_fact_id) != int(purchase_fact.id):
        return None
    return ResolvedActualMaterialCost(
        purpose_allocation=allocation,
        purchase_fact=purchase_fact,
        unit_material_cost=unit_cost,
        production_completion_id=(
            int(allocation.production_completion_id)
            if allocation.production_completion_id is not None
            else None
        ),
    )


def resolve_completion_actual_material_cost(
    db: Session, completion: ProductionCompletion
) -> ResolvedActualMaterialCost | None:
    allocation = _purpose_allocation_for_completion(db, int(completion.id))
    if allocation is None:
        return None
    purchase_fact = db.get(PurchaseReceiptFact, allocation.purchase_receipt_fact_id)
    capitalized = _positive_decimal(allocation.capitalized_cost)
    finished_quantity = int(allocation.finished_output_qty_delta or 0)
    if purchase_fact is None or capitalized is None or finished_quantity <= 0:
        return None
    unit_cost = (capitalized / Decimal(finished_quantity)).quantize(
        COST_QUANTUM, rounding=ROUND_HALF_UP
    )
    if unit_cost <= 0:
        return None
    return ResolvedActualMaterialCost(
        purpose_allocation=allocation,
        purchase_fact=purchase_fact,
        unit_material_cost=unit_cost,
        production_completion_id=int(completion.id),
    )


def _source_identity(
    *,
    source_kind: str,
    delivery_inventory_allocation_id: int | None = None,
    unordered_finished_delivery_allocation_id: int | None = None,
    bom_component_direct_delivery_allocation_id: int | None = None,
) -> tuple[str, int]:
    values = {
        "inventory_allocation": delivery_inventory_allocation_id,
        "unordered_inventory_allocation": unordered_finished_delivery_allocation_id,
        "bom_direct_completion": bom_component_direct_delivery_allocation_id,
    }
    source_id = values.get(source_kind)
    if source_id is None:
        raise ValueError("材料成本来源标识不完整")
    return source_kind, int(source_id)


def _latest_source_fact(
    db: Session, *, source_kind: str, source_id: int
) -> FinanceDeliveryMaterialCostFact | None:
    column = {
        "inventory_allocation": (
            FinanceDeliveryMaterialCostFact.delivery_inventory_allocation_id
        ),
        "unordered_inventory_allocation": (
            FinanceDeliveryMaterialCostFact.unordered_finished_delivery_allocation_id
        ),
        "bom_direct_completion": (
            FinanceDeliveryMaterialCostFact.bom_component_direct_delivery_allocation_id
        ),
    }[source_kind]
    return db.scalar(
        select(FinanceDeliveryMaterialCostFact)
        .where(column == source_id)
        .order_by(
            FinanceDeliveryMaterialCostFact.snapshot_version.desc(),
            FinanceDeliveryMaterialCostFact.id.desc(),
        )
        .limit(1)
    )


def _freeze_fact(
    db: Session,
    *,
    source_kind: str,
    delivery: Delivery,
    delivery_item: DeliveryItem,
    resolved: ResolvedActualMaterialCost,
    consumed_quantity: int,
    quantity_unit: str,
    operator_id: int | None,
    inventory_lot_id: int | None = None,
    delivery_inventory_allocation_id: int | None = None,
    unordered_finished_delivery_allocation_id: int | None = None,
    bom_component_direct_delivery_allocation_id: int | None = None,
) -> FinanceDeliveryMaterialCostFact:
    source_kind, source_id = _source_identity(
        source_kind=source_kind,
        delivery_inventory_allocation_id=delivery_inventory_allocation_id,
        unordered_finished_delivery_allocation_id=(
            unordered_finished_delivery_allocation_id
        ),
        bom_component_direct_delivery_allocation_id=(
            bom_component_direct_delivery_allocation_id
        ),
    )
    quantity = int(consumed_quantity)
    if quantity <= 0:
        raise ValueError("材料成本冻结数量必须大于0")
    unit = resolved.unit_material_cost.quantize(
        COST_QUANTUM, rounding=ROUND_HALF_UP
    )
    total = (unit * Decimal(quantity)).quantize(
        COST_QUANTUM, rounding=ROUND_HALF_UP
    )
    payload = {
        "source_kind": source_kind,
        "source_id": source_id,
        "delivery_id": int(delivery.id),
        "delivery_item_id": int(delivery_item.id),
        "inventory_lot_id": int(inventory_lot_id) if inventory_lot_id else None,
        "production_completion_id": resolved.production_completion_id,
        "incoming_receipt_purpose_allocation_id": int(
            resolved.purpose_allocation.id
        ),
        "purchase_receipt_fact_id": int(resolved.purchase_fact.id),
        "consumed_quantity": quantity,
        "quantity_unit": str(quantity_unit).strip(),
        "unit_material_cost": format(unit, ".6f"),
        "total_material_cost": format(total, ".6f"),
        "currency": str(resolved.purchase_fact.currency).strip().upper(),
        "tax_included": bool(resolved.purchase_fact.tax_included),
        "tax_rate": format(Decimal(str(resolved.purchase_fact.tax_rate)), ".6f"),
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    latest = _latest_source_fact(
        db, source_kind=source_kind, source_id=source_id
    )
    if latest is not None and latest.source_fingerprint == fingerprint:
        return latest

    fact = FinanceDeliveryMaterialCostFact(
        source_kind=source_kind,
        snapshot_version=(int(latest.snapshot_version) + 1 if latest else 1),
        delivery_id=delivery.id,
        delivery_item_id=delivery_item.id,
        delivery_inventory_allocation_id=delivery_inventory_allocation_id,
        unordered_finished_delivery_allocation_id=(
            unordered_finished_delivery_allocation_id
        ),
        bom_component_direct_delivery_allocation_id=(
            bom_component_direct_delivery_allocation_id
        ),
        inventory_lot_id=inventory_lot_id,
        production_completion_id=resolved.production_completion_id,
        incoming_receipt_purpose_allocation_id=resolved.purpose_allocation.id,
        purchase_receipt_fact_id=resolved.purchase_fact.id,
        consumed_quantity=quantity,
        quantity_unit_snapshot=str(quantity_unit).strip(),
        unit_material_cost=unit,
        total_material_cost=total,
        currency_snapshot=str(resolved.purchase_fact.currency).strip().upper(),
        tax_included_snapshot=bool(resolved.purchase_fact.tax_included),
        tax_rate_snapshot=Decimal(str(resolved.purchase_fact.tax_rate)).quantize(
            COST_QUANTUM, rounding=ROUND_HALF_UP
        ),
        source_fingerprint=fingerprint,
        created_by=operator_id,
    )
    db.add(fact)
    db.flush()
    return fact


def freeze_delivery_inventory_material_cost(
    db: Session,
    *,
    allocation: DeliveryInventoryAllocation,
    lot: InventoryLot,
    operator_id: int | None,
) -> FinanceDeliveryMaterialCostFact | None:
    from app.services.graph_delivery_cost import is_graph_output
    if is_graph_output(lot):
        return _freeze_graph_cost_checked(db, allocation=allocation, lot=lot, operator_id=operator_id)
    resolved = resolve_lot_actual_material_cost(db, lot)
    if resolved is None:
        return None
    delivery_item = db.get(DeliveryItem, allocation.delivery_item_id)
    delivery = db.get(Delivery, delivery_item.delivery_id) if delivery_item else None
    if delivery_item is None or delivery is None:
        return None
    return _freeze_fact(
        db,
        source_kind="inventory_allocation",
        delivery=delivery,
        delivery_item=delivery_item,
        resolved=resolved,
        consumed_quantity=int(allocation.consumed_stock_quantity),
        quantity_unit=lot.unit,
        operator_id=operator_id,
        inventory_lot_id=lot.id,
        delivery_inventory_allocation_id=allocation.id,
    )


def freeze_unordered_delivery_material_cost(
    db: Session,
    *,
    allocation: UnorderedFinishedDeliveryAllocation,
    lot: InventoryLot,
    operator_id: int | None,
) -> FinanceDeliveryMaterialCostFact | None:
    from app.services.graph_delivery_cost import is_graph_output
    if is_graph_output(lot):
        return _freeze_graph_cost_checked(db, allocation=allocation, lot=lot, operator_id=operator_id, unordered=True)
    resolved = resolve_lot_actual_material_cost(db, lot)
    if resolved is None:
        return None
    delivery_item = db.get(DeliveryItem, allocation.delivery_item_id)
    delivery = db.get(Delivery, delivery_item.delivery_id) if delivery_item else None
    if delivery_item is None or delivery is None:
        return None
    return _freeze_fact(
        db,
        source_kind="unordered_inventory_allocation",
        delivery=delivery,
        delivery_item=delivery_item,
        resolved=resolved,
        consumed_quantity=int(allocation.consumed_quantity),
        quantity_unit=lot.unit,
        operator_id=operator_id,
        inventory_lot_id=lot.id,
        unordered_finished_delivery_allocation_id=allocation.id,
    )


def _freeze_graph_cost_checked(db, **kwargs):
    from app.services.graph_delivery_cost import freeze_graph_delivery_cost
    from app.services.bom_subkits import SubkitError
    from app.services.warehouse_inventory import WarehouseInventoryError
    try:
        return freeze_graph_delivery_cost(db, **kwargs)
    except SubkitError as error:
        raise WarehouseInventoryError(str(error), error.status_code) from error


def freeze_bom_direct_delivery_material_cost(
    db: Session,
    *,
    allocation: BomComponentDirectDeliveryAllocation,
    completion: ProductionCompletion,
    operator_id: int | None,
) -> FinanceDeliveryMaterialCostFact | None:
    resolved = resolve_completion_actual_material_cost(db, completion)
    if resolved is None:
        return None
    delivery_item = db.get(DeliveryItem, allocation.delivery_item_id)
    delivery = db.get(Delivery, delivery_item.delivery_id) if delivery_item else None
    if delivery_item is None or delivery is None:
        return None
    return _freeze_fact(
        db,
        source_kind="bom_direct_completion",
        delivery=delivery,
        delivery_item=delivery_item,
        resolved=resolved,
        consumed_quantity=int(allocation.consumed_quantity),
        quantity_unit="pieces",
        operator_id=operator_id,
        bom_component_direct_delivery_allocation_id=allocation.id,
    )


def _latest_facts_by_source(
    db: Session, delivery_item_ids: list[int]
) -> dict[tuple[str, int], FinanceDeliveryMaterialCostFact]:
    if not delivery_item_ids:
        return {}
    rows = list(
        db.scalars(
            select(FinanceDeliveryMaterialCostFact)
            .where(
                FinanceDeliveryMaterialCostFact.delivery_item_id.in_(
                    delivery_item_ids
                )
            )
            .order_by(
                FinanceDeliveryMaterialCostFact.source_kind,
                FinanceDeliveryMaterialCostFact.snapshot_version,
                FinanceDeliveryMaterialCostFact.id,
            )
        ).all()
    )
    result: dict[tuple[str, int], FinanceDeliveryMaterialCostFact] = {}
    for row in rows:
        source_id = {
            "inventory_allocation": row.delivery_inventory_allocation_id,
            "unordered_inventory_allocation": (
                row.unordered_finished_delivery_allocation_id
            ),
            "bom_direct_completion": (
                row.bom_component_direct_delivery_allocation_id
            ),
        }.get(row.source_kind)
        if source_id is not None:
            result[(row.source_kind, int(source_id))] = row
    return result


def material_cost_coverage_report(db: Session, *, month: str) -> dict[str, Any]:
    """Return a small, actionable month-close quality report.

    Delivery date is the physical material-cost period.  Revenue remains on
    the confirmed-statement month elsewhere in the finance workbook; the two
    bases are intentionally labelled instead of silently forced to match.
    """

    month_start, next_month_start = _month_bounds(month)
    delivery_rows = list(
        db.execute(
            select(
                DeliveryItem,
                Delivery.delivery_number,
                Delivery.delivery_date,
                Customer.name,
            )
            .join(Delivery, Delivery.id == DeliveryItem.delivery_id)
            .join(Customer, Customer.id == Delivery.customer_id)
            .where(
                Delivery.status == "dispatched",
                DeliveryItem.is_current.is_(True),
                Delivery.delivery_date >= month_start,
                Delivery.delivery_date < next_month_start,
            )
            .order_by(Delivery.delivery_date, Delivery.id, DeliveryItem.id)
        ).all()
    )
    delivery_item_ids = [int(row[0].id) for row in delivery_rows]
    row_meta = {
        int(item.id): {
            "delivery_item_id": int(item.id),
            "delivery_number": delivery_number,
            "delivery_date": delivery_date,
            "customer_name": customer_name,
            "delivered_quantity": int(item.delivered_quantity or 0),
            "source_type": item.source_type,
        }
        for item, delivery_number, delivery_date, customer_name in delivery_rows
    }
    sources_by_item: dict[int, list[dict[str, Any]]] = {
        item_id: [] for item_id in delivery_item_ids
    }

    if delivery_item_ids:
        inventory_rows = list(
            db.execute(
                select(
                    DeliveryInventoryAllocation,
                    InventoryLot,
                )
                .join(
                    InventoryReservation,
                    InventoryReservation.id
                    == DeliveryInventoryAllocation.reservation_id,
                )
                .join(
                    InventoryLot,
                    InventoryLot.id == InventoryReservation.inventory_lot_id,
                )
                .where(
                    DeliveryInventoryAllocation.delivery_item_id.in_(
                        delivery_item_ids
                    )
                )
                .order_by(DeliveryInventoryAllocation.id)
            ).all()
        )
        for allocation, lot in inventory_rows:
            active_quantity = max(
                int(allocation.consumed_stock_quantity or 0)
                - int(allocation.reversed_stock_quantity or 0),
                0,
            )
            if active_quantity:
                sources_by_item[int(allocation.delivery_item_id)].append(
                    {
                        "kind": "inventory_allocation",
                        "id": int(allocation.id),
                        "active_quantity": active_quantity,
                        "lot": lot,
                    }
                )

        unordered_rows = list(
            db.execute(
                select(UnorderedFinishedDeliveryAllocation, InventoryLot)
                .join(
                    InventoryLot,
                    InventoryLot.id
                    == UnorderedFinishedDeliveryAllocation.inventory_lot_id,
                )
                .where(
                    UnorderedFinishedDeliveryAllocation.delivery_item_id.in_(
                        delivery_item_ids
                    )
                )
                .order_by(UnorderedFinishedDeliveryAllocation.id)
            ).all()
        )
        for allocation, lot in unordered_rows:
            active_quantity = max(
                int(allocation.consumed_quantity or 0)
                - int(allocation.restored_quantity or 0),
                0,
            )
            if active_quantity:
                sources_by_item[int(allocation.delivery_item_id)].append(
                    {
                        "kind": "unordered_inventory_allocation",
                        "id": int(allocation.id),
                        "active_quantity": active_quantity,
                        "lot": lot,
                    }
                )

        direct_rows = list(
            db.execute(
                select(BomComponentDirectDeliveryAllocation, ProductionCompletion)
                .join(
                    ProductionCompletion,
                    ProductionCompletion.id
                    == BomComponentDirectDeliveryAllocation.production_completion_id,
                )
                .where(
                    BomComponentDirectDeliveryAllocation.delivery_item_id.in_(
                        delivery_item_ids
                    )
                )
                .order_by(BomComponentDirectDeliveryAllocation.id)
            ).all()
        )
        for allocation, completion in direct_rows:
            active_quantity = max(
                int(allocation.consumed_quantity or 0)
                - int(allocation.reversed_quantity or 0),
                0,
            )
            if active_quantity:
                sources_by_item[int(allocation.delivery_item_id)].append(
                    {
                        "kind": "bom_direct_completion",
                        "id": int(allocation.id),
                        "active_quantity": active_quantity,
                        "completion": completion,
                    }
                )

    from app.models.bom_subkit import SubkitDeliveryAllocation
    if delivery_item_ids:
        for allocation in db.scalars(select(SubkitDeliveryAllocation).where(
            SubkitDeliveryAllocation.delivery_item_id.in_(delivery_item_ids),
            SubkitDeliveryAllocation.reversed.is_(False))):
            sources_by_item[allocation.delivery_item_id].append({
                "kind": "subkit", "id": allocation.id, "active_quantity": allocation.quantity,
                "frozen_cost": allocation.total_cost,
                "cost_detail": json.loads(allocation.cost_detail_json or "{}")})
    facts = _latest_facts_by_source(db, delivery_item_ids)
    frozen_sources = 0
    eligible_unfrozen_sources = 0
    estimate_only_sources = 0
    missing_sources = 0
    foreign_currency_sources = 0
    actual_material_cost = Decimal("0")
    currency_totals: dict[str, Decimal] = {}
    covered_lines = 0
    partial_lines = 0
    missing_details: list[dict[str, Any]] = []

    from app.services.graph_delivery_cost import graph_cost_report_sources, active_graph_cost
    graph_facts = graph_cost_report_sources(db, delivery_item_ids)
    for item_id in delivery_item_ids:
        sources = sources_by_item[item_id]
        line_frozen = 0
        line_reasons: set[str] = set()
        line_cost = Decimal("0")
        if not sources:
            line_reasons.add("no_delivery_cost_source")
        for source in sources:
            if source["kind"] == "subkit":
                detail = source["cost_detail"]
                if not detail.get("actual"):
                    estimate_only_sources += 1
                    line_reasons.add("estimate_only")
                    continue
                frozen_sources += 1
                currency = str(detail.get("currency") or "").strip().upper()
                currency_totals[currency] = currency_totals.get(currency, Decimal(0)) + source["frozen_cost"]
                if currency == "CNY":
                    line_frozen += 1
                    line_cost += source["frozen_cost"]
                else:
                    foreign_currency_sources += 1
                    line_reasons.add("foreign_currency_rate_missing")
                continue
            key = (str(source["kind"]), int(source["id"]))
            graph_evidence = graph_facts.get(key)
            if graph_evidence is not None:
                graph_fact, graph_portions = graph_evidence
                graph_amount = active_graph_cost(graph_fact, graph_portions, int(source["active_quantity"]))
                frozen_sources += 1
                currency = graph_fact.currency
                currency_totals[currency] = currency_totals.get(currency, Decimal(0)) + graph_amount
                if currency == "CNY":
                    line_frozen += 1
                    line_cost += graph_amount
                else:
                    foreign_currency_sources += 1
                    line_reasons.add("foreign_currency_rate_missing")
                continue
            fact = facts.get(key)
            if fact is not None:
                frozen_sources += 1
                source_cost = Decimal(str(fact.unit_material_cost)) * Decimal(
                    int(source["active_quantity"])
                )
                currency = str(fact.currency_snapshot or "").strip().upper()
                currency_totals[currency] = (
                    currency_totals.get(currency, Decimal("0")) + source_cost
                )
                if currency == "CNY":
                    line_frozen += 1
                    line_cost += source_cost
                else:
                    foreign_currency_sources += 1
                    line_reasons.add("foreign_currency_rate_missing")
                continue
            resolved = None
            lot = source.get("lot")
            if lot is not None:
                resolved = resolve_lot_actual_material_cost(db, lot)
            else:
                resolved = resolve_completion_actual_material_cost(
                    db, source["completion"]
                )
            if resolved is not None:
                eligible_unfrozen_sources += 1
                line_reasons.add("actual_cost_not_frozen")
            elif lot is not None and (
                lot.estimated_unit_cost_snapshot is not None
                or lot.cost_snapshot_source is not None
            ):
                estimate_only_sources += 1
                line_reasons.add("estimate_only")
            else:
                missing_sources += 1
                line_reasons.add("missing_purchase_lineage")

        if sources and line_frozen == len(sources):
            covered_lines += 1
        elif line_frozen:
            partial_lines += 1
        actual_material_cost += line_cost
        if line_reasons and len(missing_details) < 20:
            meta = row_meta[item_id]
            missing_details.append(
                {
                    **meta,
                    "reason_codes": sorted(line_reasons),
                    "reason": "；".join(
                        {
                            "no_delivery_cost_source": "送货没有库存/完工成本来源",
                            "actual_cost_not_frozen": "可追溯实际成本尚未冻结",
                            "estimate_only": "当前只有估算成本",
                            "foreign_currency_rate_missing": "外币成本缺少人工确认汇率",
                            "missing_purchase_lineage": "缺少最终采购价或收料用途链",
                        }[code]
                        for code in sorted(line_reasons)
                    ),
                }
            )

    total_lines = len(delivery_item_ids)
    uncovered_lines = total_lines - covered_lines
    coverage_rate = (
        Decimal(covered_lines) / Decimal(total_lines)
        if total_lines
        else Decimal("1")
    )
    return {
        "month": month,
        "accounting_basis": "按送货日期统计物理出库；人民币金额按收料时冻结的含税采购成本",
        "scope_note": "外币没有人工确认汇率时只显示原币金额并阻止结转；工资、能耗、外协和期间费用继续在成本费用池单独复核。",
        "total_delivery_lines": total_lines,
        "total_delivery_quantity": sum(
            int(meta["delivered_quantity"]) for meta in row_meta.values()
        ),
        "covered_delivery_lines": covered_lines,
        "partial_delivery_lines": partial_lines,
        "uncovered_delivery_lines": uncovered_lines,
        "line_coverage_rate": coverage_rate.quantize(
            RATE_QUANTUM, rounding=ROUND_HALF_UP
        ),
        "frozen_source_count": frozen_sources,
        "eligible_unfrozen_source_count": eligible_unfrozen_sources,
        "estimate_only_source_count": estimate_only_sources,
        "missing_source_count": missing_sources,
        "foreign_currency_source_count": foreign_currency_sources,
        "untraced_delivery_line_count": sum(
            1 for item_id in delivery_item_ids if not sources_by_item[item_id]
        ),
        "actual_material_cost": actual_material_cost.quantize(
            MONEY_QUANTUM, rounding=ROUND_HALF_UP
        ),
        "currency_totals": [
            {
                "currency": currency,
                "amount": amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP),
            }
            for currency, amount in sorted(currency_totals.items())
        ],
        "lineage_ready": uncovered_lines == 0,
        "missing_details": missing_details,
    }
