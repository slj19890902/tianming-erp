from __future__ import annotations
from app.core.sheet_dimensions import SheetDimension, sheet_dimension_number, validate_sheet_dimensions

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
from app.services.box_type_rules import (
    BoxTypeRuleError,
    canonical_box_style,
    get_box_type_rule,
    normalize_box_configuration,
    recommend_box_type,
)
from app.services.report_crease import crease_width_error
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
    report_length_mm: SheetDimension | None = None
    report_width_mm: SheetDimension | None = None
    crease_type: str | None = None
    crease_left_mm: int | None = None
    crease_middle_mm: int | None = None
    crease_right_mm: int | None = None
    base_report_length_mm: SheetDimension | None = None
    base_report_width_mm: SheetDimension | None = None
    base_crease_type: str | None = None
    base_crease_left_mm: int | None = None
    base_crease_middle_mm: int | None = None
    base_crease_right_mm: int | None = None
    splice_mode: str | None = None
    pieces_per_box: int | None = None
    flap_mm: int | None = None
    default_cutting_mode: str | None = None


def _marker(client_line_id: str) -> str:
    return f"{MANUAL_SIZE_PRODUCT_REMARK_PREFIX}|line:{client_line_id}"


def _validate_input(
    db: Session,
    customer: Customer,
    data: ManualSizeProductInput,
) -> tuple[Material, str, dict[str, object]]:
    if not data.client_line_id.strip():
        raise ManualSizeProductError("手工尺寸订单必须提供明细幂等标识")
    requested_box_style = str(data.box_type or "").strip()
    box_style = canonical_box_style(requested_box_style)
    rule = get_box_type_rule(box_style)
    if not requested_box_style or not box_style or rule is None:
        raise ManualSizeProductError("手工尺寸订单必须选择系统已登记的箱型")
    dimensions = {
        "length_mm": data.length_mm,
        "width_mm": data.width_mm,
        "height_mm": data.height_mm,
    }
    missing_dimensions = [
        name
        for name in rule.required_dimensions
        if dimensions.get(name) is None or int(dimensions[name] or 0) <= 0
    ]
    if missing_dimensions:
        labels = {"length_mm": "长", "width_mm": "宽", "height_mm": "高"}
        raise ManualSizeProductError(
            "手工尺寸订单必须填写正数的"
            + "、".join(labels[name] for name in missing_dimensions)
        )
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
    saved_preferences = db.scalars(
        select(CustomerQuotePreference)
        .where(
            CustomerQuotePreference.customer_id == customer.id,
            CustomerQuotePreference.material_id == material.id,
            CustomerQuotePreference.flute_type == flute_type,
            CustomerQuotePreference.is_active.is_(True),
        )
    ).all()
    if not any(
        canonical_box_style(row.box_type) == box_style
        for row in saved_preferences
    ):
        raise ManualSizeProductError(
            "手工尺寸订单只能选择该客户报价偏好中已保存并启用的箱型、层数、楞型、供应商和材质代码"
        )
    try:
        recommendation = recommend_box_type(
            box_style=box_style,
            length_mm=data.length_mm,
            width_mm=data.width_mm,
            height_mm=data.height_mm,
            splice_mode=data.splice_mode or "single",
            flap_mm=data.flap_mm,
            crease_type=data.crease_type,
        )
        configuration = normalize_box_configuration(
            box_style=box_style,
            splice_mode=data.splice_mode or str(recommendation.get("splice_mode") or "single"),
            pieces_per_box=data.pieces_per_box,
            flap_mm=data.flap_mm if data.flap_mm is not None else recommendation.get("flap_mm"),
            default_cutting_mode=data.default_cutting_mode or "一开一",
            crease_type=data.crease_type or recommendation.get("crease_type"),
        )
    except BoxTypeRuleError as error:
        raise ManualSizeProductError(str(error)) from error

    def chosen(name: str) -> object:
        explicit = getattr(data, name)
        return explicit if explicit is not None else recommendation.get(name)

    report_length = chosen("report_length_mm")
    report_width = chosen("report_width_mm")
    if report_length is None or report_width is None or sheet_dimension_number(report_length) <= 0 or sheet_dimension_number(report_width) <= 0:
        raise ManualSizeProductError(
            "当前箱型没有完整自动公式，必须人工填写正数的报料长和报料宽"
        )
    crease_type = chosen("crease_type")
    crease_values = {
        "crease_left_mm": chosen("crease_left_mm"),
        "crease_middle_mm": chosen("crease_middle_mm"),
        "crease_right_mm": chosen("crease_right_mm"),
    }
    error = crease_width_error(
        label="压线",
        crease_type=str(crease_type or "").strip() or None,
        report_width_mm=sheet_dimension_number(report_width),
        left_mm=crease_values["crease_left_mm"],
        middle_mm=crease_values["crease_middle_mm"],
        right_mm=crease_values["crease_right_mm"],
    )
    if error:
        raise ManualSizeProductError(error)
    if rule.code == "a1_0201" and crease_type == "压线" and (
        int(crease_values["crease_left_mm"] or 0)
        != int(crease_values["crease_right_mm"] or 0)
    ):
        raise ManualSizeProductError("A1 压线上摇盖与下摇盖必须保持一致")

    base_report_length = chosen("base_report_length_mm")
    base_report_width = chosen("base_report_width_mm")
    base_crease_type = chosen("base_crease_type")
    if base_report_length is not None or base_report_width is not None:
        if (
            base_report_length is None
            or base_report_width is None
            or sheet_dimension_number(base_report_length) <= 0
            or sheet_dimension_number(base_report_width) <= 0
        ):
            raise ManualSizeProductError("天地盖底片必须同时填写正数的报料长和报料宽")
        error = crease_width_error(
            label="底压线",
            crease_type=str(base_crease_type or "").strip() or None,
            report_width_mm=sheet_dimension_number(base_report_width),
            left_mm=chosen("base_crease_left_mm"),
            middle_mm=chosen("base_crease_middle_mm"),
            right_mm=chosen("base_crease_right_mm"),
        )
        if error:
            raise ManualSizeProductError(f"天地盖底片{error}")

    validate_sheet_dimensions(report_length, report_width, base_report_length, base_report_width,
                              layer_count=data.layer_count, flute_type=data.flute_type)
    normalized = {
        "box_style": requested_box_style,
        "report_length_mm": sheet_dimension_number(report_length),
        "report_width_mm": sheet_dimension_number(report_width),
        "crease_type": str(crease_type or "").strip() or None,
        **{
            key: (int(value) if value is not None else None)
            for key, value in crease_values.items()
        },
        "base_report_length_mm": sheet_dimension_number(base_report_length) if base_report_length is not None else None,
        "base_report_width_mm": sheet_dimension_number(base_report_width) if base_report_width is not None else None,
        "base_crease_type": str(base_crease_type or "").strip() or None,
        "base_crease_left_mm": int(chosen("base_crease_left_mm")) if chosen("base_crease_left_mm") is not None else None,
        "base_crease_middle_mm": int(chosen("base_crease_middle_mm")) if chosen("base_crease_middle_mm") is not None else None,
        "base_crease_right_mm": int(chosen("base_crease_right_mm")) if chosen("base_crease_right_mm") is not None else None,
        "splice_mode": configuration["splice_mode"],
        "pieces_per_box": configuration["pieces_per_box"],
        "flap_mm": configuration["flap_mm"],
        "default_cutting_mode": configuration["default_cutting_mode"],
    }
    return material, flute_type, normalized


def _same_manual_size_product(
    product: Product,
    data: ManualSizeProductInput,
    *,
    flute_type: str,
    normalized: dict[str, object],
) -> bool:
    def dimension(value: int | None) -> Decimal | None:
        return Decimal(str(value)) if value is not None else None

    return (
        canonical_box_style(product.box_style)
        == canonical_box_style(str(normalized["box_style"]))
        and product.material_id == data.material_id
        and product.layer_count == data.layer_count
        and normalize_flute_type(product.flute_type) == flute_type
        and product.length_mm == dimension(data.length_mm)
        and product.width_mm == dimension(data.width_mm)
        and product.height_mm == dimension(data.height_mm)
        and product.sale_unit_price == data.sale_unit_price
        and all(
            getattr(product, field) == normalized[field]
            for field in (
                "report_length_mm", "report_width_mm", "crease_type",
                "crease_left_mm", "crease_middle_mm", "crease_right_mm",
                "base_report_length_mm", "base_report_width_mm", "base_crease_type",
                "base_crease_left_mm", "base_crease_middle_mm", "base_crease_right_mm",
                "splice_mode", "pieces_per_box", "flap_mm", "default_cutting_mode",
            )
        )
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
    material, flute_type, normalized = _validate_input(db, customer, data)
    marker = _marker(data.client_line_id.strip())
    existing = db.scalar(
        select(Product)
        .where(Product.customer_id == customer.id, Product.remark == marker)
        .order_by(Product.id)
        .limit(1)
    )
    if existing is not None:
        if not _same_manual_size_product(
            existing, data, flute_type=flute_type, normalized=normalized
        ):
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
            length_mm=(Decimal(str(data.length_mm)) if data.length_mm is not None else None),
            width_mm=(Decimal(str(data.width_mm)) if data.width_mm is not None else None),
            height_mm=(Decimal(str(data.height_mm)) if data.height_mm is not None else None),
            layer_count=material.layer_count,
            flute_type=flute_type,
            box_category="normal",
            box_style=str(normalized["box_style"]),
            sale_unit_price=data.sale_unit_price,
            report_length_mm=normalized["report_length_mm"],
            report_width_mm=normalized["report_width_mm"],
            crease_type=normalized["crease_type"],
            crease_left_mm=normalized["crease_left_mm"],
            crease_middle_mm=normalized["crease_middle_mm"],
            crease_right_mm=normalized["crease_right_mm"],
            base_report_length_mm=normalized["base_report_length_mm"],
            base_report_width_mm=normalized["base_report_width_mm"],
            base_crease_type=normalized["base_crease_type"],
            base_crease_left_mm=normalized["base_crease_left_mm"],
            base_crease_middle_mm=normalized["base_crease_middle_mm"],
            base_crease_right_mm=normalized["base_crease_right_mm"],
            splice_mode=normalized["splice_mode"],
            pieces_per_box=normalized["pieces_per_box"],
            flap_mm=normalized["flap_mm"],
            default_cutting_mode=normalized["default_cutting_mode"],
            sheet_cutting_settings={"schema_version": 2, "whole": {
                "length_parts": 1, "width_parts": 1, "mold_count": 1, "is_die_cut": False}},
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
