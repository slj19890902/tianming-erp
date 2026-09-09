from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
import hashlib
import json
from typing import Any, Mapping, Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.order import OrderItem
from app.models.order_estimated_cost_snapshot import SalesOrderItemEstimatedCostSnapshot
from app.models.order_material_cost_snapshot import SalesOrderItemMaterialCostSnapshot
from app.services.order_material_cost_snapshot import get_latest_order_item_material_cost_snapshot
from app.services.processing_cost import estimate_order_item_processing_cost


RULE_VERSION = "p1-131-estimated-v2"
PRECISION_VERSION = "p1-28c1-decimal-v1"
MONEY = Decimal("0.01")
UNIT = Decimal("0.000001")
ALLOWED_LOSS_RATES = {Decimal("0.03"), Decimal("0.05")}
ONE_TIME_FEE_KEYS = ("die_fee", "plate_fee", "freight_fee", "other_fee")
COST_HEALTH_VERSION = "p1-28c2-health-v1"
VERY_LOW_MARGIN_RATE = Decimal("0.15")
REVIEW_MARGIN_RATE = Decimal("0.25")

ESTIMATED_COST_SNAPSHOT_UNIQUE_CONSTRAINTS = frozenset(
    {
        "uq_order_item_estimated_cost_snapshot_version",
        "uq_order_item_estimated_cost_snapshot_fingerprint",
    }
)
_SQLITE_ESTIMATED_COST_UNIQUE_SIGNATURES = (
    "unique constraint failed: "
    "sales_order_item_estimated_cost_snapshots.order_item_reference_snapshot, "
    "sales_order_item_estimated_cost_snapshots.snapshot_version",
    "unique constraint failed: "
    "sales_order_item_estimated_cost_snapshots.order_item_reference_snapshot, "
    "sales_order_item_estimated_cost_snapshots.source_fingerprint",
)


def is_estimated_cost_snapshot_unique_conflict(error: IntegrityError) -> bool:
    """Return true only for the two intentional snapshot idempotency races."""

    original = getattr(error, "orig", None)
    diagnostic = getattr(original, "diag", None)
    constraint_name = getattr(diagnostic, "constraint_name", None)
    if constraint_name in ESTIMATED_COST_SNAPSHOT_UNIQUE_CONSTRAINTS:
        return True

    message = " ".join(str(original or "").lower().split())
    if any(signature in message for signature in _SQLITE_ESTIMATED_COST_UNIQUE_SIGNATURES):
        return True
    return any(
        name.lower() in message
        for name in ESTIMATED_COST_SNAPSHOT_UNIQUE_CONSTRAINTS
    )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _money(value: object) -> Decimal:
    result = Decimal(str(value or 0))
    if not result.is_finite() or result < 0:
        raise ValueError("费用必须为不小于 0 的有效金额")
    return result.quantize(MONEY, rounding=ROUND_HALF_UP)


def _item_reference(item: OrderItem) -> str:
    return (item.item_order_number or "").strip() or f"order-{int(item.order_id)}-item-{int(item.id)}"


def _parameters(latest: SalesOrderItemEstimatedCostSnapshot | None, values: Mapping[str, object] | None) -> dict[str, Decimal]:
    if values is None and latest is not None:
        old = json.loads(latest.breakdown_json or "{}")
        values = {
            "loss_rate": latest.loss_rate,
            **{key: old.get("one_time_fees", {}).get(key, 0) for key in ONE_TIME_FEE_KEYS},
        }
    values = values or {}
    loss_rate = Decimal(str(values.get("loss_rate", "0.03")))
    if loss_rate not in ALLOWED_LOSS_RATES:
        raise ValueError("生产加报损耗只能选择 3% 或 5%")
    return {
        # SQLite Numeric reloads 0.03 as 0.030000. Canonicalize the input
        # before fingerprinting so an unchanged estimate stays idempotent.
        "loss_rate": loss_rate.normalize(),
        **{key: _money(values.get(key, 0)) for key in ONE_TIME_FEE_KEYS},
    }


def _loss_cost(material: SalesOrderItemMaterialCostSnapshot | None, rate: Decimal) -> tuple[Decimal, list[dict[str, Any]]]:
    rows = json.loads(material.components_json or "[]") if material is not None else []
    total = Decimal("0")
    result: list[dict[str, Any]] = []
    for row in rows:
        purchase = max(int(row.get("purchase_sheet_quantity") or 0), 0)
        spare = max(int(row.get("spare_sheet_quantity") or 0), 0)
        theoretical = max(purchase - spare, 0)
        with_loss = int((Decimal(theoretical) * (Decimal("1") + rate)).to_integral_value(rounding=ROUND_CEILING)) + spare
        added = max(with_loss - purchase, 0)
        area = Decimal(str(row.get("area_per_sheet_m2") or 0))
        price = Decimal(str(row.get("effective_square_price") or 0))
        cost = (area * price * Decimal(added)).quantize(MONEY, rounding=ROUND_HALF_UP)
        total += cost
        result.append(
            {
                "label": row.get("label"),
                "theoretical_purchase_sheet_quantity": theoretical,
                "existing_purchase_sheet_quantity": purchase,
                "spare_sheet_quantity": spare,
                "loss_inclusive_purchase_sheet_quantity": with_loss,
                "added_loss_sheet_quantity": added,
                "added_loss_material_cost": str(cost),
            }
        )
    return total.quantize(MONEY, rounding=ROUND_HALF_UP), result


def freeze_order_item_estimated_cost(
    db: Session,
    item: OrderItem,
    *,
    material_snapshot: SalesOrderItemMaterialCostSnapshot | None = None,
    actor_id: int | None = None,
    parameters: Mapping[str, object] | None = None,
) -> tuple[SalesOrderItemEstimatedCostSnapshot, bool]:
    """Append an immutable internal estimate or return its identical version."""

    material = material_snapshot or get_latest_order_item_material_cost_snapshot(db, item)
    reference = _item_reference(item)
    latest = db.scalar(
        select(SalesOrderItemEstimatedCostSnapshot)
        .where(SalesOrderItemEstimatedCostSnapshot.order_item_reference_snapshot == reference)
        .order_by(SalesOrderItemEstimatedCostSnapshot.snapshot_version.desc())
        .limit(1)
    )
    params = _parameters(latest, parameters)
    quantity = max(int(item.quantity or 0), 1)
    standard_processing = estimate_order_item_processing_cost(db, item)
    rule_version = ("multilevel-bom-estimated-v1"
                    if standard_processing.get("rule_version") == "multilevel-bom-processing-v1"
                    else RULE_VERSION)
    printing = standard_processing["printing"]
    die_cut = standard_processing["die_cut"]
    joining = standard_processing["joining"]
    extra_assembly = standard_processing["extra_assembly"]
    active_modes = {
        printing["printer_mode"],
        die_cut["die_cut_mode"],
        joining["joining_mode"],
        extra_assembly["assembly_mode"],
    }
    if active_modes == {"none"}:
        category = (
            "external_purchase"
            if getattr(item, "supply_mode_snapshot", None) == "external_purchase"
            else "none"
        )
    else:
        category = "standard_labor"
    batch_cost = Decimal("0")
    extra_color_unit = Decimal("0")
    processing_cost_value = standard_processing["estimated_processing_cost"]
    processing_total = (
        Decimal(str(processing_cost_value)).quantize(MONEY, rounding=ROUND_HALF_UP)
        if processing_cost_value is not None
        else None
    )
    unit_cost = (
        (processing_total / Decimal(quantity)).quantize(UNIT, rounding=ROUND_HALF_UP)
        if processing_cost_value is not None
        else None
    )
    color_count = printing["color_count"]
    missing = list(standard_processing["missing_items"])
    loss_total, loss_rows = _loss_cost(material, params["loss_rate"])
    material_known = Decimal(str(material.known_material_subtotal or 0)) if material else Decimal("0")
    material_status = material.calculation_status if material else "missing"
    if material_status != "calculated":
        missing.extend(json.loads(material.missing_items_json or "[]") if material else ["材料成本快照缺失"])
    missing = list(dict.fromkeys(str(value) for value in missing if str(value).strip()))
    one_time = sum((params[key] for key in ONE_TIME_FEE_KEYS), Decimal("0")).quantize(MONEY)
    known_processing = (processing_total if processing_total is not None else
                        Decimal(str(standard_processing.get("known_processing_subtotal") or 0)))
    known = (material_known + loss_total + known_processing + one_time).quantize(MONEY, rounding=ROUND_HALF_UP)
    complete = (
        material_status == "calculated"
        and standard_processing["calculation_status"] == "calculated"
        and not missing
    )
    status = "calculated" if complete else "partial" if known > 0 else "missing"
    total = known if complete else None
    per_unit = (total / Decimal(quantity)).quantize(UNIT, rounding=ROUND_HALF_UP) if total is not None else None
    breakdown = {
        "scope_label": "预计成本，非实际成本",
        "material_cost": str(material_known.quantize(MONEY)),
        "loss_rate": str(params["loss_rate"]),
        "loss_components": loss_rows,
        "loss_material_total_cost": str(loss_total),
        "processing_category": category,
        "processing_batch_cost": str(batch_cost.quantize(MONEY)),
        "processing_unit_cost": (
            str(unit_cost.quantize(UNIT)) if unit_cost is not None else None
        ),
        "printing_color_count": color_count,
        "extra_color_unit_cost": str(extra_color_unit.quantize(UNIT)),
        "processing_total_cost": (
            str(processing_total) if processing_total is not None else None
        ),
        "standard_processing": standard_processing,
        "one_time_fees": {key: str(params[key]) for key in ONE_TIME_FEE_KEYS},
        "one_time_fee_total": str(one_time),
        "tax_rate_reference": "0.13",
        "tax_basis_label": "沿用材料价原口径（未重复加税）",
    }
    payload = {
        "material_cost_snapshot_id": material.id if material else None,
        "material_cost_snapshot_version": material.snapshot_version if material else None,
        "calculation_status": status,
        "order_quantity_snapshot": quantity,
        "loss_rate": str(params["loss_rate"]),
        "processing_category": category,
        "printing_color_count": color_count,
        "loss_material_total_cost": str(loss_total),
        "processing_total_cost": (
            str(processing_total) if processing_total is not None else None
        ),
        "one_time_fee_total": str(one_time),
        "known_estimated_subtotal": str(known),
        "estimated_unit_total_cost": str(per_unit) if per_unit is not None else None,
        "estimated_order_total_cost": str(total) if total is not None else None,
        "breakdown": breakdown,
        "missing_items": missing,
        "rule_version": rule_version,
        "precision_version": PRECISION_VERSION,
    }
    fingerprint = hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()
    existing = db.scalar(
        select(SalesOrderItemEstimatedCostSnapshot).where(
            SalesOrderItemEstimatedCostSnapshot.order_item_reference_snapshot == reference,
            SalesOrderItemEstimatedCostSnapshot.source_fingerprint == fingerprint,
        )
    )
    if existing is not None:
        return existing, False
    next_version = int(
        db.scalar(
            select(func.max(SalesOrderItemEstimatedCostSnapshot.snapshot_version)).where(
                SalesOrderItemEstimatedCostSnapshot.order_item_reference_snapshot == reference
            )
        )
        or 0
    ) + 1
    snapshot = SalesOrderItemEstimatedCostSnapshot(
        sales_order_item_id=item.id,
        order_item_reference_snapshot=reference,
        snapshot_version=next_version,
        source_fingerprint=fingerprint,
        material_cost_snapshot_id=payload["material_cost_snapshot_id"],
        material_cost_snapshot_version=payload["material_cost_snapshot_version"],
        calculation_status=status,
        scope_code="estimated_total",
        rule_version=rule_version,
        precision_version=PRECISION_VERSION,
        order_quantity_snapshot=quantity,
        loss_rate=params["loss_rate"],
        processing_category=category,
        printing_color_count=color_count,
        processing_batch_cost=batch_cost,
        processing_unit_cost=unit_cost,
        extra_color_unit_cost=extra_color_unit,
        loss_material_total_cost=loss_total,
        processing_total_cost=processing_total,
        one_time_fee_total=one_time,
        known_estimated_subtotal=known,
        estimated_unit_total_cost=per_unit,
        estimated_order_total_cost=total,
        tax_rate_reference=Decimal("0.13"),
        tax_basis_code="material_source_as_stored",
        breakdown_json=_json(breakdown),
        missing_items_json=_json(missing),
        created_by=actor_id,
    )
    db.add(snapshot)
    db.flush()
    return snapshot, True


def get_latest_order_item_estimated_cost_snapshots_by_items(
    db: Session, items: Sequence[OrderItem]
) -> dict[int, SalesOrderItemEstimatedCostSnapshot]:
    references = {int(item.id): _item_reference(item) for item in items}
    if not references:
        return {}
    result: dict[int, SalesOrderItemEstimatedCostSnapshot] = {}
    rows = db.scalars(
        select(SalesOrderItemEstimatedCostSnapshot)
        .where(SalesOrderItemEstimatedCostSnapshot.sales_order_item_id.in_(references))
        .order_by(SalesOrderItemEstimatedCostSnapshot.sales_order_item_id, SalesOrderItemEstimatedCostSnapshot.snapshot_version)
    ).all()
    for row in rows:
        item_id = int(row.sales_order_item_id)
        if row.order_item_reference_snapshot == references.get(item_id):
            result[item_id] = row
    return result


def get_latest_order_item_estimated_cost_snapshot(
    db: Session, item: OrderItem
) -> SalesOrderItemEstimatedCostSnapshot | None:
    return db.scalar(
        select(SalesOrderItemEstimatedCostSnapshot)
        .where(
            SalesOrderItemEstimatedCostSnapshot.order_item_reference_snapshot
            == _item_reference(item)
        )
        .order_by(SalesOrderItemEstimatedCostSnapshot.snapshot_version.desc())
        .limit(1)
    )


def classify_estimated_cost_health(
    snapshot: SalesOrderItemEstimatedCostSnapshot,
    sale_amount: object,
) -> dict[str, Any]:
    """Return an advisory-only health label using Decimal comparisons."""

    common = {
        "estimated_cost_health_version": COST_HEALTH_VERSION,
        "estimated_cost_health_basis_label": "按订单录入售价试算，预计而非实际利润",
        "estimated_cost_health_blocks_save": False,
    }
    if (
        snapshot.calculation_status != "calculated"
        or snapshot.estimated_order_total_cost is None
    ):
        return {
            **common,
            "estimated_cost_health_code": "cost_incomplete",
            "estimated_cost_health_label": "成本资料待完善",
            "estimated_cost_health_tone": "orange",
            "estimated_gross_profit": None,
            "estimated_margin_rate": None,
        }
    sale = Decimal(str(sale_amount or 0))
    if not sale.is_finite() or sale <= 0:
        return {
            **common,
            "estimated_cost_health_code": "sale_missing",
            "estimated_cost_health_label": "售价待完善",
            "estimated_cost_health_tone": "orange",
            "estimated_gross_profit": None,
            "estimated_margin_rate": None,
        }
    cost = Decimal(str(snapshot.estimated_order_total_cost))
    profit = (sale - cost).quantize(MONEY, rounding=ROUND_HALF_UP)
    margin = (profit / sale).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    if profit < 0:
        code, label, tone = "estimated_loss", "预计亏损", "red"
    elif margin < VERY_LOW_MARGIN_RATE:
        code, label, tone = "very_low", "利润空间很低", "red"
    elif margin < REVIEW_MARGIN_RATE:
        code, label, tone = "review", "建议复核", "orange"
    else:
        code, label, tone = "healthy", "预计正常", "green"
    return {
        **common,
        "estimated_cost_health_code": code,
        "estimated_cost_health_label": label,
        "estimated_cost_health_tone": tone,
        "estimated_gross_profit": str(profit),
        "estimated_margin_rate": str(margin),
    }


def serialize_order_item_estimated_cost_snapshot(
    snapshot: SalesOrderItemEstimatedCostSnapshot,
    *,
    sale_amount: object | None = None,
) -> dict[str, Any]:
    total = str(snapshot.estimated_order_total_cost) if snapshot.estimated_order_total_cost is not None else None
    unit = str(snapshot.estimated_unit_total_cost) if snapshot.estimated_unit_total_cost is not None else None
    data = {
        "estimated_total_cost_status": snapshot.calculation_status,
        "estimated_total_cost_status_label": {
            "calculated": "预计总成本已冻结",
            "partial": "预计总成本部分待完善",
            "missing": "预计总成本待完善",
        }[snapshot.calculation_status],
        "estimated_total_cost_scope_label": "预计成本，非实际成本",
        "estimated_total_cost_snapshot_version": snapshot.snapshot_version,
        "estimated_total_cost_rule_version": snapshot.rule_version,
        "estimated_total_cost_order_quantity_snapshot": snapshot.order_quantity_snapshot,
        "estimated_loss_rate": str(snapshot.loss_rate),
        "estimated_processing_category": snapshot.processing_category,
        "estimated_cost_breakdown": json.loads(snapshot.breakdown_json or "{}"),
        "estimated_cost_missing_items": json.loads(snapshot.missing_items_json or "[]"),
        "known_estimated_subtotal": str(snapshot.known_estimated_subtotal),
        "estimated_order_total_cost": total,
        "estimated_unit_total_cost": unit,
        "cost_status": "calculated" if snapshot.calculation_status == "calculated" else "pending",
        "estimated_cost": unit,
    }
    if sale_amount is not None:
        data.update(classify_estimated_cost_health(snapshot, sale_amount))
    return data
