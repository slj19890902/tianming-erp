from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from phase1_postgres.database import get_session
from phase1_postgres.models import Customer, FluteType, Material, Product


router = APIRouter(prefix="/api/master", tags=["master-data"])


class CustomerPayload(BaseModel):
    customer_number: int | None = Field(default=None, ge=0)
    customer_code: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=255)
    short_name: str | None = None
    contact_person: str | None = None
    phone: str | None = None
    mobile: str | None = None
    address: str | None = None
    payment_term_days: int = Field(default=30, ge=0)
    delivery_method: str = Field(default="配送", pattern="^(自提|配送|物流)$")
    default_tax_rate: Decimal = Field(default=Decimal("0.13"), ge=0)
    invoice_title: str | None = None
    tax_number: str | None = None
    note: str | None = None
    is_active: bool = True


class CustomerStatusPayload(BaseModel):
    is_active: bool


class FluteTypePayload(BaseModel):
    code: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=100)
    add_width_mm: Decimal = Decimal("0")
    basis_weight_gsm: Decimal | None = None
    freight_rate: Decimal | None = None
    loss_rate: Decimal = Decimal("0")
    note: str | None = None
    is_active: bool = True


class MaterialPayload(BaseModel):
    code: str = Field(min_length=1, max_length=100)
    name: str | None = None
    paper_composition: str | None = None
    basis_weight_description: str | None = None
    layer_count: int | None = Field(default=None, ge=1)
    flute_type_id: int | None = None
    customer_square_price: Decimal | None = Field(default=None, ge=0)
    supplier_square_price: Decimal | None = Field(default=None, ge=0)
    note: str | None = None
    is_active: bool = True


class ProductPayload(BaseModel):
    customer_id: int
    product_code: str = Field(min_length=1, max_length=120)
    customer_material_code: str = Field(min_length=1, max_length=120)
    product_name: str = Field(min_length=1, max_length=255)
    material_id: int | None = None
    default_material_text: str | None = None
    flute_type_id: int | None = None
    length_mm: Decimal | None = Field(default=None, gt=0)
    width_mm: Decimal | None = Field(default=None, gt=0)
    height_mm: Decimal | None = Field(default=None, gt=0)
    box_category: str = Field(default="normal", pattern="^(normal|die_cut)$")
    box_style: str | None = None
    print_content: str | None = None
    production_process: str | None = None
    default_score_line: str | None = None
    default_cardboard_length_mm: Decimal | None = Field(default=None, gt=0)
    default_cardboard_width_mm: Decimal | None = Field(default=None, gt=0)
    default_pieces_per_sheet: int = Field(default=1, ge=1)
    default_unit_price: Decimal | None = Field(default=None, ge=0)
    note: str | None = None
    is_active: bool = True


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = " ".join(value.strip().split())
    return text or None


def _code(value: str) -> str:
    return " ".join(value.strip().upper().split())


def _page_response(total: int, page: int, page_size: int, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"total": total, "page": page, "page_size": page_size, "items": items}


def _decimal(value: Any) -> str | None:
    return None if value is None else str(value)


def customer_dict(item: Customer) -> dict[str, Any]:
    return {
        "id": item.id,
        "customer_number": item.customer_number,
        "customer_code": item.customer_code,
        "name": item.name,
        "short_name": item.short_name,
        "contact_person": item.contact_person,
        "phone": item.phone,
        "mobile": item.mobile,
        "address": item.address,
        "payment_term_days": item.payment_term_days,
        "delivery_method": item.delivery_method,
        "default_tax_rate": _decimal(item.default_tax_rate),
        "invoice_title": item.invoice_title,
        "tax_number": item.tax_number,
        "note": item.note,
        "is_active": item.is_active,
    }


def flute_type_dict(item: FluteType) -> dict[str, Any]:
    return {
        "id": item.id,
        "code": item.code,
        "name": item.name,
        "add_width_mm": _decimal(item.add_width_mm),
        "basis_weight_gsm": _decimal(item.basis_weight_gsm),
        "freight_rate": _decimal(item.freight_rate),
        "loss_rate": _decimal(item.loss_rate),
        "note": item.note,
        "is_active": item.is_active,
    }


def material_dict(item: Material) -> dict[str, Any]:
    return {
        "id": item.id,
        "code": item.code,
        "name": item.name,
        "paper_composition": item.paper_composition,
        "basis_weight_description": item.basis_weight_description,
        "layer_count": item.layer_count,
        "flute_type_id": item.flute_type_id,
        "flute_type_code": item.flute_type.code if item.flute_type else None,
        "customer_square_price": _decimal(item.customer_square_price),
        "supplier_square_price": _decimal(item.supplier_square_price),
        "note": item.note,
        "is_active": item.is_active,
    }


def product_dict(item: Product) -> dict[str, Any]:
    return {
        "id": item.id,
        "customer_id": item.customer_id,
        "customer_name": item.customer.name if item.customer else None,
        "product_code": item.product_code,
        "customer_material_code": item.customer_material_code,
        "product_name": item.product_name,
        "material_id": item.material_id,
        "material_code": item.material.code if item.material else item.default_material_text,
        "default_material_text": item.default_material_text,
        "flute_type_id": item.flute_type_id,
        "flute_type_code": item.flute_type.code if item.flute_type else None,
        "length_mm": _decimal(item.length_mm),
        "width_mm": _decimal(item.width_mm),
        "height_mm": _decimal(item.height_mm),
        "box_category": item.box_category,
        "box_style": item.box_style,
        "print_content": item.print_content,
        "production_process": item.production_process,
        "default_score_line": item.default_score_line,
        "default_cardboard_length_mm": _decimal(item.default_cardboard_length_mm),
        "default_cardboard_width_mm": _decimal(item.default_cardboard_width_mm),
        "default_pieces_per_sheet": item.default_pieces_per_sheet,
        "default_unit_price": _decimal(item.default_unit_price),
        "historical_search_key": item.historical_search_key,
        "historical_style_no": item.historical_style_no,
        "historical_material_code": item.historical_material_code,
        "note": item.note,
        "is_active": item.is_active,
    }


def _commit_or_conflict(session: Session, detail: str) -> None:
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail=detail) from error


def _not_found(model_name: str):
    raise HTTPException(status_code=404, detail=f"{model_name}不存在")


@router.get("/customers")
def list_customers(
    keyword: str = "",
    include_inactive: bool = False,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    query = select(Customer).order_by(Customer.customer_number.nulls_last(), Customer.id)
    if not include_inactive:
        query = query.where(Customer.is_active.is_(True))
    if keyword.strip():
        pattern = f"%{keyword.strip()}%"
        query = query.where(or_(Customer.name.like(pattern), Customer.customer_code.like(pattern), Customer.short_name.like(pattern)))
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    items = session.scalars(query.offset((page - 1) * page_size).limit(page_size)).all()
    return _page_response(total, page, page_size, [customer_dict(item) for item in items])


@router.post("/customers", status_code=status.HTTP_201_CREATED)
def create_customer(payload: CustomerPayload, session: Session = Depends(get_session)) -> dict[str, Any]:
    data = payload.model_dump()
    data["customer_code"] = _code(payload.customer_code)
    data["name"] = _clean(payload.name) or payload.name
    customer = Customer(**data)
    session.add(customer)
    session.flush()
    _commit_or_conflict(session, "客户编号、编码或名称重复")
    session.refresh(customer)
    return customer_dict(customer)


@router.put("/customers/{customer_id}")
def update_customer(customer_id: int, payload: CustomerPayload, session: Session = Depends(get_session)) -> dict[str, Any]:
    customer = session.get(Customer, customer_id)
    if customer is None:
        _not_found("客户")
    for key, value in payload.model_dump().items():
        setattr(customer, key, value)
    customer.customer_code = _code(payload.customer_code)
    customer.name = _clean(payload.name) or payload.name
    _commit_or_conflict(session, "客户编号、编码或名称重复")
    session.refresh(customer)
    return customer_dict(customer)


@router.put("/customers/{customer_id}/status")
def update_customer_status(
    customer_id: int,
    payload: CustomerStatusPayload,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    customer = session.get(Customer, customer_id)
    if customer is None:
        _not_found("客户")
    customer.is_active = payload.is_active
    session.commit()
    session.refresh(customer)
    return customer_dict(customer)


@router.delete("/customers/{customer_id}", status_code=status.HTTP_204_NO_CONTENT)
def disable_customer(customer_id: int, session: Session = Depends(get_session)) -> Response:
    customer = session.get(Customer, customer_id)
    if customer is None:
        _not_found("客户")
    customer.is_active = False
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/flute-types")
def list_flute_types(
    keyword: str = "",
    include_inactive: bool = False,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=200),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    query = select(FluteType).order_by(FluteType.code)
    if not include_inactive:
        query = query.where(FluteType.is_active.is_(True))
    if keyword.strip():
        pattern = f"%{keyword.strip()}%"
        query = query.where(or_(FluteType.code.like(pattern), FluteType.name.like(pattern)))
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    items = session.scalars(query.offset((page - 1) * page_size).limit(page_size)).all()
    return _page_response(total, page, page_size, [flute_type_dict(item) for item in items])


@router.post("/flute-types", status_code=status.HTTP_201_CREATED)
def create_flute_type(payload: FluteTypePayload, session: Session = Depends(get_session)) -> dict[str, Any]:
    flute = FluteType(**{**payload.model_dump(), "code": _code(payload.code), "name": _clean(payload.name) or payload.name})
    session.add(flute)
    session.flush()
    _commit_or_conflict(session, "楞型编码重复")
    session.refresh(flute)
    return flute_type_dict(flute)


@router.put("/flute-types/{flute_type_id}")
def update_flute_type(flute_type_id: int, payload: FluteTypePayload, session: Session = Depends(get_session)) -> dict[str, Any]:
    flute = session.get(FluteType, flute_type_id)
    if flute is None:
        _not_found("楞型")
    for key, value in payload.model_dump().items():
        setattr(flute, key, value)
    flute.code = _code(payload.code)
    flute.name = _clean(payload.name) or payload.name
    _commit_or_conflict(session, "楞型编码重复")
    session.refresh(flute)
    return flute_type_dict(flute)


@router.delete("/flute-types/{flute_type_id}", status_code=status.HTTP_204_NO_CONTENT)
def disable_flute_type(flute_type_id: int, session: Session = Depends(get_session)) -> Response:
    flute = session.get(FluteType, flute_type_id)
    if flute is None:
        _not_found("楞型")
    flute.is_active = False
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/materials")
def list_materials(
    keyword: str = "",
    include_inactive: bool = False,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=200),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    query = select(Material).order_by(Material.code)
    if not include_inactive:
        query = query.where(Material.is_active.is_(True))
    if keyword.strip():
        pattern = f"%{keyword.strip()}%"
        query = query.where(
            or_(
                Material.code.like(pattern),
                Material.name.like(pattern),
                Material.paper_composition.like(pattern),
                Material.basis_weight_description.like(pattern),
            )
        )
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    items = session.scalars(query.offset((page - 1) * page_size).limit(page_size)).all()
    return _page_response(total, page, page_size, [material_dict(item) for item in items])


@router.post("/materials", status_code=status.HTTP_201_CREATED)
def create_material(payload: MaterialPayload, session: Session = Depends(get_session)) -> dict[str, Any]:
    if payload.flute_type_id is not None and session.get(FluteType, payload.flute_type_id) is None:
        raise HTTPException(status_code=400, detail="楞型不存在")
    material = Material(**{**payload.model_dump(), "code": _code(payload.code)})
    session.add(material)
    session.flush()
    _commit_or_conflict(session, "材质编码重复")
    session.refresh(material)
    return material_dict(material)


@router.put("/materials/{material_id}")
def update_material(material_id: int, payload: MaterialPayload, session: Session = Depends(get_session)) -> dict[str, Any]:
    material = session.get(Material, material_id)
    if material is None:
        _not_found("材质")
    if payload.flute_type_id is not None and session.get(FluteType, payload.flute_type_id) is None:
        raise HTTPException(status_code=400, detail="楞型不存在")
    for key, value in payload.model_dump().items():
        setattr(material, key, value)
    material.code = _code(payload.code)
    _commit_or_conflict(session, "材质编码重复")
    session.refresh(material)
    return material_dict(material)


@router.delete("/materials/{material_id}", status_code=status.HTTP_204_NO_CONTENT)
def disable_material(material_id: int, session: Session = Depends(get_session)) -> Response:
    material = session.get(Material, material_id)
    if material is None:
        _not_found("材质")
    material.is_active = False
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/products")
def list_products(
    customer_id: int | None = None,
    keyword: str = "",
    include_inactive: bool = False,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=200),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    query = select(Product).join(Customer).outerjoin(Material).outerjoin(FluteType).order_by(Customer.name, Product.product_code)
    if customer_id is not None:
        query = query.where(Product.customer_id == customer_id)
    if not include_inactive:
        query = query.where(Product.is_active.is_(True))
    for token in keyword.split():
        pattern = f"%{token}%"
        query = query.where(
            or_(
                Product.product_code.like(pattern),
                Product.customer_material_code.like(pattern),
                Product.product_name.like(pattern),
                Product.default_material_text.like(pattern),
                Customer.name.like(pattern),
                Material.code.like(pattern),
                FluteType.code.like(pattern),
                cast(Product.length_mm, String).like(pattern),
                cast(Product.width_mm, String).like(pattern),
                cast(Product.height_mm, String).like(pattern),
            )
        )
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    items = session.scalars(query.offset((page - 1) * page_size).limit(page_size)).unique().all()
    return _page_response(total, page, page_size, [product_dict(item) for item in items])


def product_history_dict(item: Product) -> dict[str, Any]:
    return {
        "id": item.id,
        "customer_id": item.customer_id,
        "customer_name": item.customer.name if item.customer else None,
        "historical_search_key": item.historical_search_key or item.product_name,
        "style_no": item.historical_style_no or item.product_code,
        "product_name": item.product_name,
        "paper_length_mm": _decimal(item.default_cardboard_length_mm),
        "paper_width_mm": _decimal(item.default_cardboard_width_mm),
        "score_line": item.default_score_line,
        "material": item.historical_material_code or item.default_material_text or (item.material.code if item.material else None),
    }


@router.get("/products/history-search")
def search_product_history(
    keyword: str,
    limit: int = Query(default=10, ge=1, le=30),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    tokens = [token for token in keyword.strip().split() if token]
    if not tokens and keyword.strip():
        tokens = [keyword.strip()]
    if not tokens:
        raise HTTPException(status_code=400, detail="请输入K列关键词")

    query = select(Product).join(Customer).outerjoin(Material).where(Product.is_active.is_(True))
    for token in tokens:
        pattern = f"%{token}%"
        query = query.where(
            or_(
                Product.historical_search_key.like(pattern),
                Product.historical_style_no.like(pattern),
                Product.product_code.like(pattern),
                Product.customer_material_code.like(pattern),
                Product.product_name.like(pattern),
                Product.default_material_text.like(pattern),
                Customer.name.like(pattern),
                Material.code.like(pattern),
            )
        )
    items = session.scalars(
        query.order_by(
            Product.historical_search_key.is_(None),
            Product.updated_at.desc(),
            Product.id.desc(),
        ).limit(limit)
    ).unique().all()
    if not items:
        return {"matched": False, "item": None, "items": []}
    history_items = [product_history_dict(item) for item in items]
    return {"matched": True, "item": history_items[0], "items": history_items}


@router.post("/products", status_code=status.HTTP_201_CREATED)
def create_product(payload: ProductPayload, session: Session = Depends(get_session)) -> dict[str, Any]:
    _validate_product_refs(session, payload)
    product = Product(**{**payload.model_dump(), "product_code": _code(payload.product_code), "customer_material_code": _code(payload.customer_material_code), "product_name": _clean(payload.product_name) or payload.product_name})
    session.add(product)
    session.flush()
    _commit_or_conflict(session, "同一客户下存货编码或客户料号重复")
    session.refresh(product)
    return product_dict(product)


@router.put("/products/{product_id}")
def update_product(product_id: int, payload: ProductPayload, session: Session = Depends(get_session)) -> dict[str, Any]:
    product = session.get(Product, product_id)
    if product is None:
        _not_found("常用箱")
    _validate_product_refs(session, payload)
    for key, value in payload.model_dump().items():
        setattr(product, key, value)
    product.product_code = _code(payload.product_code)
    product.customer_material_code = _code(payload.customer_material_code)
    product.product_name = _clean(payload.product_name) or payload.product_name
    _commit_or_conflict(session, "同一客户下存货编码或客户料号重复")
    session.refresh(product)
    return product_dict(product)


@router.delete("/products/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
def disable_product(product_id: int, session: Session = Depends(get_session)) -> Response:
    product = session.get(Product, product_id)
    if product is None:
        _not_found("常用箱")
    product.is_active = False
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _validate_product_refs(session: Session, payload: ProductPayload) -> None:
    if session.get(Customer, payload.customer_id) is None:
        raise HTTPException(status_code=400, detail="客户不存在")
    if payload.material_id is not None and session.get(Material, payload.material_id) is None:
        raise HTTPException(status_code=400, detail="材质不存在")
    if payload.flute_type_id is not None and session.get(FluteType, payload.flute_type_id) is None:
        raise HTTPException(status_code=400, detail="楞型不存在")
