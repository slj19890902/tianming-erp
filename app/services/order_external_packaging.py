from __future__ import annotations

import json
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.external_packaging_component import (
    ProductExternalComponent,
    ProductExternalComponentSet,
)
from app.models.order import OrderItem
from app.models.order_external_packaging import (
    SalesOrderItemExternalComponent,
    SalesOrderItemExternalComponentCandidate,
)
from app.services.supplier_master import SUPPLIER_CATEGORY_LABELS


DISCRETE_PURCHASE_UNITS = frozenset(
    {"根", "件", "张", "令", "只", "个", "套", "片", "卷", "箱"}
)
CONTINUOUS_PURCHASE_UNITS = frozenset({"米", "kg", "吨"})
SIX_PLACES = Decimal("0.000001")
THREE_PLACES = Decimal("0.001")


class OrderExternalPackagingSnapshotError(ValueError):
    pass


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _calculated_quantities(
    component: SalesOrderItemExternalComponent,
    *,
    order_quantity: int,
) -> tuple[Decimal, Decimal | None, str | None, str | None]:
    requirement = (
        Decimal(order_quantity)
        * Decimal(component.quantity_per_finished_unit)
        * (Decimal("1") + Decimal(component.waste_rate))
    ).quantize(SIX_PLACES, rounding=ROUND_HALF_UP)
    default_candidate = next(
        (candidate for candidate in component.candidates if candidate.is_default),
        None,
    )
    purchase_unit = (
        default_candidate.purchase_unit_snapshot if default_candidate is not None else None
    )
    if purchase_unit is None:
        return requirement, None, None, "未冻结默认供应商候选"

    purchase_quantity = requirement
    if purchase_unit != component.consumption_unit:
        factor = component.units_per_purchase_unit
        if factor is None or not component.conversion_basis:
            return requirement, None, purchase_unit, "用量单位与采购单位不同，缺少换算依据"
        purchase_quantity = requirement / Decimal(factor)

    if purchase_unit in DISCRETE_PURCHASE_UNITS:
        purchase_quantity = purchase_quantity.to_integral_value(rounding=ROUND_CEILING)
    elif purchase_unit in CONTINUOUS_PURCHASE_UNITS:
        purchase_quantity = purchase_quantity.quantize(
            THREE_PLACES, rounding=ROUND_HALF_UP
        )
    else:
        purchase_quantity = purchase_quantity.quantize(
            SIX_PLACES, rounding=ROUND_HALF_UP
        )
    return requirement, purchase_quantity, purchase_unit, None


def _component_response(
    row: SalesOrderItemExternalComponent,
    *,
    order_quantity: int,
) -> dict[str, Any]:
    requirement, purchase_quantity, purchase_unit, blocked_reason = (
        _calculated_quantities(row, order_quantity=order_quantity)
    )
    candidates = [
        {
            "id": candidate.id,
            "external_product_id": candidate.external_product_id_snapshot,
            "is_default": candidate.is_default,
            "supplier_id": candidate.supplier_id_snapshot,
            "supplier_name": candidate.supplier_name_snapshot,
            "supplier_product_code": candidate.supplier_product_code_snapshot,
            "product_name": candidate.product_name_snapshot,
            "purchase_unit": candidate.purchase_unit_snapshot,
            "customer_scope_id": candidate.customer_scope_id_snapshot,
            "external_product_version": candidate.external_product_version_snapshot,
        }
        for candidate in row.candidates
    ]
    default_candidate = next(
        (candidate for candidate in candidates if candidate["is_default"]), None
    )
    return {
        "id": row.id,
        "source_kind": row.source_kind,
        "source_component_set_id": row.source_component_set_id,
        "source_component_id": row.source_component_id,
        "source_component_set_version": row.source_component_set_version,
        "display_order": row.display_order,
        "purpose": row.purpose,
        "quantity_per_finished_unit": _decimal_text(
            Decimal(row.quantity_per_finished_unit)
        ),
        "waste_rate": _decimal_text(Decimal(row.waste_rate)),
        "consumption_unit": row.consumption_unit,
        "units_per_purchase_unit": (
            _decimal_text(Decimal(row.units_per_purchase_unit))
            if row.units_per_purchase_unit is not None
            else None
        ),
        "conversion_basis": row.conversion_basis,
        "is_required": row.is_required,
        "remarks": row.remarks,
        "category_code": row.category_code,
        "category_label": SUPPLIER_CATEGORY_LABELS.get(
            row.category_code, row.category_code
        ),
        "specification": json.loads(row.specification_json or "{}"),
        "specification_summary": row.specification_summary,
        "requirement_quantity": _decimal_text(requirement),
        "requirement_unit": row.consumption_unit,
        "suggested_purchase_quantity": (
            _decimal_text(purchase_quantity)
            if purchase_quantity is not None
            else None
        ),
        "suggested_purchase_unit": purchase_unit,
        "suggestion_blocked_reason": blocked_reason,
        "default_candidate": default_candidate,
        "alternative_candidates": [
            candidate for candidate in candidates if not candidate["is_default"]
        ],
        "candidates": candidates,
    }


def _freeze_direct_product_component(
    db: Session,
    *,
    order_item: OrderItem,
) -> list[dict[str, Any]]:
    category = str(order_item.external_packaging_category_code_snapshot or "").strip()
    specification_json = str(
        order_item.external_packaging_specification_json_snapshot or ""
    ).strip()
    specification_summary = str(
        order_item.external_packaging_specification_summary_snapshot or ""
    ).strip()
    purchase_unit = str(
        order_item.external_packaging_purchase_unit_snapshot or ""
    ).strip()
    source_version = int(order_item.external_packaging_product_version_snapshot or 0)
    try:
        candidate_rows = json.loads(
            order_item.external_packaging_candidate_snapshot_json or "[]"
        )
    except (TypeError, json.JSONDecodeError) as error:
        raise OrderExternalPackagingSnapshotError(
            "纯外购产品的供应商候选快照无效，请重新核对常用箱后下单"
        ) from error
    if (
        not category
        or not specification_json
        or not specification_summary
        or not purchase_unit
        or source_version < 1
        or not isinstance(candidate_rows, list)
        or not candidate_rows
    ):
        raise OrderExternalPackagingSnapshotError(
            "纯外购产品资料不完整，请先补齐类别、规格、单位和供应商候选"
        )
    defaults = [row for row in candidate_rows if bool(row.get("is_default"))]
    if len(defaults) != 1:
        raise OrderExternalPackagingSnapshotError(
            "纯外购产品必须冻结且只能冻结一个默认供应商候选"
        )
    component = SalesOrderItemExternalComponent(
        sales_order_item_id=order_item.id,
        source_kind="direct_product",
        source_component_set_id=None,
        source_component_id=None,
        source_component_set_version=source_version,
        display_order=1,
        purpose=order_item.snapshot_product_name,
        quantity_per_finished_unit=Decimal("1"),
        waste_rate=Decimal("0"),
        consumption_unit=purchase_unit,
        units_per_purchase_unit=None,
        conversion_basis=None,
        is_required=True,
        remarks="P1-40B 纯外购产品下单快照",
        category_code=category,
        specification_json=specification_json,
        specification_summary=specification_summary,
    )
    db.add(component)
    db.flush()
    for raw in candidate_rows:
        candidate_unit = str(raw.get("purchase_unit") or "").strip()
        if candidate_unit != purchase_unit:
            raise OrderExternalPackagingSnapshotError(
                "纯外购产品候选采购单位与常用箱冻结单位不一致"
            )
        try:
            external_product_id = int(raw["external_product_id"])
            supplier_id = int(raw["supplier_id"])
            external_product_version = int(raw["external_product_version"])
        except (KeyError, TypeError, ValueError) as error:
            raise OrderExternalPackagingSnapshotError(
                "纯外购产品供应商候选快照缺少稳定标识"
            ) from error
        db.add(
            SalesOrderItemExternalComponentCandidate(
                order_component_id=component.id,
                source_candidate_id=None,
                external_product_id_snapshot=external_product_id,
                is_default=bool(raw.get("is_default")),
                supplier_id_snapshot=supplier_id,
                supplier_name_snapshot=str(raw.get("supplier_name") or "").strip(),
                supplier_product_code_snapshot=str(
                    raw.get("supplier_product_code") or ""
                ).strip(),
                product_name_snapshot=str(raw.get("product_name") or "").strip(),
                purchase_unit_snapshot=candidate_unit,
                customer_scope_id_snapshot=raw.get("customer_scope_id"),
                external_product_version_snapshot=external_product_version,
            )
        )
    db.flush()
    db.refresh(component, attribute_names=["candidates"])
    return [_component_response(component, order_quantity=int(order_item.quantity))]


def freeze_order_item_external_components(
    db: Session,
    *,
    order_item: OrderItem,
) -> list[dict[str, Any]]:
    """Copy the current common-box external component version exactly once."""

    existing = list(
        db.scalars(
            select(SalesOrderItemExternalComponent)
            .options(
                selectinload(SalesOrderItemExternalComponent.candidates)
            )
            .where(
                SalesOrderItemExternalComponent.sales_order_item_id == order_item.id
            )
            .order_by(SalesOrderItemExternalComponent.display_order)
        ).all()
    )
    if existing:
        return [
            _component_response(row, order_quantity=int(order_item.quantity))
            for row in existing
        ]
    if order_item.supply_mode_snapshot == "external_purchase":
        return _freeze_direct_product_component(db, order_item=order_item)

    component_set = db.scalar(
        select(ProductExternalComponentSet)
        .options(
            selectinload(ProductExternalComponentSet.components).selectinload(
                ProductExternalComponent.candidates
            )
        )
        .where(
            ProductExternalComponentSet.product_id == order_item.product_id,
            ProductExternalComponentSet.is_current.is_(True),
        )
    )
    if component_set is None:
        if order_item.supply_mode_snapshot == "mixed_bom":
            raise OrderExternalPackagingSnapshotError(
                "混合 BOM 产品尚未配置外购包装组件，不能下单"
            )
        return []

    frozen: list[SalesOrderItemExternalComponent] = []
    for component in component_set.components:
        order_component = SalesOrderItemExternalComponent(
            sales_order_item_id=order_item.id,
            source_kind="bound_component",
            source_component_set_id=component_set.id,
            source_component_id=component.id,
            source_component_set_version=component_set.version,
            display_order=component.display_order,
            purpose=component.purpose,
            quantity_per_finished_unit=component.quantity_per_finished_unit,
            waste_rate=component.waste_rate,
            consumption_unit=component.consumption_unit,
            units_per_purchase_unit=component.units_per_purchase_unit,
            conversion_basis=component.conversion_basis,
            is_required=component.is_required,
            remarks=component.remarks,
            category_code=component.category_code,
            specification_json=component.specification_json,
            specification_summary=component.specification_summary,
        )
        db.add(order_component)
        db.flush()
        for candidate in component.candidates:
            db.add(
                SalesOrderItemExternalComponentCandidate(
                    order_component_id=order_component.id,
                    source_candidate_id=candidate.id,
                    external_product_id_snapshot=candidate.external_product_id,
                    is_default=candidate.is_default,
                    supplier_id_snapshot=candidate.supplier_id_snapshot,
                    supplier_name_snapshot=candidate.supplier_name_snapshot,
                    supplier_product_code_snapshot=(
                        candidate.supplier_product_code_snapshot
                    ),
                    product_name_snapshot=candidate.product_name_snapshot,
                    purchase_unit_snapshot=candidate.purchase_unit_snapshot,
                    customer_scope_id_snapshot=candidate.customer_scope_id_snapshot,
                    external_product_version_snapshot=(
                        candidate.external_product_version_snapshot
                    ),
                )
            )
        frozen.append(order_component)
    db.flush()
    for row in frozen:
        db.refresh(row, attribute_names=["candidates"])
    return [
        _component_response(row, order_quantity=int(order_item.quantity))
        for row in frozen
    ]


def get_order_item_external_components_by_item_ids(
    db: Session,
    order_items: list[OrderItem],
) -> dict[int, list[dict[str, Any]]]:
    if not order_items:
        return {}
    quantities = {int(item.id): int(item.quantity) for item in order_items}
    rows = list(
        db.scalars(
            select(SalesOrderItemExternalComponent)
            .options(
                selectinload(SalesOrderItemExternalComponent.candidates)
            )
            .where(
                SalesOrderItemExternalComponent.sales_order_item_id.in_(quantities)
            )
            .order_by(
                SalesOrderItemExternalComponent.sales_order_item_id,
                SalesOrderItemExternalComponent.display_order,
            )
        ).all()
    )
    result: dict[int, list[dict[str, Any]]] = {
        item_id: [] for item_id in quantities
    }
    for row in rows:
        item_id = int(row.sales_order_item_id)
        result[item_id].append(
            _component_response(row, order_quantity=quantities[item_id])
        )
    return result
