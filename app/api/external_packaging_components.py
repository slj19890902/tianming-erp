from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.api.deps import RoleChecker, get_db
from app.api.master_data_common import audit_master_change
from app.models.external_packaging_component import (
    ProductExternalComponent,
    ProductExternalComponentCandidate,
    ProductExternalComponentSet,
)
from app.models.product import Product
from app.models.supplier import ExternalPackagingProduct, Supplier
from app.models.user import User
from app.services.supplier_master import SUPPLIER_CATEGORY_LABELS


router = APIRouter()
admin_only = RoleChecker(["admin"])
ALLOWED_UNITS = {"根", "米", "件", "张", "令", "kg", "吨", "只", "个", "套", "片", "卷"}


class ExternalCandidatePayload(BaseModel):
    external_product_id: int = Field(gt=0)
    is_default: bool = False


class ExternalComponentPayload(BaseModel):
    purpose: str = Field(min_length=1, max_length=200)
    quantity_per_finished_unit: Decimal = Field(gt=0, max_digits=18, decimal_places=6)
    waste_rate: Decimal = Field(default=Decimal("0"), ge=0, le=1, max_digits=8, decimal_places=6)
    consumption_unit: str = Field(min_length=1, max_length=20)
    units_per_purchase_unit: Decimal | None = Field(
        default=None, gt=0, max_digits=18, decimal_places=6
    )
    conversion_basis: str | None = Field(default=None, max_length=500)
    is_required: bool = True
    remarks: str | None = None
    candidates: list[ExternalCandidatePayload] = Field(min_length=1, max_length=20)

    @field_validator("purpose", "consumption_unit", "conversion_basis", "remarks", mode="before")
    @classmethod
    def clean_text(cls, value: Any) -> Any:
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None


class ExternalComponentSetPayload(BaseModel):
    expected_version: int = Field(ge=0)
    components: list[ExternalComponentPayload] = Field(default_factory=list, max_length=30)


def _product_or_404(db: Session, product_id: int) -> Product:
    product = db.get(Product, product_id)
    if product is None or product.deleted_at is not None or product.purged_at is not None:
        raise HTTPException(status_code=404, detail="常用箱不存在")
    return product


def _current_set(db: Session, product_id: int) -> ProductExternalComponentSet | None:
    return db.scalar(
        select(ProductExternalComponentSet)
        .options(
            selectinload(ProductExternalComponentSet.components).selectinload(
                ProductExternalComponent.candidates
            )
        )
        .where(
            ProductExternalComponentSet.product_id == product_id,
            ProductExternalComponentSet.is_current.is_(True),
        )
    )


def _live_products(
    db: Session, product_ids: set[int]
) -> dict[int, ExternalPackagingProduct]:
    if not product_ids:
        return {}
    rows = db.scalars(
        select(ExternalPackagingProduct)
        .options(
            selectinload(ExternalPackagingProduct.supplier).selectinload(
                Supplier.supply_categories
            ),
            selectinload(ExternalPackagingProduct.customer_scope),
        )
        .where(ExternalPackagingProduct.id.in_(product_ids))
    ).all()
    return {row.id: row for row in rows}


def _candidate_is_available(row: ExternalPackagingProduct) -> bool:
    if not row.is_active or row.supplier is None or not row.supplier.is_active:
        return False
    return any(
        category.is_active and category.category_code == row.category_code
        for category in row.supplier.supply_categories
    )


def _set_snapshot(
    db: Session, product: Product, row: ProductExternalComponentSet | None
) -> dict[str, Any]:
    if row is None:
        return {
            "product_id": product.id,
            "customer_id": product.customer_id,
            "version": 0,
            "components": [],
        }
    product_ids = {
        candidate.external_product_id
        for component in row.components
        for candidate in component.candidates
    }
    live = _live_products(db, product_ids)
    components: list[dict[str, Any]] = []
    for component in row.components:
        candidates = []
        for candidate in component.candidates:
            current = live.get(candidate.external_product_id)
            candidates.append(
                {
                    "id": candidate.id,
                    "external_product_id": candidate.external_product_id,
                    "is_default": candidate.is_default,
                    "supplier_id": candidate.supplier_id_snapshot,
                    "supplier_name": candidate.supplier_name_snapshot,
                    "supplier_product_code": candidate.supplier_product_code_snapshot,
                    "product_name": candidate.product_name_snapshot,
                    "purchase_unit": candidate.purchase_unit_snapshot,
                    "customer_scope_id": candidate.customer_scope_id_snapshot,
                    "external_product_version": candidate.external_product_version_snapshot,
                    "currently_available": bool(current and _candidate_is_available(current)),
                }
            )
        components.append(
            {
                "id": component.id,
                "display_order": component.display_order,
                "purpose": component.purpose,
                "quantity_per_finished_unit": str(component.quantity_per_finished_unit),
                "waste_rate": str(component.waste_rate),
                "consumption_unit": component.consumption_unit,
                "units_per_purchase_unit": (
                    str(component.units_per_purchase_unit)
                    if component.units_per_purchase_unit is not None
                    else None
                ),
                "conversion_basis": component.conversion_basis,
                "is_required": component.is_required,
                "remarks": component.remarks,
                "category_code": component.category_code,
                "category_label": SUPPLIER_CATEGORY_LABELS.get(
                    component.category_code, component.category_code
                ),
                "specification": json.loads(component.specification_json or "{}"),
                "specification_summary": component.specification_summary,
                "candidates": candidates,
            }
        )
    return {
        "product_id": product.id,
        "customer_id": product.customer_id,
        "version": row.version,
        "components": components,
    }


@router.get("/{product_id}/external-components")
def get_external_components(
    product_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(admin_only),
) -> dict[str, Any]:
    product = _product_or_404(db, product_id)
    return _set_snapshot(db, product, _current_set(db, product.id))


@router.get("/{product_id}/external-component-candidates")
def list_external_component_candidates(
    product_id: int,
    keyword: str = Query(default="", max_length=100),
    db: Session = Depends(get_db),
    _user: User = Depends(admin_only),
) -> dict[str, Any]:
    product = _product_or_404(db, product_id)
    rows = db.scalars(
        select(ExternalPackagingProduct)
        .options(
            selectinload(ExternalPackagingProduct.supplier).selectinload(
                Supplier.supply_categories
            ),
            selectinload(ExternalPackagingProduct.customer_scope),
        )
        .where(
            ExternalPackagingProduct.is_active.is_(True),
            (ExternalPackagingProduct.customer_scope_id.is_(None))
            | (ExternalPackagingProduct.customer_scope_id == product.customer_id),
        )
        .order_by(
            ExternalPackagingProduct.category_code,
            ExternalPackagingProduct.supplier_id,
            ExternalPackagingProduct.supplier_product_code,
        )
    ).all()
    cleaned = keyword.strip().casefold()
    items = []
    for row in rows:
        if not _candidate_is_available(row):
            continue
        haystack = " ".join(
            (
                row.supplier.display_name or row.supplier.standard_name,
                row.supplier_product_code,
                row.product_name,
                row.specification_summary,
            )
        ).casefold()
        if cleaned and cleaned not in haystack:
            continue
        items.append(
            {
                "id": row.id,
                "supplier_id": row.supplier_id,
                "supplier_name": row.supplier.display_name or row.supplier.standard_name,
                "category_code": row.category_code,
                "category_label": SUPPLIER_CATEGORY_LABELS.get(
                    row.category_code, row.category_code
                ),
                "supplier_product_code": row.supplier_product_code,
                "product_name": row.product_name,
                "purchase_unit": row.purchase_unit,
                "specification_summary": row.specification_summary,
                "specification": json.loads(row.specification_json or "{}"),
                "customer_scope_id": row.customer_scope_id,
                "customer_scope_name": (
                    row.customer_scope.name if row.customer_scope is not None else None
                ),
                "version": row.version,
            }
        )
    return {"product_id": product.id, "customer_id": product.customer_id, "items": items}


def _validate_components(
    db: Session, product: Product, payload: ExternalComponentSetPayload
) -> list[tuple[ExternalComponentPayload, list[ExternalPackagingProduct]]]:
    purposes: set[str] = set()
    all_ids: list[int] = []
    for component in payload.components:
        purpose_key = component.purpose.casefold()
        if purpose_key in purposes:
            raise HTTPException(status_code=422, detail=f"外购组件用途重复：{component.purpose}")
        purposes.add(purpose_key)
        if component.consumption_unit not in ALLOWED_UNITS:
            raise HTTPException(status_code=422, detail=f"{component.purpose}的用量单位无效")
        candidate_ids = [item.external_product_id for item in component.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise HTTPException(status_code=422, detail=f"{component.purpose}存在重复供应商候选")
        if sum(1 for item in component.candidates if item.is_default) != 1:
            raise HTTPException(status_code=422, detail=f"{component.purpose}必须且只能选择一个默认候选")
        all_ids.extend(candidate_ids)
    if len(all_ids) != len(set(all_ids)):
        raise HTTPException(status_code=422, detail="同一个供应商产品不能重复绑定到多个组件")

    products = _live_products(db, set(all_ids))
    validated: list[tuple[ExternalComponentPayload, list[ExternalPackagingProduct]]] = []
    for component in payload.components:
        rows = [products.get(item.external_product_id) for item in component.candidates]
        if any(row is None for row in rows):
            raise HTTPException(status_code=422, detail=f"{component.purpose}包含不存在的供应商产品")
        typed_rows = [row for row in rows if row is not None]
        for row in typed_rows:
            if not _candidate_is_available(row):
                raise HTTPException(
                    status_code=409,
                    detail=f"{row.supplier_product_code}已停用或供应商已停用，不能保存为新候选",
                )
            if row.customer_scope_id not in (None, product.customer_id):
                raise HTTPException(
                    status_code=409,
                    detail=f"{row.supplier_product_code}是其他客户专用产品，不能绑定当前常用箱",
                )
        first = typed_rows[0]
        if any(
            row.category_code != first.category_code
            or row.specification_json != first.specification_json
            for row in typed_rows[1:]
        ):
            raise HTTPException(
                status_code=422,
                detail=f"{component.purpose}的候选类别或结构化规格不一致",
            )
        purchase_units = {row.purchase_unit for row in typed_rows}
        if len(purchase_units) != 1:
            raise HTTPException(
                status_code=422,
                detail=f"{component.purpose}的候选采购单位不一致，请拆成不同组件",
            )
        purchase_unit = next(iter(purchase_units))
        if purchase_unit != component.consumption_unit and (
            component.units_per_purchase_unit is None
            or not component.conversion_basis
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{component.purpose}按{component.consumption_unit}使用、按{purchase_unit}采购，"
                    "必须填写每采购单位可用数量和换算依据"
                ),
            )
        validated.append((component, typed_rows))
    return validated


@router.put("/{product_id}/external-components")
def replace_external_components(
    product_id: int,
    payload: ExternalComponentSetPayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict[str, Any]:
    product = _product_or_404(db, product_id)
    current = _current_set(db, product.id)
    current_version = current.version if current is not None else 0
    if current_version != payload.expected_version:
        raise HTTPException(status_code=409, detail="外购组件资料已更新，请刷新后再保存")
    validated = _validate_components(db, product, payload)
    before = _set_snapshot(db, product, current)
    try:
        if current is not None:
            result = db.execute(
                update(ProductExternalComponentSet)
                .where(
                    ProductExternalComponentSet.id == current.id,
                    ProductExternalComponentSet.is_current.is_(True),
                    ProductExternalComponentSet.version == payload.expected_version,
                )
                .values(is_current=False)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                db.rollback()
                raise HTTPException(status_code=409, detail="外购组件资料版本冲突")
        new_set = ProductExternalComponentSet(
            product_id=product.id,
            version=current_version + 1,
            is_current=True,
            created_by=user.id,
        )
        db.add(new_set)
        db.flush()
        for display_order, (component_payload, rows) in enumerate(validated, start=1):
            first = rows[0]
            component = ProductExternalComponent(
                component_set_id=new_set.id,
                display_order=display_order,
                purpose=component_payload.purpose,
                quantity_per_finished_unit=component_payload.quantity_per_finished_unit,
                waste_rate=component_payload.waste_rate,
                consumption_unit=component_payload.consumption_unit,
                units_per_purchase_unit=component_payload.units_per_purchase_unit,
                conversion_basis=component_payload.conversion_basis,
                is_required=component_payload.is_required,
                remarks=component_payload.remarks,
                category_code=first.category_code,
                specification_json=first.specification_json,
                specification_summary=first.specification_summary,
            )
            db.add(component)
            db.flush()
            flags = {
                item.external_product_id: item.is_default
                for item in component_payload.candidates
            }
            for row in rows:
                db.add(
                    ProductExternalComponentCandidate(
                        component_id=component.id,
                        external_product_id=row.id,
                        is_default=flags[row.id],
                        supplier_id_snapshot=row.supplier_id,
                        supplier_name_snapshot=row.supplier.display_name
                        or row.supplier.standard_name,
                        supplier_product_code_snapshot=row.supplier_product_code,
                        product_name_snapshot=row.product_name,
                        purchase_unit_snapshot=row.purchase_unit,
                        customer_scope_id_snapshot=row.customer_scope_id,
                        external_product_version_snapshot=row.version,
                    )
                )
        db.flush()
        after = _set_snapshot(db, product, new_set)
        audit_master_change(
            db,
            user=user,
            action="REPLACE",
            resource="ProductExternalPackagingComponents",
            resource_id=product.id,
            details={"before": before, "after": after},
        )
        db.commit()
        return _set_snapshot(db, product, _current_set(db, product.id))
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="外购组件资料保存冲突，请刷新后重试") from error
