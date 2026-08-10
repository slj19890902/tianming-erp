from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.material import Material
from app.models.product import Product
from app.models.user import User
from app.services.flute_mapping import normalize_flute_type

AUTO_CREATE_REMARK = "PDF导入自动创建（待补全报料信息）"


class NewProductError(ValueError):
    """Raised when an auto-created product is missing required fields."""


_DIM_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*[×xX*]\s*(\d+(?:\.\d+)?)(?:\s*[×xX*]\s*(\d+(?:\.\d+)?))?"
)


def _norm(value: str | None) -> str:
    return re.sub(r"[\s（）()\-—_·,，.。/\\\"'×xX*]", "", value or "").casefold()


def parse_dimensions(
    spec: str | None,
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    match = _DIM_RE.search(spec or "")
    if not match:
        return (None, None, None)
    return tuple(
        Decimal(group) if group is not None else None for group in match.groups()
    )  # type: ignore[return-value]


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", "", (value or "")).strip()


@dataclass(frozen=True)
class NewProductInput:
    inventory_code: str
    product_name: str
    specification: str | None = None
    material_id: int | None = None
    length_mm: Decimal | None = None
    width_mm: Decimal | None = None
    height_mm: Decimal | None = None
    sale_unit_price: Decimal | None = None
    layer_count: int | None = None
    flute_type: str | None = None


def _dedup_key(customer_id: int, data: NewProductInput) -> str:
    code = _norm(data.inventory_code)
    if code:
        return f"{customer_id}|code|{code}|name|{_norm(data.product_name)}"
    dims = "x".join(
        format(value, "f").rstrip("0").rstrip(".")
        for value in (data.length_mm, data.width_mm, data.height_mm)
        if value is not None
    )
    return (
        f"{customer_id}|ns|{_norm(data.product_name)}|"
        f"{_norm(data.specification)}|{dims}|{data.material_id or ''}"
    )


def _find_existing(
    db: Session,
    customer_id: int,
    data: NewProductInput,
) -> Product | None:
    code = _clean(data.inventory_code)
    if code:
        products = db.scalars(
            select(Product)
            .where(
                Product.customer_id == customer_id,
                Product.deleted_at.is_(None),
                or_(
                    Product.customer_material_code == code,
                    Product.product_code == code,
                ),
            )
            .order_by(Product.id)
        ).all()
        target_name = _norm(data.product_name)
        exact = [
            product
            for product in products
            if target_name and _norm(product.product_name) == target_name
        ]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise NewProductError("同一存货编码和产品名称命中多个常用箱，请先整理主档")

    target_name = _norm(data.product_name)
    if not target_name:
        return None
    target_spec = _norm(data.specification)
    target_dims = (data.length_mm, data.width_mm, data.height_mm)
    has_dims = all(value is not None for value in target_dims)
    candidates = db.scalars(
        select(Product)
        .where(
            Product.customer_id == customer_id,
            Product.deleted_at.is_(None),
        )
        .order_by(Product.id)
    ).all()
    spec_match: Product | None = None
    for product in candidates:
        if _norm(product.product_name) != target_name:
            continue
        product_dims = (product.length_mm, product.width_mm, product.height_mm)
        if (
            has_dims
            and all(value is not None for value in product_dims)
            and tuple(product_dims) == target_dims
            and product.material_id == data.material_id
        ):
            return product
        if target_spec and spec_match is None:
            product_spec = _norm(
                "×".join(
                    format(value, "f").rstrip("0").rstrip(".")
                    for value in product_dims
                    if value is not None
                )
            )
            if product_spec and (target_spec in product_spec or product_spec in target_spec):
                spec_match = product
    return spec_match


def resolve_or_create_product(
    db: Session,
    *,
    customer: Customer,
    data: NewProductInput,
    cache: dict[str, Product],
    user: User,
    reason: str,
    source: str,
) -> Product:
    code = _clean(data.inventory_code)
    name = (data.product_name or "").strip()
    if not code:
        raise NewProductError("缺少存货编码（新产品需填写）")
    if not name:
        raise NewProductError("缺少产品名称（新产品需填写）")

    key = _dedup_key(customer.id, data)
    if key in cache:
        return cache[key]

    existing = _find_existing(db, customer.id, data)
    if existing is not None:
        cache[key] = existing
        return existing

    material = db.get(Material, data.material_id) if data.material_id is not None else None
    material_id = material.id if material is not None else None
    layer_count = material.layer_count if material is not None else data.layer_count

    product = Product(
        customer_id=customer.id,
        product_code=code,
        customer_material_code=code,
        product_name=name,
        material_id=material_id,
        length_mm=data.length_mm,
        width_mm=data.width_mm,
        height_mm=data.height_mm,
        layer_count=layer_count,
        flute_type=normalize_flute_type(data.flute_type),
        box_category="normal",
        sale_unit_price=data.sale_unit_price,
        remark=AUTO_CREATE_REMARK,
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
        reason=reason,
        source=source,
    )
    cache[key] = product
    return product
