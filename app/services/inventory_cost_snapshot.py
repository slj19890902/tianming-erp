from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import json
import re
import unicodedata

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.time_contract import utc_now_naive
from app.models.material import Material
from app.models.product import Product
from app.models.warehouse_inventory import InventoryLot, SemiFinishedInventoryDetail
from app.services.material_pricing import get_effective_material_price


SQUARE_MM_PER_M2 = Decimal("1000000")
AREA_QUANTUM = Decimal("0.000001")
COST_QUANTUM = Decimal("0.0001")
PRICE_QUANTUM = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class InventoryCostEstimate:
    unit_cost: Decimal
    square_price: Decimal
    area_m2: Decimal
    source: str
    detail: dict


def _positive_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
    except (ValueError, ArithmeticError):
        return None
    return number if number > 0 else None


def _material_key(value: str | None, layer_count: int | None = None) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").upper()
    compact = re.sub(r"\s+", "", normalized)
    expected = 5 if layer_count == 5 else 3 if layer_count == 3 else 7 if layer_count == 7 else None
    if (
        expected is not None
        and len(compact) == expected
        and all(char.isprintable() and not char.isspace() for char in compact)
    ):
        return compact
    tokens = re.findall(r"[A-Z0-9]+", normalized)
    candidates = [token for token in tokens if any(char.isalpha() for char in token)]
    if not candidates:
        return ""
    if expected:
        exact = next((token for token in candidates if len(token) == expected), None)
        if exact:
            return exact
    return candidates[0]


def _find_material(
    db: Session,
    *,
    material_id: int | None,
    material_code: str | None,
    supplier_name: str | None,
    layer_count: int | None,
) -> Material | None:
    supplier = (supplier_name or "").strip()
    if material_id:
        material = db.get(Material, material_id)
        if material is None or not material.is_active:
            return None
        if supplier and (material.supplier_name or "").strip() != supplier:
            return None
        return material
    key = _material_key(material_code, layer_count)
    if not key:
        return None
    statement = select(Material).where(
        Material.is_active.is_(True),
        func.upper(func.replace(Material.code, " ", "")) == key,
    )
    if supplier:
        return db.scalars(
            statement.where(func.trim(Material.supplier_name) == supplier).order_by(
                Material.id
            )
        ).first()
    matches = list(db.scalars(statement.order_by(Material.id)))
    return matches[0] if len(matches) == 1 else None


def _area_m2(length_mm: object, width_mm: object) -> Decimal | None:
    length = _positive_decimal(length_mm)
    width = _positive_decimal(width_mm)
    if length is None or width is None:
        return None
    return (length * width / SQUARE_MM_PER_M2).quantize(
        AREA_QUANTUM, rounding=ROUND_HALF_UP
    )


def _effective_square_price(
    db: Session,
    *,
    material: Material,
    supplier_name: str | None,
    layer_count: int | None,
    flute_type: str | None,
) -> tuple[Decimal, dict] | None:
    unit = (material.price_unit or "").strip().lower()
    if not unit or not any(token in unit for token in ("㎡", "m²", "m2", "平方")):
        return None
    price = get_effective_material_price(
        db,
        material=material,
        supplier_name=(supplier_name or "").strip() or None,
        layer_count=layer_count,
        flute_type=flute_type,
    )
    effective = _positive_decimal(price.get("effective_price"))
    if effective is None:
        return None
    return effective.quantize(PRICE_QUANTUM, rounding=ROUND_HALF_UP), price


def estimate_finished_product_cost(
    db: Session,
    *,
    product: Product,
    material_code: str | None = None,
    flute_type: str | None = None,
) -> InventoryCostEstimate | None:
    material = _find_material(
        db,
        material_id=product.material_id,
        material_code=material_code or product.default_material_code or product.legacy_material_text,
        supplier_name=product.material.supplier_name if product.material is not None else None,
        layer_count=product.layer_count,
    )
    if material is None:
        return None
    effective = _effective_square_price(
        db,
        material=material,
        supplier_name=material.supplier_name,
        layer_count=product.layer_count or material.layer_count,
        flute_type=flute_type or product.flute_type,
    )
    if effective is None:
        return None
    square_price, price_detail = effective
    report_length = product.report_length_mm or product.default_cardboard_length
    report_width = product.report_width_mm or product.default_cardboard_width
    cover_area = _area_m2(report_length, report_width)
    if cover_area is None:
        return None
    box_style = (product.box_style or "").strip()
    is_a3 = "A3" in box_style.upper() or "天地盖" in box_style
    has_any_base_dimension = (
        product.base_report_length_mm is not None
        or product.base_report_width_mm is not None
    )
    base_area = _area_m2(product.base_report_length_mm, product.base_report_width_mm)
    if (is_a3 or has_any_base_dimension) and base_area is None:
        return None
    from app.services.sheet_cutting_settings import component_settings
    sheet_settings = getattr(product, "sheet_cutting_settings", None)
    if sheet_settings is not None:
        cover_setting = component_settings(sheet_settings, "cover" if base_area is not None else "whole")
        cover_contract = cover_setting.contract(report_length, report_width)
        cover_area = (cover_area / cover_setting.mold_count if cover_setting.actual_supplier_length_mm is None
                      else _area_m2(*cover_contract.supplier_size_mm) / cover_contract.yield_per_supplier_sheet)
        if base_area is not None:
            base_setting = component_settings(sheet_settings, "base")
            base_contract = base_setting.contract(product.base_report_length_mm, product.base_report_width_mm)
            base_area = (base_area / base_setting.mold_count if base_setting.actual_supplier_length_mm is None
                         else _area_m2(*base_contract.supplier_size_mm) / base_contract.yield_per_supplier_sheet)
    components = [
        {
            "component": "cover" if base_area is not None else "whole",
            "length_mm": str(report_length),
            "width_mm": str(report_width),
            "area_m2": str(cover_area),
        }
    ]
    area = cover_area
    if base_area is not None:
        area += base_area
        components.append(
            {
                "component": "base",
                "length_mm": str(product.base_report_length_mm),
                "width_mm": str(product.base_report_width_mm),
                "area_m2": str(base_area),
            }
        )
    else:
        pieces = max(int(product.pieces_per_box or 1), 1)
        area *= pieces
        components[0]["pieces_per_box"] = pieces
    area = area.quantize(AREA_QUANTUM, rounding=ROUND_HALF_UP)
    unit_cost = (area * square_price).quantize(COST_QUANTUM, rounding=ROUND_HALF_UP)
    return InventoryCostEstimate(
        unit_cost=unit_cost,
        square_price=square_price,
        area_m2=area,
        source="material_quote_area",
        detail={
            "inventory_type": "finished",
            **({"sheet_cutting_settings": sheet_settings, "area_basis": "supplier_sheet_area_per_output" if sheet_settings["schema_version"] == 3 else "theoretical_sheet_area_per_mold_output"} if sheet_settings else {}),
            "formula": "length_mm * width_mm / 1,000,000 * current_effective_material_square_price",
            "estimate_basis": "current_material_quote_not_actual_cash_cost",
            "material_id": material.id,
            "material_code": material.code,
            "supplier_name": material.supplier_name,
            "price_unit": material.price_unit,
            "material_quote_date": material.quote_date.isoformat() if material.quote_date else None,
            "material_price_source": material.price_source,
            "base_square_price": price_detail.get("base_price"),
            "flute_delta": price_detail.get("flute_delta"),
            "flute_rule_id": price_detail.get("rule_id"),
            "flute_type": (flute_type or product.flute_type or "").strip().upper() or None,
            "components": components,
        },
    )


def estimate_semi_finished_cost(
    db: Session,
    *,
    material_id: int | None,
    material_code: str | None,
    supplier_name: str | None,
    layer_count: int | None,
    flute_type: str | None,
    board_length_mm: object,
    board_width_mm: object,
) -> InventoryCostEstimate | None:
    material = _find_material(
        db,
        material_id=material_id,
        material_code=material_code,
        supplier_name=supplier_name,
        layer_count=layer_count,
    )
    area = _area_m2(board_length_mm, board_width_mm)
    if material is None or area is None:
        return None
    effective = _effective_square_price(
        db,
        material=material,
        supplier_name=(supplier_name or "").strip() or material.supplier_name,
        layer_count=layer_count or material.layer_count,
        flute_type=flute_type,
    )
    if effective is None:
        return None
    square_price, price_detail = effective
    unit_cost = (area * square_price).quantize(COST_QUANTUM, rounding=ROUND_HALF_UP)
    return InventoryCostEstimate(
        unit_cost=unit_cost,
        square_price=square_price,
        area_m2=area,
        source="material_quote_area",
        detail={
            "inventory_type": "semi_finished",
            "formula": "length_mm * width_mm / 1,000,000 * current_effective_material_square_price",
            "estimate_basis": "current_material_quote_not_actual_cash_cost",
            "material_id": material.id,
            "material_code": material.code,
            "supplier_name": material.supplier_name,
            "price_unit": material.price_unit,
            "material_quote_date": material.quote_date.isoformat() if material.quote_date else None,
            "material_price_source": material.price_source,
            "base_square_price": price_detail.get("base_price"),
            "flute_delta": price_detail.get("flute_delta"),
            "flute_rule_id": price_detail.get("rule_id"),
            "flute_type": (flute_type or "").strip().upper() or None,
            "board_length_mm": str(board_length_mm),
            "board_width_mm": str(board_width_mm),
        },
    )


def estimate_inventory_lot_cost(
    db: Session,
    lot: InventoryLot,
) -> InventoryCostEstimate | None:
    if lot.finished_detail is not None:
        product = db.get(Product, lot.finished_detail.product_id)
        if product is None:
            return None
        return estimate_finished_product_cost(
            db,
            product=product,
            material_code=lot.finished_detail.material_code_snapshot,
            flute_type=lot.finished_detail.flute_type_snapshot,
        )
    detail: SemiFinishedInventoryDetail | None = lot.semi_finished_detail
    if detail is None:
        return None
    return estimate_semi_finished_cost(
        db,
        material_id=detail.material_id,
        material_code=detail.material_code_snapshot,
        supplier_name=detail.supplier_name,
        layer_count=detail.layer_count,
        flute_type=detail.flute_type,
        board_length_mm=detail.board_length_mm,
        board_width_mm=detail.board_width_mm,
    )


def apply_cost_snapshot(
    lot: InventoryLot,
    estimate: InventoryCostEstimate | None,
    *,
    captured_at: datetime | None = None,
) -> None:
    if estimate is None:
        return
    lot.estimated_unit_cost_snapshot = estimate.unit_cost
    lot.estimated_square_price_snapshot = estimate.square_price
    lot.estimated_cost_area_m2_snapshot = estimate.area_m2
    lot.cost_snapshot_source = estimate.source
    lot.cost_snapshot_detail_json = json.dumps(
        estimate.detail, ensure_ascii=False, sort_keys=True
    )
    lot.cost_snapshot_at = captured_at or utc_now_naive()


def estimate_from_snapshot(lot: InventoryLot) -> InventoryCostEstimate | None:
    unit_cost = _positive_decimal(lot.estimated_unit_cost_snapshot)
    square_price = _positive_decimal(lot.estimated_square_price_snapshot)
    area = _positive_decimal(lot.estimated_cost_area_m2_snapshot)
    if unit_cost is None:
        return None
    if (square_price is None or area is None) and lot.cost_snapshot_source not in {"inventory_confirmed_material", "owner_current_external_reference", "manual_sheet_unit_cost"}:
        return None
    square_price = square_price or Decimal(0)
    area = area or Decimal(0)
    try:
        detail = json.loads(lot.cost_snapshot_detail_json or "{}")
    except (TypeError, json.JSONDecodeError):
        detail = {}
    return InventoryCostEstimate(
        unit_cost=unit_cost.quantize(COST_QUANTUM, rounding=ROUND_HALF_UP),
        square_price=square_price.quantize(PRICE_QUANTUM, rounding=ROUND_HALF_UP),
        area_m2=area.quantize(AREA_QUANTUM, rounding=ROUND_HALF_UP),
        source=lot.cost_snapshot_source or "inventory_cost_snapshot",
        detail=detail,
    )
