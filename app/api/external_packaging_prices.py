from __future__ import annotations

from datetime import date
from decimal import Decimal
import hashlib
import json
import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.api.deps import PermissionChecker, RoleChecker, get_db
from app.api.master_data_common import audit_master_change
from app.core.time_contract import beijing_today
from app.models.external_packaging_price import ExternalPackagingPriceVersion
from app.models.supplier import ExternalPackagingProduct, Supplier
from app.models.user import User
from app.services.supplier_master import supplier_snapshot


router = APIRouter()
can_cost = PermissionChecker("cost.view")
admin_only = RoleChecker(["admin"])
ALLOWED_UNITS = {"根", "米", "件", "张", "令", "kg", "吨", "只", "个", "套", "片", "卷", "㎡", "m³"}
CORNER_GUARD_CATEGORY = "paper_corner_guard"
CORNER_GUARD_QUOTE_UNIT = "米"


class TierPricePayload(BaseModel):
    min_quantity: Decimal = Field(gt=0, max_digits=18, decimal_places=4)
    unit_price: Decimal = Field(gt=0, max_digits=18, decimal_places=6)


class ExternalPackagingPricePayload(BaseModel):
    quote_unit: str = Field(min_length=1, max_length=20)
    unit_conversion_basis: str | None = None
    currency: str = Field(default="CNY", min_length=3, max_length=3)
    tax_mode: Literal["tax_inclusive", "tax_exclusive"]
    tax_rate: Decimal = Field(ge=0, le=1, max_digits=8, decimal_places=6)
    tax_amount_per_unit: Decimal | None = Field(
        default=None, ge=0, max_digits=18, decimal_places=6
    )
    unit_price: Decimal = Field(gt=0, max_digits=18, decimal_places=6)
    effective_from: date
    effective_to: date | None = None
    moq_quantity: Decimal | None = Field(
        default=None, gt=0, max_digits=18, decimal_places=4
    )
    moq_unit: str | None = Field(default=None, max_length=20)
    packaging_multiple: Decimal | None = Field(
        default=None, gt=0, max_digits=18, decimal_places=4
    )
    tier_prices: list[TierPricePayload] = Field(default_factory=list, max_length=30)
    shipping_fee_mode: Literal[
        "not_provided", "included", "per_order", "per_unit"
    ] = "not_provided"
    shipping_fee: Decimal | None = Field(
        default=None, ge=0, max_digits=18, decimal_places=6
    )
    sample_fee: Decimal | None = Field(
        default=None, ge=0, max_digits=18, decimal_places=6
    )
    plate_fee: Decimal | None = Field(
        default=None, ge=0, max_digits=18, decimal_places=6
    )
    die_fee: Decimal | None = Field(
        default=None, ge=0, max_digits=18, decimal_places=6
    )
    evidence_reference: str = Field(min_length=1, max_length=500)
    remarks: str | None = None

    @field_validator(
        "quote_unit",
        "unit_conversion_basis",
        "currency",
        "moq_unit",
        "evidence_reference",
        "remarks",
        mode="before",
    )
    @classmethod
    def strip_text(cls, value: Any) -> Any:
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        normalized = value.upper()
        if re.fullmatch(r"[A-Z]{3}", normalized) is None:
            raise ValueError("币种必须使用三位英文字母")
        return normalized


def _product_or_404(db: Session, product_id: int) -> ExternalPackagingProduct:
    product = db.scalar(
        select(ExternalPackagingProduct)
        .options(joinedload(ExternalPackagingProduct.supplier))
        .where(ExternalPackagingProduct.id == product_id)
    )
    if product is None:
        raise HTTPException(status_code=404, detail="外购包装产品不存在")
    return product


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = value.normalize()
    return format(normalized, "f")


def _tier_snapshot(raw: str) -> list[dict[str, str]]:
    try:
        rows = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return [
        {
            "min_quantity": str(row.get("min_quantity")),
            "unit_price": str(row.get("unit_price")),
        }
        for row in rows
    ]


def _price_snapshot(row: ExternalPackagingPriceVersion) -> dict[str, Any]:
    return {
        "id": row.id,
        "external_product_id": row.external_product_id,
        "version_number": row.version_number,
        "product_version": row.product_version,
        "quote_unit": row.quote_unit,
        "unit_conversion_basis": row.unit_conversion_basis,
        "currency": row.currency,
        "tax_mode": row.tax_mode,
        "tax_rate": _decimal_text(row.tax_rate),
        "tax_amount_per_unit": _decimal_text(row.tax_amount_per_unit),
        "unit_price": _decimal_text(row.unit_price),
        "effective_from": row.effective_from.isoformat(),
        "effective_to": row.effective_to.isoformat() if row.effective_to else None,
        "moq_quantity": _decimal_text(row.moq_quantity),
        "moq_unit": row.moq_unit,
        "packaging_multiple": _decimal_text(row.packaging_multiple),
        "tier_prices": _tier_snapshot(row.tier_prices_json),
        "shipping_fee_mode": row.shipping_fee_mode,
        "shipping_fee": _decimal_text(row.shipping_fee),
        "sample_fee": _decimal_text(row.sample_fee),
        "plate_fee": _decimal_text(row.plate_fee),
        "die_fee": _decimal_text(row.die_fee),
        "evidence_reference": row.evidence_reference,
        "remarks": row.remarks,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _current_price(
    db: Session,
    product_id: int,
    *,
    as_of: date,
    caliber: ExternalPackagingPriceVersion | None = None,
) -> ExternalPackagingPriceVersion | None:
    filters = [
        ExternalPackagingPriceVersion.external_product_id == product_id,
        ExternalPackagingPriceVersion.effective_from <= as_of,
        or_(
            ExternalPackagingPriceVersion.effective_to.is_(None),
            ExternalPackagingPriceVersion.effective_to >= as_of,
        ),
    ]
    if caliber is not None:
        filters.extend(
            [
                ExternalPackagingPriceVersion.quote_unit == caliber.quote_unit,
                ExternalPackagingPriceVersion.currency == caliber.currency,
                ExternalPackagingPriceVersion.tax_mode == caliber.tax_mode,
                ExternalPackagingPriceVersion.tax_rate == caliber.tax_rate,
            ]
        )
    return db.scalar(
        select(ExternalPackagingPriceVersion)
        .where(*filters)
        .order_by(
            ExternalPackagingPriceVersion.effective_from.desc(),
            ExternalPackagingPriceVersion.version_number.desc(),
        )
        .limit(1)
    )


def _clean_price_payload(
    product: ExternalPackagingProduct,
    payload: ExternalPackagingPricePayload,
) -> tuple[dict[str, Any], str]:
    unit = payload.quote_unit
    if unit not in ALLOWED_UNITS:
        raise HTTPException(status_code=422, detail="报价主单位不在允许范围内")
    if (
        product.category_code == CORNER_GUARD_CATEGORY
        and unit != CORNER_GUARD_QUOTE_UNIT
    ):
        raise HTTPException(
            status_code=422,
            detail="纸护角正式报价单位必须为“米”；采购按根/支，系统按客户单根长度自动换算",
        )
    conversion_basis = str(payload.unit_conversion_basis or "").strip() or None
    if unit != product.purchase_unit and not conversion_basis:
        raise HTTPException(
            status_code=422,
            detail="报价单位与产品采购单位不同，请填写供应商换算依据；系统不会自动换算",
        )
    if payload.effective_to is not None and payload.effective_to < payload.effective_from:
        raise HTTPException(status_code=422, detail="报价失效日期不能早于生效日期")
    if (payload.moq_quantity is None) != (payload.moq_unit is None):
        raise HTTPException(status_code=422, detail="MOQ数量和单位必须同时填写")
    if payload.moq_unit is not None and payload.moq_unit != unit:
        raise HTTPException(status_code=422, detail="本阶段 MOQ 必须使用报价主单位")
    tiers = sorted(payload.tier_prices, key=lambda row: row.min_quantity)
    tier_quantities = [row.min_quantity for row in tiers]
    if len(tier_quantities) != len(set(tier_quantities)):
        raise HTTPException(status_code=422, detail="阶梯价最小数量不能重复")
    if payload.moq_quantity is not None and any(
        quantity < payload.moq_quantity for quantity in tier_quantities
    ):
        raise HTTPException(status_code=422, detail="阶梯价起始数量不能小于 MOQ")
    tier_rows = [
        {
            "min_quantity": _decimal_text(row.min_quantity),
            "unit_price": _decimal_text(row.unit_price),
        }
        for row in tiers
    ]
    shipping_fee = payload.shipping_fee
    if payload.shipping_fee_mode in {"not_provided", "included"}:
        if shipping_fee not in (None, Decimal("0")):
            raise HTTPException(
                status_code=422, detail="运费未提供或已含单价时不得另填运费金额"
            )
        shipping_fee = None
    elif shipping_fee is None or shipping_fee <= 0:
        raise HTTPException(status_code=422, detail="按单/按单位计运费时必须填写大于0的金额")
    facts = {
        "product_version": product.version,
        "specification_snapshot_json": product.specification_json,
        "quote_unit": unit,
        "unit_conversion_basis": conversion_basis,
        "currency": payload.currency,
        "tax_mode": payload.tax_mode,
        "tax_rate": _decimal_text(payload.tax_rate),
        "tax_amount_per_unit": _decimal_text(payload.tax_amount_per_unit),
        "unit_price": _decimal_text(payload.unit_price),
        "effective_from": payload.effective_from.isoformat(),
        "effective_to": payload.effective_to.isoformat() if payload.effective_to else None,
        "moq_quantity": _decimal_text(payload.moq_quantity),
        "moq_unit": payload.moq_unit,
        "packaging_multiple": _decimal_text(payload.packaging_multiple),
        "tier_prices": tier_rows,
        "shipping_fee_mode": payload.shipping_fee_mode,
        "shipping_fee": _decimal_text(shipping_fee),
        "sample_fee": _decimal_text(payload.sample_fee),
        "plate_fee": _decimal_text(payload.plate_fee),
        "die_fee": _decimal_text(payload.die_fee),
        "evidence_reference": payload.evidence_reference,
        "remarks": str(payload.remarks or "").strip() or None,
    }
    fingerprint_facts = {
        key: value
        for key, value in facts.items()
        if key not in {"evidence_reference", "remarks"}
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_facts,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode(
            "utf-8"
        )
    ).hexdigest()
    values = {
        **facts,
        "tax_rate": payload.tax_rate,
        "tax_amount_per_unit": payload.tax_amount_per_unit,
        "unit_price": payload.unit_price,
        "effective_from": payload.effective_from,
        "effective_to": payload.effective_to,
        "moq_quantity": payload.moq_quantity,
        "packaging_multiple": payload.packaging_multiple,
        "tier_prices_json": json.dumps(tier_rows, ensure_ascii=False, separators=(",", ":")),
        "shipping_fee": shipping_fee,
        "sample_fee": payload.sample_fee,
        "plate_fee": payload.plate_fee,
        "die_fee": payload.die_fee,
        "quote_fingerprint": fingerprint,
    }
    values.pop("tier_prices")
    return values, fingerprint


@router.get("/products/{product_id}/prices")
def list_price_versions(
    product_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(can_cost),
) -> dict:
    product = _product_or_404(db, product_id)
    rows = db.scalars(
        select(ExternalPackagingPriceVersion)
        .where(ExternalPackagingPriceVersion.external_product_id == product.id)
        .order_by(ExternalPackagingPriceVersion.version_number.desc())
    ).all()
    current = _current_price(db, product.id, as_of=beijing_today())
    return {
        "product": {
            "id": product.id,
            "supplier_id": product.supplier_id,
            "supplier_name": product.supplier.display_name or product.supplier.standard_name,
            "supplier_product_code": product.supplier_product_code,
            "product_name": product.product_name,
            "purchase_unit": product.purchase_unit,
            "specification_summary": product.specification_summary,
            "version": product.version,
        },
        "current": _price_snapshot(current) if current else None,
        "items": [_price_snapshot(row) for row in rows],
    }


@router.post("/products/{product_id}/prices", status_code=status.HTTP_201_CREATED)
def create_price_version(
    product_id: int,
    payload: ExternalPackagingPricePayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
    _cost_user: User = Depends(can_cost),
) -> dict:
    product = _product_or_404(db, product_id)
    if not product.is_active or not product.supplier.is_active:
        raise HTTPException(status_code=409, detail="供应商或外购产品已停用，不能新增报价")
    values, fingerprint = _clean_price_payload(product, payload)
    existing = db.scalar(
        select(ExternalPackagingPriceVersion).where(
            ExternalPackagingPriceVersion.external_product_id == product.id,
            ExternalPackagingPriceVersion.quote_fingerprint == fingerprint,
        )
    )
    if existing is not None:
        return {"created": False, "item": _price_snapshot(existing)}
    next_version = int(
        db.scalar(
            select(func.max(ExternalPackagingPriceVersion.version_number)).where(
                ExternalPackagingPriceVersion.external_product_id == product.id
            )
        )
        or 0
    ) + 1
    row = ExternalPackagingPriceVersion(
        external_product_id=product.id,
        version_number=next_version,
        created_by=user.id,
        **values,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as error:
        db.rollback()
        duplicate = db.scalar(
            select(ExternalPackagingPriceVersion).where(
                ExternalPackagingPriceVersion.external_product_id == product.id,
                ExternalPackagingPriceVersion.quote_fingerprint == fingerprint,
            )
        )
        if duplicate is not None:
            return {"created": False, "item": _price_snapshot(duplicate)}
        raise HTTPException(
            status_code=409, detail="报价版本并发冲突，请刷新价格历史后重试"
        ) from error
    audit_master_change(
        db,
        user=user,
        action="CREATE_PRICE_VERSION",
        resource="ExternalPackagingPriceVersion",
        resource_id=row.id,
        details={
            "external_product_id": product.id,
            "version_number": row.version_number,
            "effective_from": row.effective_from.isoformat(),
            "price_values_redacted": True,
        },
    )
    db.commit()
    db.refresh(row)
    return {"created": True, "item": _price_snapshot(row)}


@router.get("/products/{product_id}/price-comparison")
def compare_current_prices(
    product_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(can_cost),
) -> dict:
    target = _product_or_404(db, product_id)
    as_of = beijing_today()
    target_price = _current_price(db, target.id, as_of=as_of)
    if target_price is None:
        return {
            "as_of": as_of.isoformat(),
            "target_product_id": target.id,
            "status": "price_missing",
            "items": [],
        }
    products = db.scalars(
        select(ExternalPackagingProduct)
        .options(joinedload(ExternalPackagingProduct.supplier))
        .where(
            ExternalPackagingProduct.category_code == target.category_code,
            ExternalPackagingProduct.specification_json == target.specification_json,
            ExternalPackagingProduct.is_active.is_(True),
        )
        .order_by(ExternalPackagingProduct.supplier_id, ExternalPackagingProduct.id)
    ).all()
    items: list[dict[str, Any]] = []
    incompatible_count = 0
    for product in products:
        if not product.supplier.is_active:
            continue
        price = _current_price(db, product.id, as_of=as_of, caliber=target_price)
        if price is None:
            incompatible_count += 1
            continue
        condition_differences: list[str] = []
        for field, label in (
            ("moq_quantity", "MOQ数量"),
            ("moq_unit", "MOQ单位"),
            ("packaging_multiple", "包装倍数"),
            ("shipping_fee_mode", "运费方式"),
            ("shipping_fee", "运费"),
            ("sample_fee", "打样费"),
            ("plate_fee", "版费"),
            ("die_fee", "刀模费"),
        ):
            if getattr(price, field) != getattr(target_price, field):
                condition_differences.append(label)
        items.append(
            {
                "external_product_id": product.id,
                "supplier_id": product.supplier_id,
                "supplier": supplier_snapshot(product.supplier),
                "supplier_product_code": product.supplier_product_code,
                "product_name": product.product_name,
                "unit_price": _decimal_text(price.unit_price),
                "quote_unit": price.quote_unit,
                "currency": price.currency,
                "tax_mode": price.tax_mode,
                "tax_rate": _decimal_text(price.tax_rate),
                "moq_quantity": _decimal_text(price.moq_quantity),
                "moq_unit": price.moq_unit,
                "condition_differences": condition_differences,
            }
        )
    if items:
        minimum = min(Decimal(item["unit_price"]) for item in items)
        for item in items:
            item["difference_to_lowest"] = _decimal_text(
                Decimal(item["unit_price"]) - minimum
            )
    return {
        "as_of": as_of.isoformat(),
        "target_product_id": target.id,
        "status": "comparable" if items else "no_same_caliber_price",
        "caliber": {
            "specification_summary": target.specification_summary,
            "quote_unit": target_price.quote_unit,
            "currency": target_price.currency,
            "tax_mode": target_price.tax_mode,
            "tax_rate": _decimal_text(target_price.tax_rate),
        },
        "incompatible_count": incompatible_count,
        "items": items,
    }
