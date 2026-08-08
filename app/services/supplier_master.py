from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.supplier import Supplier, SupplierAlias


SUPPLIER_CATEGORY_LABELS = {
    "corrugated_board": "瓦楞纸板",
    "paper_corner_guard": "纸护角",
    "coated_board": "涂布白板/灰底白",
    "printed_folding_carton": "印刷折叠彩盒",
    "epe_cushion": "EPE缓冲包装",
    "other_packaging": "其他外购包装",
}


class SupplierLookupError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def normalize_supplier_identity(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).casefold()


def clean_supplier_name(value: object) -> str:
    return str(value or "").strip()


def clean_business_code(value: object) -> str | None:
    compact = re.sub(r"\s+", "", str(value or "").strip()).upper()
    return compact or None


def supplier_snapshot(supplier: Supplier) -> dict:
    category_rows = list(supplier.supply_categories)
    return {
        "id": supplier.id,
        "standard_name": supplier.standard_name,
        "display_name": supplier.display_name,
        "business_code": supplier.business_code,
        "contact_name": supplier.contact_name,
        "phone": supplier.phone,
        "remarks": supplier.remarks,
        "sort_order": supplier.sort_order,
        "is_active": supplier.is_active,
        "version": supplier.version,
        "aliases": [alias.alias_name for alias in supplier.aliases],
        "supply_categories": (
            [row.category_code for row in category_rows if row.is_active]
            if category_rows
            else ["corrugated_board"]
        ),
    }


def resolve_supplier(
    db: Session,
    value: object,
    *,
    require_active: bool = True,
) -> Supplier:
    supplied_name = clean_supplier_name(value)
    normalized = normalize_supplier_identity(supplied_name)
    if not normalized:
        raise SupplierLookupError(
            "SUPPLIER_REQUIRED",
            "供应商不能为空",
        )

    supplier = db.scalar(
        select(Supplier)
        .options(
            selectinload(Supplier.aliases),
            selectinload(Supplier.supply_categories),
        )
        .where(Supplier.normalized_name == normalized)
    )
    if supplier is None:
        supplier = db.scalar(
            select(Supplier)
            .join(SupplierAlias)
            .options(
                selectinload(Supplier.aliases),
                selectinload(Supplier.supply_categories),
            )
            .where(SupplierAlias.normalized_alias == normalized)
        )
    if supplier is None:
        raise SupplierLookupError(
            "SUPPLIER_NOT_REGISTERED",
            f"供应商“{supplied_name}”尚未建档，请先在供应商维护中新增并启用",
        )
    if require_active and not supplier.is_active:
        raise SupplierLookupError(
            "SUPPLIER_DISABLED",
            f"供应商“{supplier.display_name or supplier.standard_name}”已停用，不能用于新业务",
        )
    return supplier
