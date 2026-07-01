from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session, selectinload

from app.api.deps import RoleChecker, get_db
from app.models.company_config import CompanyConfig
from app.models.customer import Customer
from app.models.material import Material
from app.models.product import Product
from app.models.quotation import QuotationItem, QuotationOrder
from app.models.user import User
from app.services import material_pricing
from app.services.pricing import PricingError, calculate_price


router = APIRouter()
can_read = RoleChecker(["admin", "sales", "finance"])
can_operate = RoleChecker(["admin", "sales"])
MONEY = Decimal("0.01")
PRICE = Decimal("0.0001")
STATUS_LABELS = {
    "draft": "草稿",
    "quoted": "已报价",
    "accepted": "客户接受",
    "converted": "已转常用箱",
    "voided": "作废",
}


class QuotationPreviewPayload(BaseModel):
    box_type: str
    length_mm: Decimal | None = Field(default=None, gt=0)
    width_mm: Decimal | None = Field(default=None, gt=0)
    height_mm: Decimal | None = Field(default=None, gt=0)
    material_id: int | None = None
    flute_type: str | None = None
    margin_rate: Decimal = Field(default=Decimal("20"), ge=0, lt=100)


class QuotationItemPayload(QuotationPreviewPayload):
    product_name: str = Field(min_length=1, max_length=250)
    temporary_code: str | None = Field(default=None, max_length=150)
    quantity: int | None = Field(default=None, gt=0)
    final_unit_price: Decimal | None = Field(default=None, ge=0)
    remarks: str | None = None


class QuotationPayload(BaseModel):
    quotation_date: date = Field(default_factory=date.today)
    remarks: str | None = None
    items: list[QuotationItemPayload] = Field(min_length=1, max_length=100)


class ConvertPayload(BaseModel):
    product_code: str = Field(min_length=1, max_length=150)
    product_name: str | None = Field(default=None, max_length=250)

    @field_validator("product_code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        code = value.strip()
        if not code:
            raise ValueError("存货编码不能为空")
        return code


def _is_a1(box_type: str | None) -> bool:
    value = str(box_type or "").strip().upper()
    return "A1" in value or "0201" in value


def _material_or_none(db: Session, material_id: int | None) -> Material | None:
    if material_id is None:
        return None
    material = db.get(Material, material_id)
    if material is None or not material.is_active:
        raise HTTPException(status_code=400, detail="所选材质不存在或已停用")
    return material


def _preview(db: Session, payload: QuotationPreviewPayload) -> dict:
    if not _is_a1(payload.box_type):
        return {
            "auto_calculated": False,
            "estimated_unit_cost": None,
            "suggested_unit_price": None,
            "margin_rate": payload.margin_rate,
            "message": "当前箱型暂无自动报价公式，请手工填写单价",
        }
    material = _material_or_none(db, payload.material_id)
    if material is None:
        return {
            "auto_calculated": False,
            "estimated_unit_cost": None,
            "suggested_unit_price": None,
            "margin_rate": payload.margin_rate,
            "message": "请选择材质后计算建议单价",
        }
    effective = material_pricing.get_effective_material_price(
        db,
        material=material,
        flute_type=payload.flute_type,
    )
    square_price = effective.get("effective_price")
    if square_price is None:
        return {
            "auto_calculated": False,
            "estimated_unit_cost": None,
            "suggested_unit_price": None,
            "margin_rate": payload.margin_rate,
            "message": "当前材质缺少平方价，请手工填写最终单价",
        }
    try:
        result = calculate_price(
            box_category="normal",
            board_square_price=Decimal(str(square_price)),
            length_mm=payload.length_mm,
            width_mm=payload.width_mm,
            height_mm=payload.height_mm,
        )
    except PricingError as error:
        return {
            "auto_calculated": False,
            "estimated_unit_cost": None,
            "suggested_unit_price": None,
            "margin_rate": payload.margin_rate,
            "message": str(error),
        }
    cost = Decimal(result.unit_price).quantize(PRICE)
    margin_fraction = payload.margin_rate / Decimal("100")
    suggested = (cost / (Decimal("1") - margin_fraction)).quantize(
        PRICE, rounding=ROUND_HALF_UP
    )
    return {
        "auto_calculated": True,
        "estimated_unit_cost": cost,
        "suggested_unit_price": suggested,
        "margin_rate": payload.margin_rate,
        "area_m2": result.area_m2,
        "material_square_price": square_price,
        "message": "已按现有纸板成本口径计算，建议单价可手工修改",
    }


def _next_number(db: Session, quotation_date: date) -> str:
    sequence = db.execute(
        text(
            """
            INSERT INTO quotation_daily_sequences (sequence_date, last_value)
            VALUES (:sequence_date, 1)
            ON CONFLICT(sequence_date)
            DO UPDATE SET last_value = last_value + 1
            RETURNING last_value
            """
        ),
        {"sequence_date": quotation_date.isoformat()},
    ).scalar_one()
    if sequence > 999:
        raise HTTPException(status_code=409, detail="当日报价单流水号已超过999")
    return f"QT-{quotation_date:%Y%m%d}-{sequence:03d}"


def _replace_items(
    db: Session,
    quotation: QuotationOrder,
    payloads: list[QuotationItemPayload],
) -> None:
    quotation.items.clear()
    total = Decimal("0")
    for payload in payloads:
        material = _material_or_none(db, payload.material_id)
        preview = _preview(db, payload)
        suggested = preview["suggested_unit_price"]
        final_price = payload.final_unit_price
        if final_price is None:
            final_price = suggested
        if final_price is None:
            raise HTTPException(
                status_code=400,
                detail=f"{payload.product_name} 缺少最终单价，请手工填写",
            )
        final_price = Decimal(final_price).quantize(PRICE, rounding=ROUND_HALF_UP)
        item = QuotationItem(
            product_name=payload.product_name.strip(),
            temporary_code=(payload.temporary_code or "").strip() or None,
            box_type=payload.box_type.strip(),
            length_mm=payload.length_mm,
            width_mm=payload.width_mm,
            height_mm=payload.height_mm,
            material_id=material.id if material else None,
            material_supplier=material.supplier_name if material else None,
            material_code=material.code if material else None,
            flute_type=payload.flute_type,
            quantity=payload.quantity,
            estimated_unit_cost=preview["estimated_unit_cost"],
            margin_rate=payload.margin_rate,
            suggested_unit_price=suggested,
            final_unit_price=final_price,
            remarks=(payload.remarks or "").strip() or None,
        )
        quotation.items.append(item)
        if payload.quantity:
            total += final_price * payload.quantity
    quotation.total_amount = total.quantize(MONEY, rounding=ROUND_HALF_UP)


def _item_dict(item: QuotationItem) -> dict:
    amount = (
        (item.final_unit_price * item.quantity).quantize(MONEY)
        if item.quantity
        else None
    )
    return {
        "id": item.id,
        "product_name": item.product_name,
        "temporary_code": item.temporary_code,
        "box_type": item.box_type,
        "length_mm": item.length_mm,
        "width_mm": item.width_mm,
        "height_mm": item.height_mm,
        "material_id": item.material_id,
        "material_supplier": item.material_supplier,
        "material_code": item.material_code,
        "flute_type": item.flute_type,
        "quantity": item.quantity,
        "estimated_unit_cost": item.estimated_unit_cost,
        "margin_rate": item.margin_rate,
        "suggested_unit_price": item.suggested_unit_price,
        "final_unit_price": item.final_unit_price,
        "amount": amount,
        "remarks": item.remarks,
        "converted_product_id": item.converted_product_id,
    }


def _quotation_dict(quotation: QuotationOrder) -> dict:
    return {
        "id": quotation.id,
        "quotation_no": quotation.quotation_no,
        "customer_id": quotation.customer_id,
        "customer_name": quotation.customer_name,
        "quotation_date": quotation.quotation_date,
        "status": quotation.status,
        "status_label": STATUS_LABELS.get(quotation.status, quotation.status),
        "total_amount": quotation.total_amount,
        "remarks": quotation.remarks,
        "created_at": quotation.created_at,
        "updated_at": quotation.updated_at,
        "items": [_item_dict(item) for item in quotation.items],
    }


def _quotation_or_404(db: Session, quotation_id: int) -> QuotationOrder:
    quotation = db.scalar(
        select(QuotationOrder)
        .options(selectinload(QuotationOrder.items))
        .where(QuotationOrder.id == quotation_id)
    )
    if quotation is None:
        raise HTTPException(status_code=404, detail="报价单不存在")
    return quotation


@router.post("/preview")
def preview_quotation_item(
    payload: QuotationPreviewPayload,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    return _preview(db, payload)


@router.get("")
def list_quotations(
    customer_id: int | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    query = select(QuotationOrder).options(selectinload(QuotationOrder.items))
    if customer_id is not None:
        query = query.where(QuotationOrder.customer_id == customer_id)
    if status_filter:
        query = query.where(QuotationOrder.status == status_filter)
    quotations = db.scalars(
        query.order_by(
            QuotationOrder.quotation_date.desc(),
            QuotationOrder.id.desc(),
        )
    ).all()
    return {"items": [_quotation_dict(row) for row in quotations], "total": len(quotations)}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_quotation(
    customer_id: int,
    payload: QuotationPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    customer = db.get(Customer, customer_id)
    if customer is None or not customer.is_active:
        raise HTTPException(status_code=404, detail="客户不存在或已停用")
    quotation = QuotationOrder(
        quotation_no=_next_number(db, payload.quotation_date),
        customer_id=customer.id,
        customer_name=customer.name,
        quotation_date=payload.quotation_date,
        status="draft",
        remarks=(payload.remarks or "").strip() or None,
        created_by=user.id,
    )
    _replace_items(db, quotation, payload.items)
    db.add(quotation)
    db.commit()
    db.refresh(quotation)
    return _quotation_dict(_quotation_or_404(db, quotation.id))


@router.get("/{quotation_id}")
def get_quotation(
    quotation_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    return _quotation_dict(_quotation_or_404(db, quotation_id))


@router.put("/{quotation_id}")
def update_quotation(
    quotation_id: int,
    payload: QuotationPayload,
    db: Session = Depends(get_db),
    _user: User = Depends(can_operate),
) -> dict:
    quotation = _quotation_or_404(db, quotation_id)
    if quotation.status not in {"draft", "quoted"}:
        raise HTTPException(status_code=409, detail="客户已接受、已转常用箱或已作废报价不能修改")
    quotation.quotation_date = payload.quotation_date
    quotation.remarks = (payload.remarks or "").strip() or None
    quotation.status = "draft"
    _replace_items(db, quotation, payload.items)
    db.commit()
    return _quotation_dict(_quotation_or_404(db, quotation.id))


@router.post("/{quotation_id}/generate")
def generate_quotation(
    quotation_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(can_operate),
) -> dict:
    quotation = _quotation_or_404(db, quotation_id)
    if quotation.status not in {"draft", "quoted"}:
        raise HTTPException(status_code=409, detail="当前报价状态不能重新生成")
    if not quotation.items:
        raise HTTPException(status_code=400, detail="报价单至少需要一条明细")
    quotation.status = "quoted"
    db.commit()
    return _quotation_dict(_quotation_or_404(db, quotation.id))


@router.post("/{quotation_id}/accept")
def accept_quotation(
    quotation_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(can_operate),
) -> dict:
    quotation = _quotation_or_404(db, quotation_id)
    if quotation.status != "quoted":
        raise HTTPException(status_code=409, detail="只有已报价状态可以标记客户接受")
    quotation.status = "accepted"
    db.commit()
    return _quotation_dict(_quotation_or_404(db, quotation.id))


@router.post("/{quotation_id}/void")
def void_quotation(
    quotation_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(can_operate),
) -> dict:
    quotation = _quotation_or_404(db, quotation_id)
    if quotation.status == "converted" or any(
        item.converted_product_id is not None for item in quotation.items
    ):
        raise HTTPException(status_code=409, detail="已有明细转入常用箱，报价不能作废")
    quotation.status = "voided"
    db.commit()
    return _quotation_dict(_quotation_or_404(db, quotation.id))


@router.post("/items/{item_id}/convert-to-product", status_code=status.HTTP_201_CREATED)
def convert_to_product(
    item_id: int,
    payload: ConvertPayload,
    db: Session = Depends(get_db),
    _user: User = Depends(can_operate),
) -> dict:
    item = db.get(QuotationItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="报价明细不存在")
    quotation = _quotation_or_404(db, item.quotation_id)
    if quotation.status != "accepted":
        raise HTTPException(status_code=409, detail="请先将报价标记为客户接受")
    if item.converted_product_id is not None:
        raise HTTPException(status_code=409, detail="该报价明细已经转入常用箱")
    product_code = payload.product_code.strip()
    duplicate = db.scalar(
        select(Product.id).where(
            Product.customer_id == quotation.customer_id,
            or_(
                Product.product_code == product_code,
                Product.customer_material_code == product_code,
            ),
        )
    )
    if duplicate is not None:
        raise HTTPException(status_code=409, detail="该客户已存在相同存货编码")
    material = _material_or_none(db, item.material_id)
    is_a1 = _is_a1(item.box_type)
    report_length = None
    report_width = None
    if is_a1 and item.length_mm and item.width_mm and item.height_mm:
        report_length = round(2 * (item.length_mm + item.width_mm) + 30)
        report_width = round(item.width_mm + item.height_mm + 5)
    product = Product(
        customer_id=quotation.customer_id,
        product_code=product_code,
        customer_material_code=product_code,
        product_name=(payload.product_name or item.product_name).strip(),
        material_id=item.material_id,
        legacy_material_text=item.material_code,
        length_mm=item.length_mm,
        width_mm=item.width_mm,
        height_mm=item.height_mm,
        box_category="die_cut" if "异形" in item.box_type else "normal",
        box_style=item.box_type,
        unit="只",
        sale_unit_price=item.final_unit_price,
        cost_unit_price=item.estimated_unit_cost,
        board_price=material.quote_price if material else None,
        suggested_price=item.suggested_unit_price,
        flute_type=item.flute_type,
        layer_count=material.layer_count if material else None,
        report_length_mm=report_length,
        report_width_mm=report_width,
        splice_mode="single",
        pieces_per_box=1,
        flap_mm=30,
        remark=item.remarks,
        is_active=True,
    )
    db.add(product)
    db.flush()
    item.converted_product_id = product.id
    if all(row.converted_product_id is not None for row in quotation.items):
        quotation.status = "converted"
    db.commit()
    return {
        "product_id": product.id,
        "product_code": product.product_code,
        "quotation_id": quotation.id,
        "quotation_status": quotation.status,
    }


@router.get("/{quotation_id}/print")
def quotation_print(
    quotation_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    quotation = _quotation_or_404(db, quotation_id)
    company = db.get(CompanyConfig, 1)
    items = []
    for index, item in enumerate(quotation.items, start=1):
        quantity = item.quantity
        amount = (
            (item.final_unit_price * quantity).quantize(MONEY)
            if quantity
            else None
        )
        specification = "×".join(
            str(int(value)) if value == int(value) else str(value)
            for value in (item.length_mm, item.width_mm, item.height_mm)
            if value is not None
        )
        material = item.material_code or ""
        if item.flute_type:
            material = f"{material} / {item.flute_type}" if material else item.flute_type
        items.append(
            {
                "sequence": index,
                "product_name": item.product_name,
                "box_type": item.box_type,
                "specification": specification,
                "material": material,
                "quantity": quantity,
                "unit_price": item.final_unit_price,
                "amount": amount,
                "remarks": item.remarks,
            }
        )
    return {
        "quotation_no": quotation.quotation_no,
        "quotation_date": quotation.quotation_date,
        "customer_name": quotation.customer_name,
        "status": quotation.status,
        "total_amount": quotation.total_amount,
        "remarks": quotation.remarks,
        "items": items,
        "sender": {
            "company_name": company.company_name if company else "",
            "address": company.address if company else None,
            "phone": company.phone if company else None,
            "contact_person": company.contact_person if company else None,
        },
    }
