from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter
from pydantic import BaseModel, Field


router = APIRouter(prefix="/api/engine", tags=["carton-engine"])
MONEY = Decimal("0.00")
MEASURE = Decimal("0.00")
AREA = Decimal("0.0001")


class CartonCalculationPayload(BaseModel):
    length_mm: Decimal = Field(gt=0)
    width_mm: Decimal = Field(gt=0)
    height_mm: Decimal = Field(gt=0)
    box_category: str = Field(default="normal", pattern="^(normal|die_cut)$")
    length_extra_mm: Decimal = Decimal("8")
    width_extra_mm: Decimal = Decimal("4")
    glue_flap_mm: Decimal = Decimal("0")
    die_cut_expand_length_mm: Decimal | None = Field(default=None, gt=0)
    die_cut_expand_width_mm: Decimal | None = Field(default=None, gt=0)
    customer_square_price: Decimal = Field(default=Decimal("0"), ge=0)
    supplier_square_price: Decimal = Field(default=Decimal("0"), ge=0)
    extra_fee: Decimal = Field(default=Decimal("0"), ge=0)


class CartonCalculationResponse(BaseModel):
    paper_length_mm: str
    paper_width_mm: str
    score_line: str
    area_m2: str
    sale_unit_price: str
    purchase_unit_cost: str


def q2(value: Decimal) -> Decimal:
    return value.quantize(MEASURE, rounding=ROUND_HALF_UP)


def money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def area(value: Decimal) -> Decimal:
    return value.quantize(AREA, rounding=ROUND_HALF_UP)


def measure_text(value: Decimal) -> str:
    value = q2(value)
    if value == value.to_integral_value():
        return str(int(value))
    return format(value.normalize(), "f")


def calculate_carton(payload: CartonCalculationPayload) -> dict[str, Decimal | str]:
    if payload.box_category == "die_cut":
        paper_length = payload.die_cut_expand_length_mm or (
            payload.length_mm + payload.width_mm + payload.length_extra_mm
        )
        paper_width = payload.die_cut_expand_width_mm or (
            payload.width_mm + payload.height_mm + payload.width_extra_mm
        )
        area_m2 = area((paper_length * paper_width) / Decimal("1000000"))
        score_line = "模切展开"
    else:
        half_length = payload.length_mm + payload.width_mm + payload.length_extra_mm
        paper_length = half_length * Decimal("2") + payload.glue_flap_mm
        paper_width = payload.width_mm + payload.height_mm + payload.width_extra_mm
        area_m2 = area((paper_length * paper_width) / Decimal("1000000"))
        score_line = (
            f"{measure_text(payload.width_mm)}*"
            f"{measure_text(payload.length_mm)}*"
            f"{measure_text(payload.width_mm)}*"
            f"{measure_text(payload.length_mm)}"
        )

    sale = money(area_m2 * payload.customer_square_price + payload.extra_fee)
    cost = money(area_m2 * payload.supplier_square_price)
    return {
        "paper_length_mm": q2(paper_length),
        "paper_width_mm": q2(paper_width),
        "score_line": score_line,
        "area_m2": area_m2,
        "sale_unit_price": sale,
        "purchase_unit_cost": cost,
    }


@router.post("/calculate-carton", response_model=CartonCalculationResponse)
def calculate_carton_endpoint(payload: CartonCalculationPayload) -> dict[str, str]:
    result = calculate_carton(payload)
    return {key: str(value) for key, value in result.items()}
