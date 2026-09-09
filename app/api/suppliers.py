from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.api.deps import PermissionChecker, RoleChecker, get_db
from app.api.master_data_common import audit_master_change
from app.models.customer import Customer
from app.models.supplier import (
    ExternalPackagingProduct,
    Supplier,
    SupplierAlias,
    SupplierSupplyCategory,
)
from app.models.user import User
from app.services.supplier_master import (
    clean_business_code,
    clean_supplier_name,
    normalize_supplier_identity,
    SUPPLIER_CATEGORY_LABELS,
    supplier_snapshot,
)


router = APIRouter()
can_read_candidates = PermissionChecker("products.view")
admin_only = RoleChecker(["admin"])
EXTERNAL_PACKAGING_CATEGORIES = {
    "paper_corner_guard",
    "coated_board",
    "printed_folding_carton",
    "epe_cushion",
    "hollow_board",
    "honeycomb_board",
    "other_packaging",
}
PURCHASE_UNITS = {"根", "米", "件", "张", "令", "kg", "吨", "只", "个", "套", "片", "卷", "箱"}


class SupplierPayload(BaseModel):
    standard_name: str = Field(min_length=1, max_length=200)
    display_name: str | None = Field(default=None, max_length=100)
    business_code: str | None = Field(default=None, max_length=50)
    contact_name: str | None = Field(default=None, max_length=100)
    phone: str | None = Field(default=None, max_length=100)
    remarks: str | None = None
    sort_order: int = Field(default=100, ge=0, le=100_000)
    settlement_day: int = Field(default=20, ge=1, le=31)
    aliases: list[str] = Field(default_factory=list, max_length=30)
    supply_categories: list[str] | None = None

    @field_validator(
        "standard_name",
        "display_name",
        "business_code",
        "contact_name",
        "phone",
        "remarks",
        mode="before",
    )
    @classmethod
    def strip_text(cls, value: Any) -> Any:
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None

    @field_validator("aliases", mode="before")
    @classmethod
    def clean_aliases(cls, value: Any) -> list[str]:
        if value is None:
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    @field_validator("supply_categories", mode="before")
    @classmethod
    def clean_supply_categories(cls, value: Any) -> list[str] | None:
        if value is None:
            return None
        categories = list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
        unknown = [item for item in categories if item not in SUPPLIER_CATEGORY_LABELS]
        if unknown:
            raise ValueError(f"不支持的供货类别：{'、'.join(unknown)}")
        return categories


class SupplierUpdatePayload(SupplierPayload):
    expected_version: int = Field(ge=1)


class SupplierStatusPayload(BaseModel):
    expected_version: int = Field(ge=1)
    is_active: bool


class ExternalPackagingProductPayload(BaseModel):
    category_code: str
    supplier_product_code: str = Field(min_length=1, max_length=100)
    product_name: str = Field(min_length=1, max_length=200)
    purchase_unit: str = Field(min_length=1, max_length=20)
    customer_scope_id: int | None = Field(default=None, gt=0)
    specification: dict[str, Any] = Field(default_factory=dict)
    drawing_sample_version: str | None = Field(default=None, max_length=100)
    lead_time_days: int | None = Field(default=None, ge=0, le=3650)
    remarks: str | None = None

    @field_validator(
        "category_code",
        "supplier_product_code",
        "product_name",
        "purchase_unit",
        "drawing_sample_version",
        "remarks",
        mode="before",
    )
    @classmethod
    def strip_product_text(cls, value: Any) -> Any:
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None


class ExternalPackagingProductUpdatePayload(ExternalPackagingProductPayload):
    expected_version: int = Field(ge=1)


class ExternalPackagingProductStatusPayload(BaseModel):
    expected_version: int = Field(ge=1)
    is_active: bool


def _supplier_or_404(db: Session, supplier_id: int) -> Supplier:
    supplier = db.scalar(
        select(Supplier)
        .options(
            selectinload(Supplier.aliases),
            selectinload(Supplier.supply_categories),
        )
        .where(Supplier.id == supplier_id)
    )
    if supplier is None:
        raise HTTPException(status_code=404, detail="供应商不存在")
    return supplier


def _version_conflict(supplier: Supplier, expected_version: int) -> None:
    if supplier.version != expected_version:
        raise HTTPException(
            status_code=409,
            detail=(
                f"供应商资料已从 v{expected_version} 更新为 "
                f"v{supplier.version}，请刷新后再保存"
            ),
        )


def _atomic_supplier_update(
    db: Session,
    *,
    supplier: Supplier,
    expected_version: int,
    values: dict[str, Any],
) -> None:
    try:
        result = db.execute(
            update(Supplier)
            .where(
                Supplier.id == supplier.id,
                Supplier.version == expected_version,
            )
            .values(**values, version=expected_version + 1)
            .execution_options(synchronize_session=False)
        )
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="供应商名称、别名或非空业务代码与现有资料冲突",
        ) from error
    if result.rowcount != 1:
        db.rollback()
        current = _supplier_or_404(db, supplier.id)
        _version_conflict(current, expected_version)
        raise HTTPException(status_code=409, detail="供应商资料版本冲突")
    db.refresh(supplier)


def _clean_payload(payload: SupplierPayload) -> tuple[dict, list[tuple[str, str]]]:
    standard_name = clean_supplier_name(payload.standard_name)
    normalized_name = normalize_supplier_identity(standard_name)
    display_name = clean_supplier_name(payload.display_name) or None
    business_code = clean_business_code(payload.business_code)
    aliases: list[tuple[str, str]] = []
    seen = {normalized_name}
    for raw in payload.aliases:
        alias_name = clean_supplier_name(raw)
        normalized_alias = normalize_supplier_identity(alias_name)
        if not normalized_alias or normalized_alias in seen:
            continue
        seen.add(normalized_alias)
        aliases.append((alias_name, normalized_alias))
    return (
        {
            "standard_name": standard_name,
            "normalized_name": normalized_name,
            "display_name": display_name,
            "business_code": business_code,
            "normalized_business_code": business_code,
            "contact_name": clean_supplier_name(payload.contact_name) or None,
            "phone": clean_supplier_name(payload.phone) or None,
            "remarks": clean_supplier_name(payload.remarks) or None,
            "sort_order": payload.sort_order,
            "settlement_day": payload.settlement_day,
        },
        aliases,
    )


def _raise_identity_conflicts(
    db: Session,
    *,
    supplier_id: int | None,
    values: dict,
    aliases: list[tuple[str, str]],
) -> None:
    own_id = supplier_id if supplier_id is not None else -1
    requested_names = {values["normalized_name"], *(item[1] for item in aliases)}
    name_conflict = db.scalar(
        select(Supplier.id).where(
            Supplier.id != own_id,
            Supplier.normalized_name.in_(requested_names),
        )
    )
    alias_conflict = db.scalar(
        select(SupplierAlias.supplier_id).where(
            SupplierAlias.supplier_id != own_id,
            SupplierAlias.normalized_alias.in_(requested_names),
        )
    )
    if name_conflict is not None or alias_conflict is not None:
        raise HTTPException(
            status_code=409,
            detail="供应商标准名称或别名已被其他供应商使用",
        )
    business_code = values["normalized_business_code"]
    if business_code:
        code_conflict = db.scalar(
            select(Supplier.id).where(
                Supplier.id != own_id,
                Supplier.normalized_business_code == business_code,
            )
        )
        if code_conflict is not None:
            raise HTTPException(
                status_code=409,
                detail=f"供应商业务代码 {business_code} 已被使用",
            )


def _set_aliases(supplier: Supplier, aliases: list[tuple[str, str]]) -> None:
    target = {normalized: name for name, normalized in aliases}
    for existing in list(supplier.aliases):
        if existing.normalized_alias not in target:
            supplier.aliases.remove(existing)
        else:
            existing.alias_name = target.pop(existing.normalized_alias)
    supplier.aliases.extend(
        SupplierAlias(alias_name=name, normalized_alias=normalized)
        for normalized, name in target.items()
    )


def _set_supply_categories(supplier: Supplier, categories: list[str]) -> None:
    target = set(categories)
    existing = {row.category_code: row for row in supplier.supply_categories}
    for code, row in existing.items():
        row.is_active = code in target
    supplier.supply_categories.extend(
        SupplierSupplyCategory(category_code=code, is_active=True)
        for code in categories
        if code not in existing
    )


def _commit_or_conflict(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="供应商名称、别名或非空业务代码与现有资料冲突",
        ) from error


def _flush_or_conflict(db: Session) -> None:
    try:
        db.flush()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="供应商名称、别名或非空业务代码与现有资料冲突",
        ) from error


@router.get("/candidates")
def list_supplier_candidates(
    db: Session = Depends(get_db),
    _user: User = Depends(can_read_candidates),
) -> dict:
    rows = db.scalars(
        select(Supplier)
        .options(
            selectinload(Supplier.aliases),
            selectinload(Supplier.supply_categories),
        )
        .where(Supplier.is_active.is_(True))
        .order_by(
            Supplier.sort_order,
            Supplier.display_name,
            Supplier.standard_name,
            Supplier.id,
        )
    ).all()
    return {"items": [supplier_snapshot(row) for row in rows]}


@router.get("")
def list_suppliers(
    keyword: str = Query(default=""),
    include_inactive: bool = Query(default=True),
    db: Session = Depends(get_db),
    _user: User = Depends(admin_only),
) -> dict:
    statement = select(Supplier).options(
        selectinload(Supplier.aliases),
        selectinload(Supplier.supply_categories),
    )
    if not include_inactive:
        statement = statement.where(Supplier.is_active.is_(True))
    cleaned_keyword = keyword.strip()
    if cleaned_keyword:
        pattern = f"%{cleaned_keyword}%"
        statement = (
            statement.outerjoin(SupplierAlias)
            .where(
                or_(
                    Supplier.standard_name.ilike(pattern),
                    Supplier.display_name.ilike(pattern),
                    Supplier.business_code.ilike(pattern),
                    SupplierAlias.alias_name.ilike(pattern),
                )
            )
            .distinct()
        )
    rows = db.scalars(
        statement.order_by(
            Supplier.is_active.desc(),
            Supplier.sort_order,
            Supplier.display_name,
            Supplier.standard_name,
            Supplier.id,
        )
    ).all()
    return {"items": [supplier_snapshot(row) for row in rows]}


@router.get("/{supplier_id}")
def get_supplier(
    supplier_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(admin_only),
) -> dict:
    return supplier_snapshot(_supplier_or_404(db, supplier_id))


@router.post("", status_code=status.HTTP_201_CREATED)
def create_supplier(
    payload: SupplierPayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    values, aliases = _clean_payload(payload)
    _raise_identity_conflicts(
        db,
        supplier_id=None,
        values=values,
        aliases=aliases,
    )
    supplier = Supplier(**values, is_active=True, version=1)
    _set_aliases(supplier, aliases)
    _set_supply_categories(
        supplier,
        payload.supply_categories
        if payload.supply_categories is not None
        else ["corrugated_board"],
    )
    db.add(supplier)
    _flush_or_conflict(db)
    after = supplier_snapshot(supplier)
    audit_master_change(
        db,
        user=user,
        action="CREATE",
        resource="Supplier",
        resource_id=supplier.id,
        details={"after": after},
    )
    _commit_or_conflict(db)
    db.refresh(supplier)
    return supplier_snapshot(_supplier_or_404(db, supplier.id))


@router.put("/{supplier_id}")
def update_supplier(
    supplier_id: int,
    payload: SupplierUpdatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    supplier = _supplier_or_404(db, supplier_id)
    _version_conflict(supplier, payload.expected_version)
    before = supplier_snapshot(supplier)
    values, aliases = _clean_payload(payload)
    if values["normalized_name"] != supplier.normalized_name:
        old_normalized = supplier.normalized_name
        if old_normalized not in {item[1] for item in aliases}:
            aliases.append((supplier.standard_name, old_normalized))
    _raise_identity_conflicts(
        db,
        supplier_id=supplier.id,
        values=values,
        aliases=aliases,
    )
    changed = any(getattr(supplier, key) != value for key, value in values.items())
    current_aliases = [
        (row.alias_name, row.normalized_alias) for row in supplier.aliases
    ]
    if current_aliases != aliases:
        changed = True
    desired_categories = (
        payload.supply_categories
        if payload.supply_categories is not None
        else [row.category_code for row in supplier.supply_categories if row.is_active]
    )
    current_categories = [
        row.category_code for row in supplier.supply_categories if row.is_active
    ]
    if set(current_categories) != set(desired_categories):
        changed = True
    if not changed:
        return before
    _atomic_supplier_update(
        db,
        supplier=supplier,
        expected_version=payload.expected_version,
        values=values,
    )
    _set_aliases(supplier, aliases)
    _set_supply_categories(supplier, desired_categories)
    _flush_or_conflict(db)
    refreshed_statements = []
    if before.get("settlement_day", 20) != supplier.settlement_day:
        from app.core.time_contract import beijing_today
        from app.models.supplier_settlement import SupplierMonthlyStatement
        from app.services.supplier_monthly_settlement import (
            ACTIVE_DRAFT_STATUSES, SupplierSettlementError, regenerate_statement,
        )
        drafts = list(db.scalars(select(SupplierMonthlyStatement).where(
            SupplierMonthlyStatement.supplier_id == supplier.id,
            SupplierMonthlyStatement.settlement_month == beijing_today().strftime("%Y-%m"),
            SupplierMonthlyStatement.active_guard == 1,
            SupplierMonthlyStatement.status.in_(ACTIVE_DRAFT_STATUSES),
        )).all())
        for draft in drafts:
            try:
                replacement = regenerate_statement(
                    db, statement_id=draft.id, expected_version=draft.version,
                    reason="供应商对账日调整，自动重算当月未确认草稿", user=user,
                    allow_empty=True,
                )
                refreshed_statements.append(replacement.id)
            except SupplierSettlementError as error:
                if error.code == "SUPPLIER_SETTLEMENT_REGENERATION_NO_CHANGE":
                    continue
                db.rollback()
                raise HTTPException(status_code=409, detail=str(error)) from error
    after = supplier_snapshot(supplier)
    audit_master_change(
        db,
        user=user,
        action="UPDATE",
        resource="Supplier",
        resource_id=supplier.id,
        details={"before": before, "after": after,
                 "refreshed_statement_ids": refreshed_statements},
    )
    _commit_or_conflict(db)
    return supplier_snapshot(_supplier_or_404(db, supplier.id))


@router.put("/{supplier_id}/status")
def update_supplier_status(
    supplier_id: int,
    payload: SupplierStatusPayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    supplier = _supplier_or_404(db, supplier_id)
    _version_conflict(supplier, payload.expected_version)
    if supplier.is_active == payload.is_active:
        return supplier_snapshot(supplier)
    before = supplier_snapshot(supplier)
    _atomic_supplier_update(
        db,
        supplier=supplier,
        expected_version=payload.expected_version,
        values={"is_active": payload.is_active},
    )
    _flush_or_conflict(db)
    after = supplier_snapshot(supplier)
    audit_master_change(
        db,
        user=user,
        action="ACTIVATE" if payload.is_active else "DEACTIVATE",
        resource="Supplier",
        resource_id=supplier.id,
        details={"before": before, "after": after},
    )
    _commit_or_conflict(db)
    return supplier_snapshot(_supplier_or_404(db, supplier.id))


def _normalized_product_code(value: object) -> str:
    return "".join(str(value or "").strip().upper().split())


def _positive_number(specification: dict[str, Any], field: str, label: str) -> float:
    try:
        value = float(specification.get(field))
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=f"{label}必须填写有效数字") from error
    if value <= 0:
        raise HTTPException(status_code=422, detail=f"{label}必须大于 0")
    return value


def _positive_integer(
    specification: dict[str, Any], field: str, label: str, *, maximum: int
) -> int:
    value = _positive_number(specification, field, label)
    if not value.is_integer() or value > maximum:
        raise HTTPException(
            status_code=422, detail=f"{label}必须是 1 到 {maximum} 的整数"
        )
    return int(value)


def _optional_positive_number(
    specification: dict[str, Any], field: str, label: str
) -> float | None:
    raw = specification.get(field)
    if raw in (None, ""):
        return None
    return _positive_number(specification, field, label)


def _clean_external_product_payload(
    payload: ExternalPackagingProductPayload,
) -> dict[str, Any]:
    category = payload.category_code
    if category not in EXTERNAL_PACKAGING_CATEGORIES:
        raise HTTPException(status_code=422, detail="请选择有效的外购包装类别")
    unit = str(payload.purchase_unit or "").strip()
    if unit not in PURCHASE_UNITS:
        raise HTTPException(status_code=422, detail="采购单位不在允许范围内")
    specification = dict(payload.specification or {})
    if category == "paper_corner_guard":
        shape = str(specification.get("shape") or "").strip().upper()
        if shape not in {"L", "U", "其他"}:
            raise HTTPException(status_code=422, detail="纸护角形状请选择 L、U 或其他")
        left = _positive_number(specification, "side_a_mm", "纸护角边宽 A")
        right = _positive_number(specification, "side_b_mm", "纸护角边宽 B")
        thickness = _positive_number(specification, "thickness_mm", "纸护角厚度")
        length = _positive_number(specification, "length_mm", "纸护角长度")
        specification = {
            "shape": shape,
            "side_a_mm": left,
            "side_b_mm": right,
            "thickness_mm": thickness,
            "length_mm": length,
        }
        summary = f"{shape}型 {left:g}×{right:g}×{thickness:g}mm，长{length:g}mm"
    elif category == "coated_board":
        material_type = str(specification.get("material_type") or "").strip()
        if not material_type:
            raise HTTPException(status_code=422, detail="请填写涂布纸板实际材质")
        weight = _positive_number(specification, "basis_weight_gsm", "克重")
        thickness = _positive_number(specification, "thickness_mm", "厚度")
        length = _positive_number(specification, "sheet_length_mm", "单张长度")
        width = _positive_number(specification, "sheet_width_mm", "单张宽度")
        specification = {
            "material_type": material_type,
            "basis_weight_gsm": weight,
            "thickness_mm": thickness,
            "sheet_length_mm": length,
            "sheet_width_mm": width,
        }
        summary = f"{material_type} {weight:g}g/{thickness:g}mm，{length:g}×{width:g}mm"
    elif category == "printed_folding_carton":
        structure = str(specification.get("structure") or "").strip()
        if not structure:
            raise HTTPException(status_code=422, detail="请填写折叠彩盒结构")
        length = _positive_number(specification, "finished_length_mm", "成品长度")
        width = _positive_number(specification, "finished_width_mm", "成品宽度")
        height = _positive_number(specification, "finished_height_mm", "成品高度")
        unfolded_length = _positive_number(
            specification, "unfolded_length_mm", "展开长度"
        )
        unfolded_width = _positive_number(
            specification, "unfolded_width_mm", "展开宽度"
        )
        substrate = str(specification.get("substrate") or "").strip()
        if not substrate:
            raise HTTPException(status_code=422, detail="请填写折叠彩盒基材")
        colors = _positive_integer(
            specification, "print_color_count", "印刷色数", maximum=12
        )
        raw_processes = specification.get("ordered_processes") or []
        if isinstance(raw_processes, str):
            raw_processes = raw_processes.replace("→", ",").replace("、", ",").split(",")
        processes = [str(item).strip() for item in raw_processes if str(item).strip()]
        specification = {
            "structure": structure,
            "finished_length_mm": length,
            "finished_width_mm": width,
            "finished_height_mm": height,
            "unfolded_length_mm": unfolded_length,
            "unfolded_width_mm": unfolded_width,
            "substrate": substrate,
            "print_color_count": colors,
            "ordered_processes": processes,
        }
        process_text = f"，{'→'.join(processes)}" if processes else ""
        summary = (
            f"{structure} {length:g}×{width:g}×{height:g}mm，展开"
            f"{unfolded_length:g}×{unfolded_width:g}mm，{substrate}/{colors}色{process_text}"
        )
    elif category == "epe_cushion":
        shape = str(specification.get("shape") or "").strip()
        if not shape:
            raise HTTPException(status_code=422, detail="请填写 EPE 形态")
        length = _positive_number(specification, "length_mm", "EPE长度")
        width = _positive_number(specification, "width_mm", "EPE宽度")
        thickness = _positive_number(specification, "thickness_mm", "EPE厚度")
        layers = _positive_integer(specification, "layers", "EPE复合层数", maximum=50)
        density = _optional_positive_number(specification, "density_kg_m3", "EPE密度")
        performance = str(specification.get("performance") or "").strip() or None
        specification = {
            "shape": shape,
            "length_mm": length,
            "width_mm": width,
            "thickness_mm": thickness,
            "layers": layers,
            "density_kg_m3": density,
            "performance": performance,
        }
        density_text = f"，{density:g}kg/m³" if density is not None else ""
        summary = f"{shape} {length:g}×{width:g}×{thickness:g}mm，{layers}层{density_text}"
    elif category == "hollow_board":
        length = _positive_number(specification, "length_mm", "中空板长度")
        width = _positive_number(specification, "width_mm", "中空板宽度")
        thickness = _positive_number(specification, "thickness_mm", "中空板厚度")
        color = str(specification.get("color") or "").strip()
        if not color:
            raise HTTPException(status_code=422, detail="请填写中空板颜色")
        basis_weight = _optional_positive_number(
            specification, "basis_weight_gsm", "中空板克重"
        )
        density = _optional_positive_number(
            specification, "density_kg_m3", "中空板密度"
        )
        specification = {
            "length_mm": length,
            "width_mm": width,
            "thickness_mm": thickness,
            "color": color,
            "basis_weight_gsm": basis_weight,
            "density_kg_m3": density,
        }
        optional = f"，{basis_weight:g}g/㎡" if basis_weight is not None else ""
        optional += f"，{density:g}kg/m³" if density is not None else ""
        summary = f"{color} {length:g}×{width:g}×{thickness:g}mm{optional}"
    elif category == "honeycomb_board":
        material = str(specification.get("material") or "").strip()
        if not material:
            raise HTTPException(status_code=422, detail="请填写蜂窝板材质")
        aperture = _positive_number(specification, "aperture_mm", "蜂窝板孔径")
        length = _positive_number(specification, "length_mm", "蜂窝板长度")
        width = _positive_number(specification, "width_mm", "蜂窝板宽度")
        thickness = _positive_number(specification, "thickness_mm", "蜂窝板厚度")
        specification = {
            "material": material,
            "aperture_mm": aperture,
            "length_mm": length,
            "width_mm": width,
            "thickness_mm": thickness,
        }
        summary = (
            f"材质{material}，孔径{aperture:g}mm，"
            f"{length:g}×{width:g}×{thickness:g}mm"
        )
    else:
        summary = str(specification.get("summary") or "").strip()
        if not summary:
            raise HTTPException(status_code=422, detail="请填写其他外购包装的规格摘要")
        specification = {"summary": summary}
    code = str(payload.supplier_product_code or "").strip()
    normalized_code = _normalized_product_code(code)
    if not normalized_code:
        raise HTTPException(status_code=422, detail="供应商产品代码不能为空")
    return {
        "category_code": category,
        "supplier_product_code": code,
        "normalized_supplier_product_code": normalized_code,
        "product_name": str(payload.product_name or "").strip(),
        "purchase_unit": unit,
        "customer_scope_id": payload.customer_scope_id,
        "specification_summary": summary,
        "specification_json": json.dumps(specification, ensure_ascii=False, sort_keys=True),
        "drawing_sample_version": str(payload.drawing_sample_version or "").strip() or None,
        "lead_time_days": payload.lead_time_days,
        "remarks": str(payload.remarks or "").strip() or None,
    }


def _packaging_product_snapshot(row: ExternalPackagingProduct) -> dict[str, Any]:
    try:
        specification = json.loads(row.specification_json or "{}")
    except json.JSONDecodeError:
        specification = {}
    return {
        "id": row.id,
        "supplier_id": row.supplier_id,
        "category_code": row.category_code,
        "category_label": SUPPLIER_CATEGORY_LABELS.get(row.category_code, row.category_code),
        "supplier_product_code": row.supplier_product_code,
        "product_name": row.product_name,
        "purchase_unit": row.purchase_unit,
        "customer_scope_id": row.customer_scope_id,
        "customer_scope_name": (
            row.customer_scope.name if row.customer_scope is not None else None
        ),
        "specification_summary": row.specification_summary,
        "specification": specification,
        "drawing_sample_version": row.drawing_sample_version,
        "lead_time_days": row.lead_time_days,
        "remarks": row.remarks,
        "is_active": row.is_active,
        "version": row.version,
    }


def _packaging_product_or_404(
    db: Session, supplier_id: int, product_id: int
) -> ExternalPackagingProduct:
    row = db.scalar(
        select(ExternalPackagingProduct).options(
            selectinload(ExternalPackagingProduct.customer_scope)
        ).where(
            ExternalPackagingProduct.id == product_id,
            ExternalPackagingProduct.supplier_id == supplier_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="外购包装产品不存在")
    return row


def _require_customer_scope(
    db: Session, customer_scope_id: int | None
) -> None:
    if customer_scope_id is None:
        return
    customer = db.get(Customer, customer_scope_id)
    if (
        customer is None
        or getattr(customer, "status", "active") != "active"
        or not getattr(customer, "is_active", True)
    ):
        raise HTTPException(status_code=422, detail="客户专用范围必须选择有效客户")


def _require_active_supplier_category(supplier: Supplier, category: str) -> None:
    active = {
        row.category_code for row in supplier.supply_categories if row.is_active
    }
    if category not in active:
        raise HTTPException(
            status_code=409,
            detail=f"请先在供应商主档启用“{SUPPLIER_CATEGORY_LABELS[category]}”供货类别",
        )


@router.get("/{supplier_id}/packaging-products")
def list_packaging_products(
    supplier_id: int,
    keyword: str = Query(default=""),
    include_inactive: bool = Query(default=True),
    db: Session = Depends(get_db),
    _user: User = Depends(admin_only),
) -> dict:
    supplier = _supplier_or_404(db, supplier_id)
    statement = select(ExternalPackagingProduct).options(
        selectinload(ExternalPackagingProduct.customer_scope)
    ).where(
        ExternalPackagingProduct.supplier_id == supplier.id
    )
    if not include_inactive:
        statement = statement.where(ExternalPackagingProduct.is_active.is_(True))
    cleaned = keyword.strip()
    if cleaned:
        pattern = f"%{cleaned}%"
        statement = statement.where(
            or_(
                ExternalPackagingProduct.supplier_product_code.ilike(pattern),
                ExternalPackagingProduct.product_name.ilike(pattern),
                ExternalPackagingProduct.specification_summary.ilike(pattern),
            )
        )
    rows = db.scalars(
        statement.order_by(
            ExternalPackagingProduct.is_active.desc(),
            ExternalPackagingProduct.category_code,
            ExternalPackagingProduct.supplier_product_code,
            ExternalPackagingProduct.id,
        )
    ).all()
    return {"supplier": supplier_snapshot(supplier), "items": [_packaging_product_snapshot(row) for row in rows]}


@router.post("/{supplier_id}/packaging-products", status_code=status.HTTP_201_CREATED)
def create_packaging_product(
    supplier_id: int,
    payload: ExternalPackagingProductPayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    supplier = _supplier_or_404(db, supplier_id)
    if not supplier.is_active:
        raise HTTPException(status_code=409, detail="供应商已停用，不能新增外购产品")
    values = _clean_external_product_payload(payload)
    _require_customer_scope(db, values["customer_scope_id"])
    _require_active_supplier_category(supplier, values["category_code"])
    row = ExternalPackagingProduct(
        supplier_id=supplier.id, **values, is_active=True, version=1
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="该供应商下已存在相同产品代码") from error
    after = _packaging_product_snapshot(row)
    audit_master_change(
        db, user=user, action="CREATE", resource="ExternalPackagingProduct",
        resource_id=row.id, details={"supplier_id": supplier.id, "after": after},
    )
    db.commit()
    db.refresh(row)
    return _packaging_product_snapshot(row)


@router.put("/{supplier_id}/packaging-products/{product_id}")
def update_packaging_product(
    supplier_id: int,
    product_id: int,
    payload: ExternalPackagingProductUpdatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    supplier = _supplier_or_404(db, supplier_id)
    row = _packaging_product_or_404(db, supplier.id, product_id)
    if row.version != payload.expected_version:
        raise HTTPException(status_code=409, detail="外购产品资料已更新，请刷新后再保存")
    values = _clean_external_product_payload(payload)
    _require_customer_scope(db, values["customer_scope_id"])
    _require_active_supplier_category(supplier, values["category_code"])
    before = _packaging_product_snapshot(row)
    try:
        result = db.execute(
            update(ExternalPackagingProduct)
            .where(
                ExternalPackagingProduct.id == row.id,
                ExternalPackagingProduct.version == payload.expected_version,
            )
            .values(**values, version=payload.expected_version + 1)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            db.rollback()
            raise HTTPException(status_code=409, detail="外购产品资料版本冲突")
        db.refresh(row)
        after = _packaging_product_snapshot(row)
        audit_master_change(
            db, user=user, action="UPDATE", resource="ExternalPackagingProduct",
            resource_id=row.id, details={"supplier_id": supplier.id, "before": before, "after": after},
        )
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="该供应商下已存在相同产品代码") from error
    return _packaging_product_snapshot(_packaging_product_or_404(db, supplier.id, row.id))


@router.put("/{supplier_id}/packaging-products/{product_id}/status")
def update_packaging_product_status(
    supplier_id: int,
    product_id: int,
    payload: ExternalPackagingProductStatusPayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    supplier = _supplier_or_404(db, supplier_id)
    row = _packaging_product_or_404(db, supplier.id, product_id)
    if row.version != payload.expected_version:
        raise HTTPException(status_code=409, detail="外购产品资料已更新，请刷新后再操作")
    if payload.is_active:
        _require_active_supplier_category(supplier, row.category_code)
    if row.is_active == payload.is_active:
        return _packaging_product_snapshot(row)
    before = _packaging_product_snapshot(row)
    result = db.execute(
        update(ExternalPackagingProduct)
        .where(
            ExternalPackagingProduct.id == row.id,
            ExternalPackagingProduct.version == payload.expected_version,
        )
        .values(is_active=payload.is_active, version=payload.expected_version + 1)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="外购产品资料版本冲突")
    db.refresh(row)
    after = _packaging_product_snapshot(row)
    audit_master_change(
        db, user=user, action="ACTIVATE" if payload.is_active else "DEACTIVATE",
        resource="ExternalPackagingProduct", resource_id=row.id,
        details={"supplier_id": supplier.id, "before": before, "after": after},
    )
    db.commit()
    return _packaging_product_snapshot(_packaging_product_or_404(db, supplier.id, row.id))
