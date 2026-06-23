from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


MONEY_QUANTUM = Decimal("0.00")
SQUARE_MILLIMETRES_PER_SQUARE_METRE = Decimal("1000000")


class PricingError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PricingResult:
    area_m2: Decimal
    unit_price: Decimal


def _decimal(value: Any, field_name: str, *, allow_zero: bool = False) -> Decimal:
    if value is None or value == "":
        raise PricingError(f"{field_name}不能为空")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise PricingError(f"{field_name}不是有效数字") from error
    if number < 0 or (number == 0 and not allow_zero):
        raise PricingError(f"{field_name}必须大于0")
    return number


def calculate_price(
    *,
    box_category: str,
    board_square_price: Decimal,
    length_mm: Decimal | None = None,
    width_mm: Decimal | None = None,
    height_mm: Decimal | None = None,
    unfolded_length_mm: Decimal | None = None,
    unfolded_width_mm: Decimal | None = None,
    extra_fee: Decimal = Decimal("0"),
) -> PricingResult:
    category = str(box_category).strip().lower()
    board_price = _decimal(
        board_square_price,
        "纸板平方单价",
        allow_zero=True,
    )
    fee = _decimal(extra_fee, "附加费", allow_zero=True)

    if category == "normal":
        length = _decimal(length_mm, "长")
        width = _decimal(width_mm, "宽")
        height = _decimal(height_mm, "高")
        area = (
            (length + width + Decimal("8"))
            * (width + height + Decimal("4"))
            * Decimal("2")
            / SQUARE_MILLIMETRES_PER_SQUARE_METRE
        )
    elif category == "die_cut":
        unfolded_length = _decimal(unfolded_length_mm, "展开长")
        unfolded_width = _decimal(unfolded_width_mm, "展开宽")
        area = (
            unfolded_length
            * unfolded_width
            / SQUARE_MILLIMETRES_PER_SQUARE_METRE
        )
    else:
        raise PricingError("箱型必须是 normal 或 die_cut")

    unit_price = (area * board_price + fee).quantize(
        MONEY_QUANTUM,
        rounding=ROUND_HALF_UP,
    )
    return PricingResult(area_m2=area.normalize(), unit_price=unit_price)
