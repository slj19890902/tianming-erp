from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.order import OrderItem
from app.models.order_material_cost_snapshot import (
    SalesOrderItemMaterialCostSnapshot,
)
from app.services.order_material_cost import estimate_order_item_material_cost


PRECISION_VERSION = "p1-28b-decimal-v1"
FROZEN_SCOPE_LABEL = "下单时材料成本（未计生产损耗和加工费）"
CURRENT_ESTIMATE_HISTORY_LABEL = "当前规则估算，非历史成本事实"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decimal(value: object) -> Decimal | None:
    return Decimal(str(value)) if value not in (None, "") else None


def _effective_bom_components(db: Session, item_id: int) -> list[dict[str, Any]]:
    from app.services.composite_bom import get_order_item_bom_components_by_item_ids

    return get_order_item_bom_components_by_item_ids(db, [item_id]).get(item_id, [])


def _item_reference(item: OrderItem) -> str:
    return (item.item_order_number or "").strip() or (
        f"order-{int(item.order_id)}-item-{int(item.id)}"
    )


def freeze_order_item_material_cost(
    db: Session,
    item: OrderItem,
    *,
    actor_id: int | None = None,
    bom_components: Iterable[Mapping[str, Any]] | None = None,
) -> tuple[SalesOrderItemMaterialCostSnapshot, bool]:
    """Append one immutable snapshot, or return the identical prior version."""

    components = (
        list(bom_components)
        if bom_components is not None
        else _effective_bom_components(db, int(item.id))
    )
    estimate = estimate_order_item_material_cost(
        db,
        item,
        bom_components=components,
    )
    payload = {
        "order_quantity_snapshot": int(item.quantity),
        "calculation_status": estimate["material_cost_status"],
        "formula_version": estimate["material_cost_formula_version"],
        "precision_version": PRECISION_VERSION,
        "estimated_material_unit_cost": estimate["estimated_material_unit_cost"],
        "estimated_material_total_cost": estimate["estimated_material_total_cost"],
        "known_material_subtotal": estimate["known_material_subtotal"],
        "components": estimate["material_cost_components"],
        "missing_items": estimate["material_cost_missing_items"],
    }
    fingerprint = hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()
    item_reference = _item_reference(item)
    existing = db.scalar(
        select(SalesOrderItemMaterialCostSnapshot).where(
            SalesOrderItemMaterialCostSnapshot.order_item_reference_snapshot
            == item_reference,
            SalesOrderItemMaterialCostSnapshot.source_fingerprint == fingerprint,
        )
    )
    if existing is not None:
        return existing, False
    next_version = int(
        db.scalar(
            select(func.max(SalesOrderItemMaterialCostSnapshot.snapshot_version)).where(
                SalesOrderItemMaterialCostSnapshot.order_item_reference_snapshot
                == item_reference
            )
        )
        or 0
    ) + 1
    snapshot = SalesOrderItemMaterialCostSnapshot(
        sales_order_item_id=item.id,
        order_item_reference_snapshot=item_reference,
        snapshot_version=next_version,
        source_fingerprint=fingerprint,
        calculation_status=payload["calculation_status"],
        scope_code="material_only",
        formula_version=payload["formula_version"],
        precision_version=PRECISION_VERSION,
        order_quantity_snapshot=payload["order_quantity_snapshot"],
        estimated_material_unit_cost=_decimal(payload["estimated_material_unit_cost"]),
        estimated_material_total_cost=_decimal(payload["estimated_material_total_cost"]),
        known_material_subtotal=_decimal(payload["known_material_subtotal"]),
        components_json=_json(payload["components"]),
        missing_items_json=_json(payload["missing_items"]),
        created_by=actor_id,
    )
    db.add(snapshot)
    db.flush()
    return snapshot, True


def get_latest_order_item_material_cost_snapshot(
    db: Session, item: OrderItem
) -> SalesOrderItemMaterialCostSnapshot | None:
    item_reference = _item_reference(item)
    return db.scalar(
        select(SalesOrderItemMaterialCostSnapshot)
        .where(
            SalesOrderItemMaterialCostSnapshot.order_item_reference_snapshot
            == item_reference
        )
        .order_by(SalesOrderItemMaterialCostSnapshot.snapshot_version.desc())
        .limit(1)
    )


def get_latest_order_item_material_cost_snapshots_by_items(
    db: Session,
    items: Sequence[OrderItem],
) -> dict[int, SalesOrderItemMaterialCostSnapshot]:
    """Load latest matching versions for one order without per-line queries."""

    references = {int(item.id): _item_reference(item) for item in items}
    if not references:
        return {}
    result: dict[int, SalesOrderItemMaterialCostSnapshot] = {}
    rows = db.scalars(
        select(SalesOrderItemMaterialCostSnapshot)
        .where(
            SalesOrderItemMaterialCostSnapshot.sales_order_item_id.in_(references)
        )
        .order_by(
            SalesOrderItemMaterialCostSnapshot.sales_order_item_id,
            SalesOrderItemMaterialCostSnapshot.snapshot_version,
        )
    ).all()
    for row in rows:
        item_id = int(row.sales_order_item_id)
        if row.order_item_reference_snapshot == references.get(item_id):
            result[item_id] = row
    return result


def serialize_order_item_material_cost_snapshot(
    snapshot: SalesOrderItemMaterialCostSnapshot,
) -> dict[str, Any]:
    status_label = {
        "calculated": "材料成本已冻结",
        "partial": "部分材料成本已冻结",
        "missing": "材料成本资料待完善",
    }[snapshot.calculation_status]
    components = json.loads(snapshot.components_json or "[]")
    missing = json.loads(snapshot.missing_items_json or "[]")
    unit_cost = (
        str(snapshot.estimated_material_unit_cost)
        if snapshot.estimated_material_unit_cost is not None
        else None
    )
    total_cost = (
        str(snapshot.estimated_material_total_cost)
        if snapshot.estimated_material_total_cost is not None
        else None
    )
    return {
        "material_cost_status": snapshot.calculation_status,
        "material_cost_status_label": status_label,
        "material_cost_scope_label": FROZEN_SCOPE_LABEL,
        "material_cost_history_label": f"历史冻结版本 V{snapshot.snapshot_version}",
        "material_cost_is_current_estimate": False,
        "material_cost_snapshot_version": snapshot.snapshot_version,
        "material_cost_snapshot_calculated_at": snapshot.calculated_at,
        "material_cost_formula_version": snapshot.formula_version,
        "material_cost_precision_version": snapshot.precision_version,
        "material_cost_order_quantity_snapshot": snapshot.order_quantity_snapshot,
        "estimated_material_unit_cost": unit_cost,
        "estimated_material_total_cost": total_cost,
        "known_material_subtotal": (
            str(snapshot.known_material_subtotal)
            if snapshot.known_material_subtotal is not None
            else None
        ),
        "material_cost_components": components,
        "material_cost_missing_items": missing,
        "cost_status": (
            "calculated" if snapshot.calculation_status == "calculated" else "pending"
        ),
        "estimated_cost": unit_cost,
    }


def mark_current_estimate_as_non_historical(data: dict[str, Any]) -> dict[str, Any]:
    return {
        **data,
        "material_cost_history_label": CURRENT_ESTIMATE_HISTORY_LABEL,
    }
