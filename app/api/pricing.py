from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import RoleChecker
from app.models.user import User
from app.services.pricing import PricingError, calculate_price


router = APIRouter()
can_calculate = RoleChecker(["admin", "sales", "finance"])


class PricingRequest(BaseModel):
    box_category: str
    board_square_price: Decimal
    length_mm: Decimal | None = None
    width_mm: Decimal | None = None
    height_mm: Decimal | None = None
    unfolded_length_mm: Decimal | None = None
    unfolded_width_mm: Decimal | None = None
    extra_fee: Decimal = Decimal("0")


@router.post("/calculate")
def calculate_box_price(
    payload: PricingRequest,
    _user: User = Depends(can_calculate),
) -> dict:
    try:
        result = calculate_price(**payload.model_dump())
    except PricingError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {
        "box_category": payload.box_category,
        "area_m2": result.area_m2,
        "board_square_price": payload.board_square_price,
        "extra_fee": payload.extra_fee,
        "unit_price": result.unit_price,
    }
