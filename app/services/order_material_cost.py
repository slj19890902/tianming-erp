from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable, Mapping

from sqlalchemy.orm import Session

from app.models.material import Material
from app.models.order import OrderItem
from app.services.material_pricing import get_effective_material_price
from app.services.requisition_quantities import (
    DEFAULT_CUTTING_MODE,
    cutting_factor,
    purchase_sheet_quantity,
)


SQUARE_MM_PER_M2 = Decimal("1000000")
AREA_QUANTUM = Decimal("0.000001")
PRICE_QUANTUM = Decimal("0.0001")
COST_QUANTUM = Decimal("0.0001")
MONEY_QUANTUM = Decimal("0.01")


def _decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    return result if result.is_finite() and result > 0 else None


def _positive_int(value: object, default: int = 1) -> int:
    number = _decimal(value)
    if number is None or number != number.to_integral_value():
        return default
    return max(int(number), 1)


def _material(
    db: Session,
    material_id: object,
) -> Material | None:
    try:
        resolved_id = int(material_id) if material_id not in (None, "") else None
    except (TypeError, ValueError):
        resolved_id = None
    if resolved_id is None:
        return None
    material = db.get(Material, resolved_id)
    return material if material is not None and material.is_active else None


def _component(
    db: Session,
    *,
    source_type: str,
    label: str,
    length_mm: object,
    width_mm: object,
    required_piece_qty: int,
    cutting_mode: str | None,
    material_id: object,
    supplier_name: str | None,
    layer_count: int | None,
    flute_type: str | None,
    spare_sheet_quantity: int = 0,
) -> tuple[dict[str, Any] | None, list[str]]:
    missing: list[str] = []
    if int(required_piece_qty or 0) <= 0:
        missing.append(f"{label}需求数量无效")
    length = _decimal(length_mm)
    width = _decimal(width_mm)
    if length is None or width is None:
        missing.append(f"{label}缺少报料长宽")
    material = _material(db, material_id)
    if material is None:
        missing.append(f"{label}缺少有效供应商材质")
    price_detail: dict[str, Any] | None = None
    square_price: Decimal | None = None
    if material is not None:
        price_detail = get_effective_material_price(
            db,
            material=material,
            supplier_name=(supplier_name or "").strip() or material.supplier_name,
            layer_count=layer_count or material.layer_count,
            flute_type=flute_type,
        )
        square_price = _decimal(price_detail.get("effective_price"))
        if square_price is None:
            missing.append(f"{label}缺少有效平方成本")
    if missing:
        return None, missing

    mode = (cutting_mode or "").strip() or DEFAULT_CUTTING_MODE
    factor = cutting_factor(mode)
    sheets = purchase_sheet_quantity(required_piece_qty, 0, mode) + max(
        int(spare_sheet_quantity or 0), 0
    )
    area = (length * width / SQUARE_MM_PER_M2).quantize(
        AREA_QUANTUM, rounding=ROUND_HALF_UP
    )
    total = (area * Decimal(sheets) * square_price).quantize(
        COST_QUANTUM, rounding=ROUND_HALF_UP
    )
    return {
        "source_type": source_type,
        "label": label,
        "report_length_mm": str(length),
        "report_width_mm": str(width),
        "area_per_sheet_m2": str(area),
        "required_piece_quantity": int(required_piece_qty),
        "cutting_mode": mode,
        "yield_per_purchase_sheet": factor,
        "purchase_sheet_quantity": sheets,
        "spare_sheet_quantity": max(int(spare_sheet_quantity or 0), 0),
        "material_id": material.id,
        "material_code": material.code,
        "material_version": material.version,
        "supplier_name": price_detail.get("supplier_name") or material.supplier_name,
        "material_quote_date": material.quote_date.isoformat() if material.quote_date else None,
        "material_price_source": material.price_source,
        "material_price_unit": material.price_unit,
        "layer_count": price_detail.get("layer_count") or layer_count,
        "flute_type": price_detail.get("flute_type") or flute_type,
        "base_square_price": price_detail.get("base_price"),
        "flute_delta": price_detail.get("flute_delta"),
        "flute_rule_id": price_detail.get("rule_id"),
        "effective_square_price": str(
            square_price.quantize(PRICE_QUANTUM, rounding=ROUND_HALF_UP)
        ),
        "estimated_material_cost": str(total),
    }, []


def _main_sources(item: OrderItem) -> list[dict[str, Any]]:
    product = item.product
    report_length = item.snapshot_report_length_mm or item.cardboard_len
    report_width = item.snapshot_report_width_mm or item.cardboard_width
    base_length = item.snapshot_base_report_length_mm
    base_width = item.snapshot_base_report_width_mm
    common = {
        "material_id": item.material_id or (product.material_id if product else None),
        "supplier_name": item.snapshot_supplier_name,
        "layer_count": item.layer_count or (product.layer_count if product else None),
        "flute_type": item.flute_type or (product.flute_type if product else None),
    }
    if base_length is not None or base_width is not None:
        return [
            {
                **common,
                "source_type": "order_cover",
                "label": "父件盖片",
                "length_mm": report_length,
                "width_mm": report_width,
                "required_piece_qty": max(int(item.quantity or 0), 0),
                "cutting_mode": DEFAULT_CUTTING_MODE,
            },
            {
                **common,
                "source_type": "order_base",
                "label": "父件底片",
                "length_mm": base_length,
                "width_mm": base_width,
                "required_piece_qty": max(int(item.quantity or 0), 0),
                "cutting_mode": DEFAULT_CUTTING_MODE,
            },
        ]
    pieces = _positive_int(
        item.snapshot_pieces_per_box
        or (product.pieces_per_box if product is not None else None),
        1,
    )
    return [
        {
            **common,
            "source_type": "order_main",
            "label": "父件主片" if item.combination_role == "set_parent" else "主片",
            "length_mm": report_length,
            "width_mm": report_width,
            "required_piece_qty": max(int(item.quantity or 0), 0) * pieces,
            "cutting_mode": item.special_process or DEFAULT_CUTTING_MODE,
        }
    ]


def _bom_sources(
    components: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for position, row in enumerate(components, start=1):
        name = str(
            row.get("snapshot_component_product_name")
            or row.get("snapshot_product_name")
            or row.get("internal_component_code")
            or f"组件{position}"
        ).strip()
        result.append(
            {
                "source_type": "bom_component",
                "label": f"组件：{name}",
                "length_mm": row.get("snapshot_component_report_length_mm"),
                "width_mm": row.get("snapshot_component_report_width_mm"),
                "required_piece_qty": max(
                    int(
                        row.get("effective_required_piece_quantity")
                        or row.get("required_piece_quantity")
                        or 0
                    ),
                    0,
                ),
                "cutting_mode": row.get(
                    "snapshot_component_default_cutting_mode"
                )
                or DEFAULT_CUTTING_MODE,
                "material_id": row.get("snapshot_component_material_id"),
                "supplier_name": row.get("snapshot_component_supplier_name"),
                "layer_count": row.get("snapshot_component_layer_count"),
                "flute_type": row.get("snapshot_component_flute_type"),
                "spare_sheet_quantity": max(
                    int(row.get("spare_sheet_quantity") or 0), 0
                ),
            }
        )
    return result


def estimate_order_item_material_cost(
    db: Session,
    item: OrderItem,
    *,
    bom_components: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build a read-only current material estimate from frozen physical inputs.

    This is deliberately not an order-cost snapshot.  It never writes the
    order and never derives report dimensions from a box-style name.
    """

    sources = _main_sources(item)
    sources.extend(_bom_sources(bom_components))
    calculated: list[dict[str, Any]] = []
    missing: list[str] = []
    for source in sources:
        component, component_missing = _component(db, **source)
        if component is not None:
            calculated.append(component)
        missing.extend(component_missing)

    # Preserve source order while avoiding repeated noise for the operator.
    missing = list(dict.fromkeys(missing))
    complete = bool(calculated) and not missing
    status = "calculated" if complete else "partial" if calculated else "missing"
    status_label = {
        "calculated": "材料成本已估算",
        "partial": "部分材料成本待完善",
        "missing": "材料成本待完善",
    }[status]
    known_total = sum(
        (Decimal(row["estimated_material_cost"]) for row in calculated),
        Decimal("0"),
    ).quantize(COST_QUANTUM, rounding=ROUND_HALF_UP)
    quantity = max(int(item.quantity or 0), 0)
    unit_cost = (
        (known_total / Decimal(quantity)).quantize(COST_QUANTUM, rounding=ROUND_HALF_UP)
        if complete and quantity > 0
        else None
    )
    total_cost = known_total.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP) if complete else None
    return {
        "material_cost_status": status,
        "material_cost_status_label": status_label,
        "material_cost_scope_label": "当前材料成本（未计生产损耗和加工费）",
        "material_cost_is_current_estimate": True,
        "material_cost_formula_version": "p1-28a-material-v1",
        "estimated_material_unit_cost": str(unit_cost) if unit_cost is not None else None,
        "estimated_material_total_cost": str(total_cost) if total_cost is not None else None,
        "known_material_subtotal": str(
            known_total.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
        )
        if calculated
        else None,
        "material_cost_components": calculated,
        "material_cost_missing_items": missing,
        # Backward-compatible aliases for existing cost-permission screens.
        "cost_status": "calculated" if complete else "pending",
        "estimated_cost": str(unit_cost) if unit_cost is not None else None,
    }
