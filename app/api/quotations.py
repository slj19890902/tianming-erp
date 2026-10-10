from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session, selectinload

from app.api.deps import (
    PermissionChecker,
    get_db,
    has_permission,
    require_customer_access,
)
from app.models.company_config import CompanyConfig
from app.models.customer import Customer
from app.models.customer_quote_preference import CustomerQuotePreference
from app.models.material import Material
from app.models.product import Product
from app.models.quotation import QuotationItem, QuotationOrder
from app.models.user import User
from app.services import material_pricing
from app.services.customer_quote_pricing import (
    CustomerQuotePricingError,
    a1_area_m2,
    canonical_quote_box_type,
    estimate_a1_unit_price,
    resolve_customer_square_price,
)
from app.services.box_type_rules import (
    BoxTypeRuleError,
    box_type_code,
    canonical_box_style,
    normalize_box_configuration,
    recommend_box_type,
)
from app.services.flute_mapping import normalize_flute_type, validate_flute_consistency
from app.services.report_crease import crease_width_error


router = APIRouter()
can_read = PermissionChecker("quotations.view")
can_operate = PermissionChecker("quotations.edit")
can_create_product = PermissionChecker("products.create")
MONEY = Decimal("0.01")
PRICE = Decimal("0.0001")
INTERNAL_PRICING_FIELDS = frozenset(
    {
        "estimated_unit_cost",
        "estimated_gross_profit",
        "margin_rate",
        "material_square_price",
        "material_effective_square_price",
        "suggested_unit_price",
    }
)
STATUS_LABELS = {
    "draft": "草稿",
    "quoted": "已报价",
    "accepted": "客户接受",
    "converted": "已转常用箱",
    "voided": "作废",
}


class QuotationPreviewPayload(BaseModel):
    customer_id: int | None = Field(default=None, gt=0)
    box_type: str
    crease_type: str = Field(default="压线", min_length=1, max_length=20)
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
    flute_type: str | None = Field(default=None, max_length=20)
    splice_mode: str | None = Field(default=None, max_length=20)
    flap_mm: int | None = Field(default=None, ge=0)
    report_length_mm: int | None = Field(default=None, gt=0)
    report_width_mm: int | None = Field(default=None, gt=0)
    crease_type: str | None = Field(default=None, max_length=20)
    crease_left_mm: int | None = Field(default=None, ge=0)
    crease_middle_mm: int | None = Field(default=None, gt=0)
    crease_right_mm: int | None = Field(default=None, ge=0)
    report_notes: str | None = None
    base_report_length_mm: int | None = Field(default=None, gt=0)
    base_report_width_mm: int | None = Field(default=None, gt=0)
    base_crease_type: str | None = Field(default=None, max_length=20)
    base_crease_left_mm: int | None = Field(default=None, ge=0)
    base_crease_middle_mm: int | None = Field(default=None, gt=0)
    base_crease_right_mm: int | None = Field(default=None, ge=0)
    base_report_notes: str | None = None

    @field_validator("product_code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        code = value.strip()
        if not code:
            raise ValueError("存货编码不能为空")
        return code


def _validated_quotation_flute(
    layer_count: int | None,
    flute_type: str | None,
) -> str | None:
    """Validate a manually supplied business flute without using material defaults."""
    normalized_flute = normalize_flute_type(flute_type)
    error = validate_flute_consistency(normalized_flute, layer_count)
    if error is None and layer_count == 7 and normalized_flute not in {"AAA", "ABC"}:
        error = "七层瓦楞必须人工选择 AAA 或 ABC，不能为空或使用三层/五层楞型"
    if error:
        raise HTTPException(status_code=400, detail=error)
    return normalized_flute


def _is_a1(box_type: str | None) -> bool:
    return box_type_code(box_type) == "a1_0201"


def _is_a3(box_type: str | None) -> bool:
    return box_type_code(box_type) == "a3_set"


def _round_mm(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _quotation_report_values(item: QuotationItem, payload: ConvertPayload) -> dict:
    main_fields = (
        "report_length_mm",
        "report_width_mm",
        "crease_type",
        "crease_left_mm",
        "crease_middle_mm",
        "crease_right_mm",
    )
    base_fields = (
        "base_report_length_mm",
        "base_report_width_mm",
        "base_crease_type",
        "base_crease_left_mm",
        "base_crease_middle_mm",
        "base_crease_right_mm",
    )
    values = {name: getattr(payload, name) for name in (*main_fields, *base_fields)}
    is_a3 = _is_a3(item.box_type)
    manual_report_started = any(values[name] is not None for name in (*main_fields, *base_fields))

    try:
        configuration = normalize_box_configuration(
            box_style=item.box_type,
            splice_mode=payload.splice_mode,
            pieces_per_box=None,
            flap_mm=payload.flap_mm,
            default_cutting_mode="一开一",
            crease_type=payload.crease_type,
        )
    except BoxTypeRuleError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    dimensions = (
        _round_mm(item.length_mm) if item.length_mm is not None else None,
        _round_mm(item.width_mm) if item.width_mm is not None else None,
        _round_mm(item.height_mm) if item.height_mm is not None else None,
    )

    if not manual_report_started:
        length_mm, width_mm, height_mm = dimensions
        try:
            recommendation = recommend_box_type(
                box_style=item.box_type,
                length_mm=length_mm,
                width_mm=width_mm,
                height_mm=height_mm,
                splice_mode=str(configuration["splice_mode"]),
                flap_mm=configuration["flap_mm"],
                crease_type=payload.crease_type,
            )
        except BoxTypeRuleError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if recommendation["auto_calculated"]:
            for name in (*main_fields, *base_fields):
                values[name] = recommendation[name]
            configuration.update(
                splice_mode=recommendation["splice_mode"],
                pieces_per_box=recommendation["pieces_per_box"],
                flap_mm=recommendation["flap_mm"],
            )

    if values["report_length_mm"] is None or values["report_width_mm"] is None:
        raise HTTPException(
            status_code=400,
            detail="请确认单片报料长宽后再转入常用箱",
        )
    if not (values["crease_type"] or "").strip():
        raise HTTPException(status_code=400, detail="请选择压线类型后再转入常用箱")
    error = crease_width_error(
        label="压线",
        crease_type=values["crease_type"],
        report_width_mm=values["report_width_mm"],
        left_mm=values["crease_left_mm"],
        middle_mm=values["crease_middle_mm"],
        right_mm=values["crease_right_mm"],
    )
    if error:
        raise HTTPException(status_code=400, detail=error)

    if is_a3:
        if values["base_report_length_mm"] is None or values["base_report_width_mm"] is None:
            raise HTTPException(status_code=400, detail="请确认天地盖底料报料长宽")
        if not (values["base_crease_type"] or "").strip():
            raise HTTPException(status_code=400, detail="请选择天地盖底压线类型")
        error = crease_width_error(
            label="底压线",
            crease_type=values["base_crease_type"],
            report_width_mm=values["base_report_width_mm"],
            left_mm=values["base_crease_left_mm"],
            middle_mm=values["base_crease_middle_mm"],
            right_mm=values["base_crease_right_mm"],
        )
        if error:
            raise HTTPException(status_code=400, detail=error)
    else:
        for name in base_fields:
            values[name] = None

    if values["crease_type"] != "压线":
        for name in ("crease_left_mm", "crease_middle_mm", "crease_right_mm"):
            values[name] = None
    if values["base_crease_type"] != "压线":
        for name in (
            "base_crease_left_mm",
            "base_crease_middle_mm",
            "base_crease_right_mm",
        ):
            values[name] = None

    return {
        **values,
        "splice_mode": configuration["splice_mode"],
        "pieces_per_box": configuration["pieces_per_box"],
        "flap_mm": configuration["flap_mm"],
        "report_notes": payload.report_notes,
        "base_report_notes": payload.base_report_notes,
    }


def _material_or_none(db: Session, material_id: int | None) -> Material | None:
    if material_id is None:
        return None
    material = db.get(Material, material_id)
    if material is None or not material.is_active:
        raise HTTPException(status_code=400, detail="所选材质不存在或已停用")
    return material


def _preview(db: Session, payload: QuotationPreviewPayload) -> dict:
    material = _material_or_none(db, payload.material_id)
    flute_type = (
        _validated_quotation_flute(material.layer_count, payload.flute_type)
        if material is not None
        else None
    )
    if not _is_a1(payload.box_type):
        return {
            "auto_calculated": False,
            "estimated_unit_cost": None,
            "suggested_unit_price": None,
            "margin_rate": payload.margin_rate,
            "message": "当前箱型暂无自动报价公式，请手工填写单价",
        }
    if material is None:
        return {
            "auto_calculated": False,
            "estimated_unit_cost": None,
            "suggested_unit_price": None,
            "margin_rate": payload.margin_rate,
            "message": "请选择材质后计算建议单价",
        }
    if payload.customer_id is not None:
        preference = db.scalar(
            select(CustomerQuotePreference)
            .where(
                CustomerQuotePreference.customer_id == payload.customer_id,
                CustomerQuotePreference.box_type
                == canonical_quote_box_type(payload.box_type),
                CustomerQuotePreference.crease_type == payload.crease_type.strip(),
                CustomerQuotePreference.material_id == material.id,
                CustomerQuotePreference.flute_type == flute_type,
                CustomerQuotePreference.is_active.is_(True),
            )
            .order_by(CustomerQuotePreference.id.desc())
        )
        effective = material_pricing.get_effective_material_price(
            db, material=material, flute_type=flute_type
        )
        material_square_price = effective.get("effective_price")
        if material_square_price is None:
            return {
                "auto_calculated": False,
                "estimated_unit_cost": None,
                "suggested_unit_price": None,
                "margin_rate": payload.margin_rate,
                "message": "当前材质缺少平方价，请手工填写最终单价",
            }
        try:
            customer_square = resolve_customer_square_price(
                db,
                material=material,
                flute_type=flute_type or "",
                saved_square_price=(
                    preference.tax_included_square_price
                    if preference is not None
                    else None
                ),
            )
            area = a1_area_m2(
                length_mm=Decimal(payload.length_mm),
                width_mm=Decimal(payload.width_mm),
                height_mm=Decimal(payload.height_mm),
            )
            suggested = estimate_a1_unit_price(
                length_mm=Decimal(payload.length_mm),
                width_mm=Decimal(payload.width_mm),
                height_mm=Decimal(payload.height_mm),
                customer_square_price=customer_square["customer_square_price"],
            )
        except (CustomerQuotePricingError, TypeError) as error:
            return {
                "auto_calculated": False,
                "estimated_unit_cost": None,
                "suggested_unit_price": None,
                "margin_rate": payload.margin_rate,
                "message": str(error),
            }
        cost = (area * Decimal(str(material_square_price))).quantize(
            PRICE, rounding=ROUND_HALF_UP
        )
        return {
            "auto_calculated": True,
            "estimated_unit_cost": cost,
            "suggested_unit_price": suggested["estimated_unit_price"],
            "margin_rate": payload.margin_rate,
            "area_m2": area,
            "material_square_price": material_square_price,
            "material_effective_square_price": material_square_price,
            "customer_square_price": customer_square["customer_square_price"],
            "price_source": customer_square["price_source"],
            "preference_id": preference.id if preference is not None else None,
            "message": "已按客户尺寸报价偏好计算，最终单价可手工修改",
        }
    effective = material_pricing.get_effective_material_price(
        db,
        material=material,
        flute_type=flute_type,
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
        area = a1_area_m2(
            length_mm=Decimal(payload.length_mm),
            width_mm=Decimal(payload.width_mm),
            height_mm=Decimal(payload.height_mm),
        )
    except (CustomerQuotePricingError, TypeError) as error:
        return {
            "auto_calculated": False,
            "estimated_unit_cost": None,
            "suggested_unit_price": None,
            "margin_rate": payload.margin_rate,
            "message": str(error),
        }
    cost = (area * Decimal(str(square_price))).quantize(
        PRICE,
        rounding=ROUND_HALF_UP,
    )
    margin_fraction = payload.margin_rate / Decimal("100")
    suggested = (cost / (Decimal("1") - margin_fraction)).quantize(
        PRICE, rounding=ROUND_HALF_UP
    )
    return {
        "auto_calculated": True,
        "estimated_unit_cost": cost,
        "suggested_unit_price": suggested,
        "margin_rate": payload.margin_rate,
        "area_m2": area,
        "material_square_price": square_price,
        "message": "已按 A1 尺寸平方价口径计算，建议单价可手工修改",
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
    *,
    customer_id: int | None = None,
) -> None:
    quotation.items.clear()
    total = Decimal("0")
    for payload in payloads:
        material = _material_or_none(db, payload.material_id)
        flute_type = (
            _validated_quotation_flute(material.layer_count, payload.flute_type)
            if material is not None
            else None
        )
        preview = _preview(
            db,
            payload.model_copy(update={"customer_id": customer_id}),
        )
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
            flute_type=flute_type,
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


def _redact_internal_pricing(payload: dict, user: User) -> dict:
    if has_permission(user, "cost.view"):
        return payload
    return {
        key: value
        for key, value in payload.items()
        if key not in INTERNAL_PRICING_FIELDS
    }


def _item_dict(item: QuotationItem, user: User) -> dict:
    amount = (
        (item.final_unit_price * item.quantity).quantize(MONEY)
        if item.quantity
        else None
    )
    return _redact_internal_pricing(
        {
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
        },
        user,
    )


def _quotation_dict(quotation: QuotationOrder, user: User) -> dict:
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
        "items": [_item_dict(item, user) for item in quotation.items],
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
    user: User = Depends(can_read),
) -> dict:
    if payload.customer_id is not None:
        require_customer_access(payload.customer_id, current_user=user, db=db)
    return _redact_internal_pricing(_preview(db, payload), user)


@router.get("")
def list_quotations(
    customer_id: int | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    query = select(QuotationOrder).options(selectinload(QuotationOrder.items))
    if customer_id is not None:
        require_customer_access(customer_id, current_user=user, db=db)
        query = query.where(QuotationOrder.customer_id == customer_id)
    if status_filter:
        query = query.where(QuotationOrder.status == status_filter)
    quotations = db.scalars(
        query.order_by(
            QuotationOrder.quotation_date.desc(),
            QuotationOrder.id.desc(),
        )
    ).all()
    visible_quotations = []
    for quotation in quotations:
        try:
            require_customer_access(
                quotation.customer_id,
                current_user=user,
                db=db,
            )
        except HTTPException as error:
            if error.status_code == status.HTTP_403_FORBIDDEN:
                continue
            raise
        visible_quotations.append(quotation)
    return {
        "items": [_quotation_dict(row, user) for row in visible_quotations],
        "total": len(visible_quotations),
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_quotation(
    customer_id: int,
    payload: QuotationPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    customer = db.get(Customer, customer_id)
    require_customer_access(customer_id, current_user=user, db=db)
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
    _replace_items(db, quotation, payload.items, customer_id=quotation.customer_id)
    db.add(quotation)
    db.commit()
    db.refresh(quotation)
    return _quotation_dict(_quotation_or_404(db, quotation.id), user)


@router.get("/{quotation_id}")
def get_quotation(
    quotation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    quotation = _quotation_or_404(db, quotation_id)
    require_customer_access(quotation.customer_id, current_user=user, db=db)
    return _quotation_dict(quotation, user)


@router.put("/{quotation_id}")
def update_quotation(
    quotation_id: int,
    payload: QuotationPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    quotation = _quotation_or_404(db, quotation_id)
    require_customer_access(quotation.customer_id, current_user=user, db=db)
    if quotation.status not in {"draft", "quoted"}:
        raise HTTPException(status_code=409, detail="客户已接受、已转常用箱或已作废报价不能修改")
    quotation.quotation_date = payload.quotation_date
    quotation.remarks = (payload.remarks or "").strip() or None
    quotation.status = "draft"
    _replace_items(db, quotation, payload.items, customer_id=quotation.customer_id)
    db.commit()
    return _quotation_dict(_quotation_or_404(db, quotation.id), user)


@router.post("/{quotation_id}/generate")
def generate_quotation(
    quotation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    quotation = _quotation_or_404(db, quotation_id)
    require_customer_access(quotation.customer_id, current_user=user, db=db)
    if quotation.status not in {"draft", "quoted"}:
        raise HTTPException(status_code=409, detail="当前报价状态不能重新生成")
    if not quotation.items:
        raise HTTPException(status_code=400, detail="报价单至少需要一条明细")
    quotation.status = "quoted"
    db.commit()
    return _quotation_dict(_quotation_or_404(db, quotation.id), user)


@router.post("/{quotation_id}/accept")
def accept_quotation(
    quotation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    quotation = _quotation_or_404(db, quotation_id)
    require_customer_access(quotation.customer_id, current_user=user, db=db)
    if quotation.status != "quoted":
        raise HTTPException(status_code=409, detail="只有已报价状态可以标记客户接受")
    quotation.status = "accepted"
    db.commit()
    return _quotation_dict(_quotation_or_404(db, quotation.id), user)


@router.post("/{quotation_id}/void")
def void_quotation(
    quotation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    quotation = _quotation_or_404(db, quotation_id)
    require_customer_access(quotation.customer_id, current_user=user, db=db)
    if quotation.status == "converted" or any(
        item.converted_product_id is not None for item in quotation.items
    ):
        raise HTTPException(status_code=409, detail="已有明细转入常用箱，报价不能作废")
    quotation.status = "voided"
    db.commit()
    return _quotation_dict(_quotation_or_404(db, quotation.id), user)


@router.post("/items/{item_id}/convert-to-product", status_code=status.HTTP_201_CREATED)
def convert_to_product(
    item_id: int,
    payload: ConvertPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
    _product_creator: User = Depends(can_create_product),
) -> dict:
    item = db.get(QuotationItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="报价明细不存在")
    quotation = _quotation_or_404(db, item.quotation_id)
    require_customer_access(quotation.customer_id, current_user=user, db=db)
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
    if material is None:
        raise HTTPException(status_code=400, detail="请先在报价明细中选择材质")
    if not (material.supplier_name or "").strip():
        raise HTTPException(status_code=400, detail="所选材质缺少供应商，请先完善材质资料")
    if material.layer_count not in {3, 5, 7}:
        raise HTTPException(status_code=400, detail="所选材质缺少有效层数，请先完善材质资料")
    flute_type = _validated_quotation_flute(
        material.layer_count,
        payload.flute_type or item.flute_type,
    )
    if not flute_type:
        expected = {
            3: "A、B 或 E",
            5: "AB 或 BE",
            7: "AAA 或 ABC",
        }[material.layer_count]
        raise HTTPException(
            status_code=400,
            detail=f"报价明细缺少有效楞型，请先选择{expected}后再转入常用箱",
        )
    if item.final_unit_price is None:
        raise HTTPException(status_code=400, detail="报价明细缺少最终单价，不能转入常用箱")
    report_values = _quotation_report_values(item, payload)
    cost_values = (
        {
            "cost_unit_price": item.estimated_unit_cost,
            "board_price": material.quote_price,
            "suggested_price": item.suggested_unit_price,
        }
        if has_permission(user, "cost.view")
        else {}
    )
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
        box_style=canonical_box_style(item.box_type),
        unit="只",
        sale_unit_price=item.final_unit_price,
        **cost_values,
        flute_type=flute_type,
        layer_count=material.layer_count,
        report_length_mm=report_values["report_length_mm"],
        report_width_mm=report_values["report_width_mm"],
        crease_type=report_values["crease_type"],
        crease_left_mm=report_values["crease_left_mm"],
        crease_middle_mm=report_values["crease_middle_mm"],
        crease_right_mm=report_values["crease_right_mm"],
        report_notes=report_values["report_notes"],
        base_report_length_mm=report_values["base_report_length_mm"],
        base_report_width_mm=report_values["base_report_width_mm"],
        base_crease_type=report_values["base_crease_type"],
        base_crease_left_mm=report_values["base_crease_left_mm"],
        base_crease_middle_mm=report_values["base_crease_middle_mm"],
        base_crease_right_mm=report_values["base_crease_right_mm"],
        base_report_notes=report_values["base_report_notes"],
        splice_mode=report_values["splice_mode"],
        pieces_per_box=report_values["pieces_per_box"],
        flap_mm=report_values["flap_mm"],
        remark=item.remarks,
        is_active=True,
    )
    db.add(product)
    db.flush()
    from app.services.master_data_versioning import record_versioned_create

    record_versioned_create(
        db,
        object_type="product",
        entity=product,
        user=user,
        reason="报价转常用箱创建主档",
        source="quotations.convert-to-product",
    )
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
    user: User = Depends(can_read),
) -> dict:
    quotation = _quotation_or_404(db, quotation_id)
    require_customer_access(quotation.customer_id, current_user=user, db=db)
    company = db.get(CompanyConfig, 1)
    items = []
    for index, item in enumerate(quotation.items, start=1):
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
                "temporary_code": item.temporary_code,
                "product_name": item.product_name,
                "specification": specification,
                "material": material,
                "quantity": item.quantity,
                "unit_price": item.final_unit_price,
                "remarks": item.remarks,
            }
        )
    return {
        "quotation_no": quotation.quotation_no,
        "quotation_date": quotation.quotation_date,
        "customer_name": quotation.customer_name,
        "status": quotation.status,
        "remarks": quotation.remarks,
        "items": items,
        "sender": {
            "company_name": company.company_name if company else "",
            "address": company.address if company else None,
            "phone": company.phone if company else None,
            "contact_person": company.contact_person if company else None,
        },
    }
