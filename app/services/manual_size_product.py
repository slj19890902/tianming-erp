from __future__ import annotations

import secrets
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.time_contract import beijing_today
from app.models.customer import Customer
from app.models.customer_quote_preference import CustomerQuotePreference
from app.models.material import Material
from app.models.product import Product
from app.models.user import User
from app.services.flute_mapping import normalize_flute_type, validate_flute_for_write
from app.services.master_data_versioning import record_versioned_create
from app.services.supplier_master import SupplierLookupError, resolve_supplier


MANUAL_SIZE_PRODUCT_REMARK_PREFIX = "手工尺寸订单创建常用箱"


class ManualSizeProductError(ValueError):
    """The manual-size line cannot become a safe, traceable common box."""


@dataclass(frozen=True)
class ManualSizeProductInput:
    client_line_id: str
    box_type: str | None
    product_name: str | None
    length_mm: int | None
    width_mm: int | None
    height_mm: int | None
    material_id: int | None
    layer_count: int | None
    flute_type: str | None
    sale_unit_price: Decimal


def _marker(client_line_id: str) -> str:
    return f"{MANUAL_SIZE_PRODUCT_REMARK_PREFIX}|line:{client_line_id}"


def _validate_input(
    db: Session,
    customer: Customer,
    data: ManualSizeProductInput,
) -> tuple[Material, str]:
    if not data.client_line_id.strip():
        raise ManualSizeProductError("手工尺寸订单必须提供明细幂等标识")
    if (data.box_type or "").strip().upper() != "A1":
        raise ManualSizeProductError("手工尺寸订单目前仅支持 A1 箱型")
    if any(value is None or int(value) <= 0 for value in (
        data.length_mm, data.width_mm, data.height_mm,
    )):
        raise ManualSizeProductError("手工尺寸订单必须填写正数的长、宽、高")
    if data.material_id is None:
        raise ManualSizeProductError("手工尺寸订单必须明确选择材质，系统不会猜测材质")
    material = db.get(Material, data.material_id)
    if material is None:
        raise ManualSizeProductError("手工尺寸订单所选材质不存在")
    if not material.is_active:
        raise ManualSizeProductError("手工尺寸订单所选材质已停用，请重新选择")
    try:
        resolve_supplier(db, material.supplier_name, require_active=True)
    except SupplierLookupError as error:
        raise ManualSizeProductError(error.message) from error
    if data.layer_count is None or int(data.layer_count) != int(material.layer_count):
        raise ManualSizeProductError("手工尺寸订单层数必须与所选材质真实层数一致")
    flute_type = normalize_flute_type(data.flute_type)
    if not flute_type:
        raise ManualSizeProductError("手工尺寸订单必须明确选择楞型，系统不会猜测楞型")
    error = validate_flute_for_write(flute_type, material.layer_count)
    if error:
        raise ManualSizeProductError(error)
    saved_preference = db.scalar(
        select(CustomerQuotePreference.id)
        .where(
            CustomerQuotePreference.customer_id == customer.id,
            CustomerQuotePreference.box_type == "A1",
            CustomerQuotePreference.material_id == material.id,
            CustomerQuotePreference.flute_type == flute_type,
            CustomerQuotePreference.is_active.is_(True),
        )
        .limit(1)
    )
    if saved_preference is None:
        raise ManualSizeProductError(
            "手工尺寸订单只能选择该客户报价偏好中已保存并启用的箱型、层数、楞型、供应商和材质代码"
        )
    return material, flute_type


def _same_manual_size_product(
    product: Product,
    data: ManualSizeProductInput,
    *,
    flute_type: str,
) -> bool:
    return (
        product.box_style == "A1"
        and product.material_id == data.material_id
        and product.layer_count == data.layer_count
        and normalize_flute_type(product.flute_type) == flute_type
        and product.length_mm == Decimal(str(data.length_mm))
        and product.width_mm == Decimal(str(data.width_mm))
        and product.height_mm == Decimal(str(data.height_mm))
        and product.sale_unit_price == data.sale_unit_price
    )


def _new_internal_code(customer: Customer) -> str:
    # Internal code, deliberately not a guessed customer inventory code.
    return f"SZ-{customer.id}-{beijing_today():%Y%m%d}-{secrets.token_hex(3).upper()}"


def resolve_or_create_manual_size_product(
    db: Session,
    *,
    customer: Customer,
    data: ManualSizeProductInput,
    user: User,
) -> Product:
    """Create a versioned A1 common box only for an explicitly marked manual line.

    The client line marker makes a retry reuse the same common box instead of
    silently generating a second code.  The surrounding order transaction owns
    commit/rollback, so a failed order cannot leave this product behind.
    """
    material, flute_type = _validate_input(db, customer, data)
    marker = _marker(data.client_line_id.strip())
    existing = db.scalar(
        select(Product)
        .where(Product.customer_id == customer.id, Product.remark == marker)
        .order_by(Product.id)
        .limit(1)
    )
    if existing is not None:
        if not _same_manual_size_product(existing, data, flute_type=flute_type):
            raise ManualSizeProductError(
                "同一手工明细幂等标识的尺寸、材质、楞型或单价已不同，不能重复保存"
            )
        return existing

    for _attempt in range(16):
        code = _new_internal_code(customer)
        duplicate = db.scalar(
            select(Product.id)
            .where(
                Product.customer_id == customer.id,
                or_(Product.product_code == code, Product.customer_material_code == code),
            )
            .limit(1)
        )
        if duplicate is not None:
            continue
        product = Product(
            customer_id=customer.id,
            product_code=code,
            customer_material_code=code,
            product_name=(data.product_name or "").strip() or "纸箱",
            material_id=material.id,
            default_material_code=material.code,
            length_mm=Decimal(str(data.length_mm)),
            width_mm=Decimal(str(data.width_mm)),
            height_mm=Decimal(str(data.height_mm)),
            layer_count=material.layer_count,
            flute_type=flute_type,
            box_category="normal",
            box_style="A1",
            sale_unit_price=data.sale_unit_price,
            remark=marker,
            is_active=True,
        )
        # Keep this flush in the caller's transaction.  In SQLite a savepoint
        # can otherwise become the first write transaction and its release may
        # commit the product before later order validation has completed.
        db.add(product)
        db.flush()
        record_versioned_create(
            db,
            object_type="product",
            entity=product,
            user=user,
            reason="手工尺寸订单创建常用箱",
            source="orders.create.manual-size",
        )
        return product
    raise ManualSizeProductError("手工尺寸常用箱内部编码生成冲突，请重试保存")
