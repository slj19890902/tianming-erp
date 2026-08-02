from __future__ import annotations

import csv
from datetime import date
from hashlib import sha256
from io import StringIO
import json
from typing import Any, Iterable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.time_contract import (
    beijing_now_naive,
    beijing_today,
    utc_naive_to_api,
    utc_now_naive,
)
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.inventory_onboarding import (
    InventoryOnboardingBatch,
    InventoryOnboardingLine,
)
from app.models.material import Material
from app.models.product import Product
from app.models.user import User
from app.models.warehouse_inventory import (
    InventoryLot,
    InventoryPallet,
    InventoryPalletItem,
    WarehouseLocation,
)
from app.services.inventory_onboarding_uploads import (
    InventoryOnboardingRawRow,
    PRIVATE_REFERENCE_PREFIX,
    StoredInventoryOnboardingUpload,
)
from app.services.secure_uploads import resolve_stored_reference
from app.services.warehouse_inventory import (
    SEMI_FINISHED_FLUTES_BY_LAYER,
    WarehouseInventoryError,
    normalize_material_code,
    normalize_stock_date_metadata,
)


class InventoryOnboardingError(ValueError):
    def __init__(
        self,
        message: str,
        status_code: int = 400,
        code: str = "INVENTORY_ONBOARDING_INVALID",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


HEADER_ALIASES: dict[str, str] = {
    "现场序号": "field_sequence",
    "盘点序号": "field_sequence",
    "序号": "field_sequence",
    "field_sequence": "field_sequence",
    "盘点日期": "stocktake_date",
    "stocktake_date": "stocktake_date",
    "盘点人": "stocktaker_name",
    "盘点人员": "stocktaker_name",
    "stocktaker_name": "stocktaker_name",
    "库存类型": "inventory_type",
    "inventory_type": "inventory_type",
    "归属类型": "ownership_type",
    "库存归属": "ownership_type",
    "ownership_type": "ownership_type",
    "库位id": "location_id",
    "location_id": "location_id",
    "楼层": "floor",
    "warehouse_floor": "floor",
    "区域": "area_code",
    "区域编码": "area_code",
    "area_code": "area_code",
    "库位": "location_code",
    "库位编码": "location_code",
    "位置": "location_code",
    "当前位置": "location_code",
    "当前位置(不用改)": "location_code",
    "系统位置(仅参考)": "location_code",
    "location_code": "location_code",
    "现场位置": "position_note",
    "现场位置/顺序": "position_note",
    "大概位置(可空)": "position_note",
    "大概位置（可空）": "position_note",
    "位置备注": "position_note",
    "position_note": "position_note",
    "栈板": "pallet_code",
    "栈板号": "pallet_code",
    "pallet_code": "pallet_code",
    "客户id": "customer_id",
    "customer_id": "customer_id",
    "客户编码": "customer_code",
    "customer_code": "customer_code",
    "客户名称": "customer_name",
    "客户": "customer_name",
    "大概客户": "customer_name",
    "客户(大概即可)": "customer_name",
    "customer_name": "customer_name",
    "产品id": "product_id",
    "product_id": "product_id",
    "存货编码": "inventory_code",
    "产品编码": "inventory_code",
    "product_code": "inventory_code",
    "inventory_code": "inventory_code",
    "存货编码或产品": "inventory_hint",
    "产品或存货编码": "inventory_hint",
    "产品名称或存货编码": "inventory_hint",
    "产品名称或者存货编码": "inventory_hint",
    "对应产品名称或存货编码": "inventory_hint",
    "对应的产品名称或存货编码": "inventory_hint",
    "对应的产品名称或者存货编码": "inventory_hint",
    "inventory_hint": "inventory_hint",
    "产品名称": "product_name",
    "产品": "product_name",
    "品名": "product_name",
    "货物名称": "product_name",
    "product_name": "product_name",
    "材质id": "material_id",
    "material_id": "material_id",
    "材质编码": "material_code",
    "材质": "material_code",
    "material_code": "material_code",
    "数量": "quantity",
    "现场数量": "quantity",
    "现场数量(只改有差异)": "quantity",
    "现场数量（只改有差异）": "quantity",
    "实际数量": "quantity",
    "盘点数量": "quantity",
    "对应数量": "quantity",
    "对应的数量": "quantity",
    "quantity": "quantity",
    "系统数量": "system_quantity",
    "系统数量(仅参考)": "system_quantity",
    "system_quantity": "system_quantity",
    "库存批次id": "existing_lot_id",
    "inventory_lot_id": "existing_lot_id",
    "existing_lot_id": "existing_lot_id",
    "库存版本": "existing_lot_version",
    "existing_lot_version": "existing_lot_version",
    "可用数量": "existing_available",
    "existing_available": "existing_available",
    "预占数量": "existing_reserved",
    "existing_reserved": "existing_reserved",
    "单位": "unit",
    "unit": "unit",
    "入库日期": "stock_date",
    "技术日期": "stock_date",
    "stock_date": "stock_date",
    "日期可信度": "stock_date_accuracy",
    "stock_date_accuracy": "stock_date_accuracy",
    "入库日期原文": "stock_date_original_text",
    "日期原文": "stock_date_original_text",
    "stock_date_original_text": "stock_date_original_text",
    "供应商": "supplier_name",
    "supplier_name": "supplier_name",
    "层数": "layer_count",
    "layer_count": "layer_count",
    "楞型": "flute_type",
    "flute_type": "flute_type",
    "纸板长": "board_length_mm",
    "长(mm)": "board_length_mm",
    "board_length_mm": "board_length_mm",
    "纸板宽": "board_width_mm",
    "宽(mm)": "board_width_mm",
    "board_width_mm": "board_width_mm",
    "片料类型": "sheet_type",
    "sheet_type": "sheet_type",
    "组件类型": "component_type",
    "component_type": "component_type",
    "每箱片数": "pieces_per_box",
    "pieces_per_box": "pieces_per_box",
    "每张产出": "stock_yield_per_sheet",
    "每张纸产出": "stock_yield_per_sheet",
    "stock_yield_per_sheet": "stock_yield_per_sheet",
    "压线类型": "crease_type",
    "crease_type": "crease_type",
    "左压线": "crease_left_mm",
    "crease_left_mm": "crease_left_mm",
    "中压线": "crease_middle_mm",
    "crease_middle_mm": "crease_middle_mm",
    "右压线": "crease_right_mm",
    "crease_right_mm": "crease_right_mm",
    "开料备注": "cutting_note",
    "cutting_note": "cutting_note",
    "备注": "remarks",
    "remarks": "remarks",
    "处理决定": "action_decision",
    "action_decision": "action_decision",
}

INVENTORY_TYPE_VALUES = {
    "成品": "finished",
    "finished": "finished",
    "半成品": "semi_finished",
    "semi_finished": "semi_finished",
    "semi-finished": "semi_finished",
}
OWNERSHIP_VALUES = {
    "客户专用": "customer_specific",
    "专用": "customer_specific",
    "customer_specific": "customer_specific",
    "dedicated": "customer_specific",
    "通用": "general",
    "general": "general",
}
UNIT_VALUES = {
    "箱": "boxes",
    "箱/个": "boxes",
    "个": "boxes",
    "boxes": "boxes",
    "张": "sheets",
    "片": "sheets",
    "sheets": "sheets",
}
DATE_ACCURACY_VALUES = {
    "精确": "exact",
    "准确": "exact",
    "exact": "exact",
    "估算": "estimated",
    "estimated": "estimated",
    "不明": "unknown",
    "未知": "unknown",
    "unknown": "unknown",
}
SHEET_TYPE_VALUES = {
    "原纸板": "raw_board",
    "毛板": "raw_board",
    "raw_board": "raw_board",
    "净片": "net_sheet",
    "净料": "net_sheet",
    "net_sheet": "net_sheet",
    "压线片": "creased_sheet",
    "压线": "creased_sheet",
    "creased_sheet": "creased_sheet",
}
COMPONENT_VALUES = {
    "整片": "whole",
    "整张": "whole",
    "whole": "whole",
    "盖": "cover",
    "cover": "cover",
    "底": "base",
    "base": "base",
}
EDITABLE_ACTIONS = frozenset({"pending", "create_new", "exclude"})
UNKNOWN_FACT_MARKERS = frozenset(
    {"?", "？", "-", "未知", "不知道", "不清楚", "不详", "记不清"}
)
CUSTOMER_RESOLUTION_ERRORS = frozenset(
    {
        "CUSTOMER_NOT_FOUND",
        "CUSTOMER_AMBIGUOUS",
    }
)


def _text(value: object) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


def _fact_text(value: object) -> str | None:
    rendered = _text(value)
    if rendered is None or rendered.casefold() in UNKNOWN_FACT_MARKERS:
        return None
    return rendered


def _integer(value: object) -> int | None:
    if value is None or _text(value) is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    integer = int(number)
    return integer if number == integer else None


def _date(value: object) -> date | None:
    text = _text(value)
    if not text:
        return None
    normalized = text.replace("/", "-").replace(".", "-")
    try:
        return date.fromisoformat(normalized[:10])
    except ValueError:
        return None


def _enum(value: object, mapping: dict[str, str]) -> str | None:
    text = _text(value)
    if text is None:
        return None
    return mapping.get(text.lower()) or mapping.get(text)


def _canonical_header(value: object, index: int) -> str:
    text = _text(value)
    if text is None:
        return f"column_{index}"
    normalized = text.replace("（", "(").replace("）", ")").strip()
    return HEADER_ALIASES.get(normalized.lower(), normalized)


def _source_hash(
    upload_sha256: str,
    row: InventoryOnboardingRawRow,
) -> str:
    payload = "\x1f".join(
        (
            upload_sha256,
            row.sheet_name,
            str(row.row_number),
            row.raw_text,
        )
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _batch_number(source_sha256: str) -> str:
    return f"IOB-{beijing_now_naive():%Y%m%d}-{source_sha256[:10].upper()}"


def _batch_lines(db: Session, batch_id: int) -> list[InventoryOnboardingLine]:
    return db.scalars(
        select(InventoryOnboardingLine)
        .where(InventoryOnboardingLine.batch_id == batch_id)
        .order_by(
            InventoryOnboardingLine.source_sheet_name,
            InventoryOnboardingLine.source_row_number,
            InventoryOnboardingLine.id,
        )
    ).all()


def _source_mapped(line: InventoryOnboardingLine) -> dict[str, object]:
    source = line.original_values_json or {}
    mapped = source.get("mapped", {})
    return mapped if isinstance(mapped, dict) else {}


def _line_from_source(
    *,
    batch: InventoryOnboardingBatch,
    upload_sha256: str,
    row: InventoryOnboardingRawRow,
    headers: tuple[str, ...],
    updated_by: int,
) -> InventoryOnboardingLine:
    values = tuple(row.original_values)
    mapped = {
        header: values[index] if index < len(values) else None
        for index, header in enumerate(headers)
    }
    original = {
        "headers": list(headers),
        "values": list(values),
        "mapped": mapped,
    }
    inventory_type = _enum(mapped.get("inventory_type"), INVENTORY_TYPE_VALUES)
    material_hint = _fact_text(mapped.get("material_code"))
    inventory_hint = _fact_text(mapped.get("inventory_hint"))
    inventory_code = _fact_text(mapped.get("inventory_code")) or inventory_hint
    product_name = _fact_text(mapped.get("product_name"))
    if inventory_type is None and not material_hint:
        inventory_type = "finished"
    ownership_type = _enum(mapped.get("ownership_type"), OWNERSHIP_VALUES)
    unit = _enum(mapped.get("unit"), UNIT_VALUES)
    accuracy = _enum(
        mapped.get("stock_date_accuracy"),
        DATE_ACCURACY_VALUES,
    )
    action = _text(mapped.get("action_decision")) or "pending"
    if action not in EDITABLE_ACTIONS:
        action = "pending"
    line = InventoryOnboardingLine(
        batch=batch,
        source_sheet_name=row.sheet_name,
        source_row_number=row.row_number,
        source_row_hash=_source_hash(upload_sha256, row),
        raw_row_text=row.raw_text,
        original_values_json=original,
        stocktake_date=_date(mapped.get("stocktake_date")),
        stocktaker_name=_fact_text(mapped.get("stocktaker_name")),
        inventory_type=inventory_type,
        ownership_type=ownership_type,
        location_id=_integer(mapped.get("location_id")),
        location_code_snapshot=_fact_text(mapped.get("location_code")),
        floor_snapshot=_integer(mapped.get("floor")),
        area_code_snapshot=_fact_text(mapped.get("area_code")),
        pallet_code=_fact_text(mapped.get("pallet_code")),
        customer_id=_integer(mapped.get("customer_id")),
        customer_code_snapshot=_fact_text(mapped.get("customer_code")),
        customer_name_snapshot=_fact_text(mapped.get("customer_name")),
        product_id=_integer(mapped.get("product_id")),
        inventory_code_snapshot=inventory_code,
        product_name_snapshot=product_name,
        material_id=_integer(mapped.get("material_id")),
        material_code_snapshot=material_hint,
        quantity=_integer(mapped.get("quantity")),
        unit=unit,
        stock_date=_date(mapped.get("stock_date")),
        stock_date_accuracy=accuracy,
        stock_date_original_text=_text(
            mapped.get("stock_date_original_text")
        ),
        supplier_name=_fact_text(mapped.get("supplier_name")),
        layer_count=_integer(mapped.get("layer_count")),
        flute_type=(
            _fact_text(mapped.get("flute_type")).upper()
            if _fact_text(mapped.get("flute_type"))
            else None
        ),
        board_length_mm=_integer(mapped.get("board_length_mm")),
        board_width_mm=_integer(mapped.get("board_width_mm")),
        sheet_type=_enum(mapped.get("sheet_type"), SHEET_TYPE_VALUES),
        component_type=_enum(mapped.get("component_type"), COMPONENT_VALUES),
        pieces_per_box=_integer(mapped.get("pieces_per_box")),
        stock_yield_per_sheet=_integer(
            mapped.get("stock_yield_per_sheet")
        ),
        crease_type=_fact_text(mapped.get("crease_type")),
        crease_left_mm=_integer(mapped.get("crease_left_mm")),
        crease_middle_mm=_integer(mapped.get("crease_middle_mm")),
        crease_right_mm=_integer(mapped.get("crease_right_mm")),
        cutting_note=_text(mapped.get("cutting_note")),
        remarks=_text(mapped.get("remarks")),
        action_decision=action,
        match_status="pending",
        error_codes_json=[],
        warning_codes_json=[],
        match_evidence_json={},
        version=1,
        updated_by=updated_by,
    )
    if line.inventory_type == "finished" and line.unit is None:
        line.unit = "boxes"
    if line.inventory_type == "semi_finished" and line.unit is None:
        line.unit = "sheets"
    if line.ownership_type is None:
        line.ownership_type = "customer_specific"
    if line.stock_date_accuracy is None:
        line.stock_date_accuracy = (
            "exact" if line.stock_date is not None else "unknown"
        )
    if line.inventory_type == "semi_finished":
        line.component_type = line.component_type or "whole"
        line.pieces_per_box = line.pieces_per_box or 1
        line.stock_yield_per_sheet = line.stock_yield_per_sheet or 1
    return line


def _source_integer(line: InventoryOnboardingLine, field: str) -> int | None:
    return _integer(_source_mapped(line).get(field))


def _source_fact(line: InventoryOnboardingLine, field: str) -> str | None:
    return _fact_text(_source_mapped(line).get(field))


def _append_once(target: list[str], code: str) -> None:
    if code not in target:
        target.append(code)


def _resolve_customer(
    db: Session,
    line: InventoryOnboardingLine,
    errors: list[str],
    evidence: dict[str, object],
) -> Customer | None:
    candidates: list[Customer] = []
    if line.customer_id is not None:
        customer = db.get(Customer, line.customer_id)
        if customer is None:
            _append_once(errors, "CUSTOMER_ID_NOT_FOUND")
            return None
        code = line.customer_code_snapshot
        name = line.customer_name_snapshot
        if (
            (code and code != customer.customer_code)
            or (name and name != customer.name)
        ):
            _append_once(errors, "CUSTOMER_IDENTITY_CONFLICT")
            return None
        candidates = [customer]
    else:
        code = line.customer_code_snapshot
        name = line.customer_name_snapshot
        clauses = []
        if code:
            clauses.append(Customer.customer_code == code)
        if name:
            clauses.append(Customer.name == name)
        if clauses:
            candidates = db.scalars(
                select(Customer).where(or_(*clauses)).order_by(Customer.id)
            ).all()
            if code and name:
                exact = [
                    row
                    for row in candidates
                    if row.customer_code == code and row.name == name
                ]
                if not exact and candidates:
                    _append_once(errors, "CUSTOMER_IDENTITY_CONFLICT")
                    return None
                candidates = exact
        if not candidates and name:
            candidates = db.scalars(
                select(Customer)
                .where(
                    Customer.name.contains(name),
                    Customer.is_active.is_(True),
                    Customer.status == "active",
                )
                .order_by(Customer.id)
            ).all()
    unique = {row.id: row for row in candidates}
    if not unique:
        if line.ownership_type == "customer_specific":
            _append_once(errors, "CUSTOMER_NOT_FOUND")
        return None
    if len(unique) > 1:
        _append_once(errors, "CUSTOMER_AMBIGUOUS")
        return None
    customer = next(iter(unique.values()))
    if not customer.is_active or customer.status != "active":
        _append_once(errors, "CUSTOMER_INACTIVE")
        return None
    line.customer_id = customer.id
    line.customer_code_snapshot = customer.customer_code
    line.customer_name_snapshot = customer.name
    evidence["customer"] = {
        "id": customer.id,
        "code": customer.customer_code,
        "name": customer.name,
        "version": customer.version,
    }
    return customer


def _resolve_product(
    db: Session,
    line: InventoryOnboardingLine,
    customer: Customer | None,
    errors: list[str],
    evidence: dict[str, object],
) -> Product | None:
    if line.inventory_type != "finished":
        return None
    candidates: list[Product] = []
    if line.product_id is not None:
        product = db.get(Product, line.product_id)
        if product is None:
            _append_once(errors, "PRODUCT_ID_NOT_FOUND")
            return None
        code = line.inventory_code_snapshot
        name = line.product_name_snapshot
        if (
            (
                code
                and code
                not in {
                    product.product_code,
                    product.customer_material_code,
                }
            )
            or (name and name != product.product_name)
        ):
            _append_once(errors, "PRODUCT_IDENTITY_CONFLICT")
            return None
        candidates = [product]
    else:
        code = line.inventory_code_snapshot
        name = line.product_name_snapshot
        statement = select(Product)
        if customer is not None:
            statement = statement.where(Product.customer_id == customer.id)
        clauses = []
        if code:
            clauses.append(
                or_(
                    Product.product_code == code,
                    Product.customer_material_code == code,
                )
            )
        if name:
            clauses.append(Product.product_name == name)
        if clauses:
            candidates = db.scalars(
                statement.where(or_(*clauses)).order_by(Product.id)
            ).all()
            if code and name:
                exact = [
                    row
                    for row in candidates
                    if (
                        code in {row.product_code, row.customer_material_code}
                        and row.product_name == name
                    )
                ]
                if not exact and candidates:
                    _append_once(errors, "PRODUCT_IDENTITY_CONFLICT")
                    return None
                candidates = exact
        if not candidates:
            fuzzy_clauses = []
            if code:
                fuzzy_clauses.append(Product.product_name == code)
            if name:
                fuzzy_clauses.append(Product.product_name.contains(name))
            if fuzzy_clauses:
                candidates = db.scalars(
                    statement.where(or_(*fuzzy_clauses)).order_by(Product.id)
                ).all()
    unique = {row.id: row for row in candidates}
    if not unique:
        _append_once(errors, "PRODUCT_NOT_FOUND")
        return None
    if len(unique) > 1:
        _append_once(errors, "PRODUCT_AMBIGUOUS")
        return None
    product = next(iter(unique.values()))
    if not product.is_active or product.deleted_at is not None:
        _append_once(errors, "PRODUCT_INACTIVE")
        return None
    if customer is not None and product.customer_id != customer.id:
        _append_once(errors, "PRODUCT_CUSTOMER_MISMATCH")
        return None
    if customer is None:
        inferred_customer = db.get(Customer, product.customer_id)
        if inferred_customer is None or not inferred_customer.is_active:
            _append_once(errors, "PRODUCT_CUSTOMER_INVALID")
            return None
        line.customer_id = inferred_customer.id
        line.customer_code_snapshot = inferred_customer.customer_code
        line.customer_name_snapshot = inferred_customer.name
        evidence["customer"] = {
            "id": inferred_customer.id,
            "code": inferred_customer.customer_code,
            "name": inferred_customer.name,
            "version": inferred_customer.version,
            "inferred_from_product": True,
        }
    line.product_id = product.id
    line.inventory_code_snapshot = product.product_code
    line.product_name_snapshot = product.product_name
    evidence["product"] = {
        "id": product.id,
        "customer_id": product.customer_id,
        "product_code": product.product_code,
        "version": product.version,
    }
    return product


def _customer_hint_allows_product_inference(
    line: InventoryOnboardingLine,
    db: Session,
) -> bool:
    if line.customer_id is None:
        return False
    customer = db.get(Customer, line.customer_id)
    if customer is None:
        return False
    mapped = _source_mapped(line)
    code_hint = _fact_text(mapped.get("customer_code"))
    name_hint = _fact_text(mapped.get("customer_name"))
    if code_hint and code_hint.casefold() != (
        customer.customer_code or ""
    ).casefold():
        return False
    if name_hint and not (
        name_hint.casefold() in customer.name.casefold()
        or customer.name.casefold() in name_hint.casefold()
    ):
        return False
    return True


def _resolve_material(
    db: Session,
    line: InventoryOnboardingLine,
    warnings: list[str],
    errors: list[str],
    evidence: dict[str, object],
) -> Material | None:
    if line.inventory_type != "semi_finished":
        return None
    material: Material | None = None
    if line.material_id is not None:
        material = db.get(Material, line.material_id)
        if material is None:
            _append_once(errors, "MATERIAL_ID_NOT_FOUND")
            return None
        if (
            line.material_code_snapshot
            and line.material_code_snapshot != material.code
        ):
            _append_once(errors, "MATERIAL_IDENTITY_CONFLICT")
            return None
    elif line.material_code_snapshot:
        rows = db.scalars(
            select(Material)
            .where(Material.code == line.material_code_snapshot)
            .order_by(Material.id)
        ).all()
        if len(rows) == 1:
            material = rows[0]
        elif len(rows) > 1:
            _append_once(errors, "MATERIAL_AMBIGUOUS")
            return None
    if material is None:
        if not line.material_code_snapshot:
            _append_once(errors, "MATERIAL_CODE_REQUIRED")
        else:
            _append_once(warnings, "MATERIAL_MASTER_NOT_FOUND")
        return None
    if not material.is_active:
        _append_once(errors, "MATERIAL_INACTIVE")
        return None
    line.material_id = material.id
    line.material_code_snapshot = material.code
    evidence["material"] = {
        "id": material.id,
        "code": material.code,
        "version": material.version,
    }
    return material


def _resolve_location(
    db: Session,
    batch: InventoryOnboardingBatch,
    line: InventoryOnboardingLine,
    errors: list[str],
    evidence: dict[str, object],
) -> WarehouseLocation | None:
    location: WarehouseLocation | None = None
    if line.location_id is not None:
        location = db.get(WarehouseLocation, line.location_id)
        if location is None:
            _append_once(errors, "LOCATION_ID_NOT_FOUND")
            return None
        if (
            line.location_code_snapshot
            and line.location_code_snapshot != location.location_code
        ):
            _append_once(errors, "LOCATION_ID_CODE_CONFLICT")
            return None
    elif line.location_code_snapshot:
        rows = db.scalars(
            select(WarehouseLocation)
            .where(
                WarehouseLocation.location_code
                == line.location_code_snapshot
            )
            .order_by(WarehouseLocation.id)
        ).all()
        if len(rows) == 1:
            location = rows[0]
        elif len(rows) > 1:
            _append_once(errors, "LOCATION_AMBIGUOUS")
            return None
    if location is None:
        if line.inventory_type == "finished":
            source_hint = (
                _source_fact(line, "position_note")
                or _source_fact(line, "location_code")
            )
            sequence = _source_fact(line, "field_sequence") or str(
                max(line.source_row_number - 1, 1)
            )
            planned_code = (
                f"PD-{batch.source_file_sha256[:8].upper()}-"
                f"{line.source_row_number:04d}"
            )
            line.location_id = None
            line.location_code_snapshot = planned_code
            line.floor_snapshot = 3
            line.area_code_snapshot = "待定位"
            line.warehouse_name_snapshot = (
                f"位置待确认 · 现场 {sequence}"
                + (f" · {source_hint}" if source_hint else "")
            )
            evidence["planned_location"] = {
                "code": planned_code,
                "floor": 3,
                "area_code": "待定位",
                "field_sequence": sequence,
                "position_note": source_hint,
            }
            return None
        _append_once(errors, "LOCATION_NOT_FOUND")
        return None
    if not location.is_active:
        _append_once(errors, "LOCATION_INACTIVE")
    if location.placement_status != "placed":
        _append_once(errors, "LOCATION_UNPLACED")
    if (
        line.floor_snapshot is not None
        and line.floor_snapshot != location.warehouse_floor
    ):
        _append_once(errors, "LOCATION_FLOOR_CONFLICT")
    if (
        line.area_code_snapshot
        and line.area_code_snapshot != location.area_code
    ):
        _append_once(errors, "LOCATION_AREA_CONFLICT")
    allowed_types = {
        "finished": {"finished", "shared"},
        "semi_finished": {"semi_finished", "shared"},
    }.get(line.inventory_type, set())
    if allowed_types and location.warehouse_type not in allowed_types:
        _append_once(errors, "LOCATION_INVENTORY_TYPE_MISMATCH")
    line.location_id = location.id
    line.location_code_snapshot = location.location_code
    line.floor_snapshot = location.warehouse_floor
    line.warehouse_name_snapshot = location.location_name
    line.area_code_snapshot = location.area_code
    evidence["location"] = {
        "id": location.id,
        "code": location.location_code,
        "floor": location.warehouse_floor,
        "area_code": location.area_code,
        "warehouse_type": location.warehouse_type,
        "placement_status": location.placement_status,
        "is_active": location.is_active,
    }
    return location


def _ownership_customer_id(
    line: InventoryOnboardingLine,
) -> int | None:
    return (
        line.customer_id
        if line.ownership_type == "customer_specific"
        else None
    )


def _pallet_item_evidence(
    item: InventoryPalletItem,
) -> dict[str, object]:
    return {
        "id": item.id,
        "inventory_lot_id": item.inventory_lot_id,
        "customer_id": item.customer_id,
        "product_id": item.product_id,
        "inventory_code": item.inventory_code,
        "item_type": item.item_type,
        "quantity": str(item.quantity),
        "unit": item.unit,
        "match_status": item.match_status,
    }


def _semi_detail_evidence(lot: InventoryLot) -> dict[str, object] | None:
    detail = lot.semi_finished_detail
    if detail is None:
        return None
    return {
        "owner_customer_id": detail.owner_customer_id,
        "material_id": detail.material_id,
        "material_code_snapshot": detail.material_code_snapshot,
        "normalized_material_code": detail.normalized_material_code,
        "supplier_name": detail.supplier_name,
        "layer_count": detail.layer_count,
        "flute_type": detail.flute_type,
        "board_length_mm": detail.board_length_mm,
        "board_width_mm": detail.board_width_mm,
        "sheet_type": detail.sheet_type,
        "component_type": detail.component_type,
        "pieces_per_box": detail.pieces_per_box,
        "stock_yield_per_sheet": detail.stock_yield_per_sheet,
        "crease_type": detail.crease_type,
        "crease_left_mm": detail.crease_left_mm,
        "crease_middle_mm": detail.crease_middle_mm,
        "crease_right_mm": detail.crease_right_mm,
        "cutting_note": detail.cutting_note,
    }


def _finished_detail_evidence(
    lot: InventoryLot,
) -> dict[str, object] | None:
    detail = lot.finished_detail
    if detail is None:
        return None
    return {
        "owner_customer_id": detail.owner_customer_id,
        "is_general": detail.is_general,
        "product_id": detail.product_id,
        "inventory_code_snapshot": detail.inventory_code_snapshot,
        "product_name_snapshot": detail.product_name_snapshot,
    }


def _lot_evidence(lot: InventoryLot) -> dict[str, object]:
    detail = (
        _finished_detail_evidence(lot)
        if lot.inventory_type == "finished"
        else _semi_detail_evidence(lot)
    )
    return {
        "id": lot.id,
        "lot_number": lot.lot_number,
        "inventory_type": lot.inventory_type,
        "warehouse_location_id": lot.warehouse_location_id,
        "unit": lot.unit,
        "status": lot.status,
        "version": lot.version,
        "quantity_available": lot.quantity_available,
        "quantity_reserved": lot.quantity_reserved,
        "quantity_consumed": lot.quantity_consumed,
        "quantity_damaged": lot.quantity_damaged,
        "quantity_scrapped": lot.quantity_scrapped,
        "detail": detail,
    }


def _resolve_exported_existing_lot(
    db: Session,
    line: InventoryOnboardingLine,
    errors: list[str],
    warnings: list[str],
    evidence: dict[str, object],
) -> InventoryLot | None:
    lot_id = _source_integer(line, "existing_lot_id")
    if lot_id is None:
        return None
    line.existing_lot_id = lot_id
    lot = db.get(InventoryLot, lot_id)
    if (
        lot is None
        or lot.inventory_type != "finished"
        or lot.finished_detail is None
        or lot.status not in {"active", "frozen"}
    ):
        _append_once(errors, "EXPORTED_INVENTORY_NOT_FOUND")
        return None
    expected_version = _source_integer(line, "existing_lot_version")
    expected_available = _source_integer(line, "existing_available")
    expected_reserved = _source_integer(line, "existing_reserved")
    expected_on_hand = _source_integer(line, "system_quantity")
    if expected_version is None:
        _append_once(errors, "EXPORTED_INVENTORY_VERSION_REQUIRED")
    elif expected_version != lot.version:
        _append_once(errors, "EXPORTED_INVENTORY_CHANGED")
    if (
        expected_available is not None
        and expected_available != int(lot.quantity_available)
    ):
        _append_once(errors, "EXPORTED_INVENTORY_CHANGED")
    if (
        expected_reserved is not None
        and expected_reserved != int(lot.quantity_reserved)
    ):
        _append_once(errors, "EXPORTED_INVENTORY_CHANGED")
    current_on_hand = int(lot.quantity_available) + int(lot.quantity_reserved)
    if expected_on_hand is not None and expected_on_hand != current_on_hand:
        _append_once(errors, "EXPORTED_INVENTORY_CHANGED")

    detail = lot.finished_detail
    location = db.get(WarehouseLocation, lot.warehouse_location_id)
    if location is None or not location.is_active:
        _append_once(errors, "LOCATION_NOT_FOUND")
        return lot
    product = db.get(Product, detail.product_id)
    customer = (
        db.get(Customer, detail.owner_customer_id)
        if detail.owner_customer_id is not None
        else None
    )
    if product is None or not product.is_active or product.deleted_at is not None:
        _append_once(errors, "PRODUCT_INACTIVE")
        return lot
    if not detail.is_general and (
        customer is None
        or not customer.is_active
        or customer.status != "active"
    ):
        _append_once(errors, "CUSTOMER_INACTIVE")
        return lot

    line.inventory_type = "finished"
    line.ownership_type = "general" if detail.is_general else "customer_specific"
    line.location_id = location.id
    line.location_code_snapshot = location.location_code
    line.floor_snapshot = location.warehouse_floor
    line.warehouse_name_snapshot = location.location_name
    line.area_code_snapshot = location.area_code
    line.customer_id = detail.owner_customer_id
    line.customer_code_snapshot = customer.customer_code if customer else None
    line.customer_name_snapshot = detail.owner_customer_name_snapshot
    line.product_id = detail.product_id
    line.inventory_code_snapshot = detail.inventory_code_snapshot
    line.product_name_snapshot = detail.product_name_snapshot
    line.unit = "boxes"
    line.stock_date = lot.stock_date
    line.stock_date_accuracy = lot.stock_date_accuracy
    line.stock_date_original_text = lot.stock_date_original_text
    line.existing_lot_id = lot.id
    line.existing_pallet_id = (
        lot.pallet_item.pallet_id if lot.pallet_item is not None else None
    )
    line.pallet_code = (
        lot.pallet_item.pallet.pallet_code
        if lot.pallet_item is not None and lot.pallet_item.pallet is not None
        else None
    )
    pallet = (
        lot.pallet_item.pallet
        if lot.pallet_item is not None
        else None
    )
    if pallet is not None:
        evidence["existing_pallet"] = {
            "id": pallet.id,
            "version": pallet.version,
            "location_id": pallet.location_id,
            "pallet_code": pallet.pallet_code,
        }
    requested_position = _source_fact(line, "position_note")
    if requested_position:
        target = db.scalar(
            select(WarehouseLocation).where(
                WarehouseLocation.location_code
                == requested_position.strip().upper()
            )
        )
        if target is not None and target.id == location.id:
            evidence["requested_location"] = {
                "raw": requested_position,
                "target_location_id": target.id,
                "target_location_code": target.location_code,
                "move_required": False,
            }
        elif (
            target is not None
            and target.is_active
            and target.placement_status == "placed"
            and target.source_version == "V11"
            and target.warehouse_floor == 3
            and target.warehouse_type in {"finished", "shared"}
        ):
            if pallet is None or not pallet.is_current:
                _append_once(errors, "EXPORTED_INVENTORY_PALLET_REQUIRED")
            else:
                occupied = db.scalar(
                    select(InventoryPallet).where(
                        InventoryPallet.location_id == target.id,
                        InventoryPallet.is_current.is_(True),
                    )
                )
                if occupied is not None and occupied.id != pallet.id:
                    _append_once(
                        errors,
                        "LOCATION_OCCUPIED_BY_OTHER_PALLET",
                    )
                evidence["requested_location"] = {
                    "raw": requested_position,
                    "target_location_id": target.id,
                    "target_location_code": target.location_code,
                    "move_required": target.id != location.id,
                    "expected_current_pallet_id": (
                        occupied.id if occupied is not None else None
                    ),
                }
        else:
            _append_once(warnings, "EXISTING_LOCATION_NEEDS_REVIEW")
            evidence["requested_location"] = {
                "raw": requested_position,
                "target_location_id": None,
                "move_required": False,
                "mark_needs_relocation": pallet is not None,
            }
    if customer is not None:
        evidence["customer"] = {
            "id": customer.id,
            "code": customer.customer_code,
            "name": customer.name,
            "version": customer.version,
        }
    evidence["product"] = {
        "id": product.id,
        "customer_id": product.customer_id,
        "product_code": product.product_code,
        "version": product.version,
    }
    evidence["location"] = {
        "id": location.id,
        "code": location.location_code,
        "floor": location.warehouse_floor,
        "area_code": location.area_code,
        "warehouse_type": location.warehouse_type,
        "placement_status": location.placement_status,
        "is_active": location.is_active,
    }
    evidence["existing_lot"] = _lot_evidence(lot)
    evidence["exported_inventory"] = {
        "lot_id": lot.id,
        "expected_version": expected_version,
        "expected_available": expected_available,
        "expected_reserved": expected_reserved,
        "expected_on_hand": expected_on_hand,
    }
    line.action_decision = "route_n035"
    return lot


def _finished_snapshot_identity_matches(
    line: InventoryOnboardingLine,
    item: InventoryPalletItem,
) -> bool:
    return (
        item.inventory_lot_id is None
        and item.item_type == "finished"
        and item.match_status == "matched"
        and line.product_id is not None
        and item.product_id == line.product_id
        and bool(line.inventory_code_snapshot)
        and item.inventory_code == line.inventory_code_snapshot
        and item.customer_id == _ownership_customer_id(line)
    )


def _finished_snapshot_item_matches(
    line: InventoryOnboardingLine,
    item: InventoryPalletItem,
) -> bool:
    return (
        _finished_snapshot_identity_matches(line, item)
        and item.unit == "boxes"
        and item.quantity > 0
        and item.quantity == line.quantity
    )


def _formal_pallet_item_matches(
    line: InventoryOnboardingLine,
    item: InventoryPalletItem,
    lot: InventoryLot,
) -> bool:
    expected_unit = (
        "boxes" if line.inventory_type == "finished" else "sheets"
    )
    if (
        item.inventory_lot_id != lot.id
        or item.item_type != line.inventory_type
        or item.match_status != "matched"
        or lot.inventory_type != line.inventory_type
        or lot.status not in {"active", "frozen"}
        or lot.warehouse_location_id != line.location_id
        or lot.unit != expected_unit
    ):
        return False
    owner_customer_id = _ownership_customer_id(line)
    if line.inventory_type == "finished":
        detail = lot.finished_detail
        return bool(
            detail is not None
            and line.product_id is not None
            and detail.product_id == line.product_id
            and detail.inventory_code_snapshot
            == line.inventory_code_snapshot
            and bool(detail.is_general)
            == (line.ownership_type == "general")
            and detail.owner_customer_id == owner_customer_id
            and item.product_id == detail.product_id
            and item.inventory_code == detail.inventory_code_snapshot
            and (
                item.customer_id == detail.owner_customer_id
                if line.ownership_type == "customer_specific"
                else item.customer_id in {None, line.customer_id}
            )
        )
    detail = lot.semi_finished_detail
    if detail is None:
        return False
    return all(
        (
            detail.owner_customer_id == owner_customer_id,
            detail.material_id == line.material_id,
            normalize_material_code(detail.material_code_snapshot)
            == normalize_material_code(line.material_code_snapshot),
            detail.supplier_name == line.supplier_name,
            detail.layer_count == line.layer_count,
            detail.flute_type == line.flute_type,
            detail.board_length_mm == line.board_length_mm,
            detail.board_width_mm == line.board_width_mm,
            detail.sheet_type == line.sheet_type,
            detail.component_type == line.component_type,
            detail.pieces_per_box == line.pieces_per_box,
            detail.stock_yield_per_sheet == line.stock_yield_per_sheet,
            detail.crease_type == line.crease_type,
            detail.crease_left_mm == line.crease_left_mm,
            detail.crease_middle_mm == line.crease_middle_mm,
            detail.crease_right_mm == line.crease_right_mm,
            detail.cutting_note == line.cutting_note,
            item.customer_id == detail.owner_customer_id,
        )
    )


def _resolve_pallet(
    db: Session,
    batch: InventoryOnboardingBatch,
    line: InventoryOnboardingLine,
    location: WarehouseLocation | None,
    errors: list[str],
    evidence: dict[str, object],
) -> None:
    line.existing_lot_id = None
    line.existing_pallet_id = None
    if line.action_decision.startswith("route_"):
        line.action_decision = "pending"
    if not line.pallet_code:
        line.pallet_code = (
            f"STK-{batch.source_file_sha256[:8].upper()}-"
            f"{line.source_row_number:04d}"
        )
    pallet = db.scalar(
        select(InventoryPallet).where(
            InventoryPallet.pallet_code == line.pallet_code
        )
    )
    evidence["pallet_lookup"] = {
        "code": line.pallet_code,
        "expected_id": pallet.id if pallet is not None else None,
    }
    if pallet is None:
        occupied = None
        if location is not None:
            occupied = db.scalar(
                select(InventoryPallet).where(
                    InventoryPallet.location_id == location.id,
                    InventoryPallet.is_current.is_(True),
                )
            )
            if occupied is not None:
                _append_once(errors, "LOCATION_OCCUPIED_BY_OTHER_PALLET")
            evidence["location_occupancy"] = {
                "location_id": location.id,
                "expected_current_pallet_id": (
                    occupied.id if occupied is not None else None
                ),
            }
        if not errors:
            line.action_decision = "create_new"
        return
    line.existing_pallet_id = pallet.id
    evidence["pallet"] = {
        "id": pallet.id,
        "code": pallet.pallet_code,
        "location_id": pallet.location_id,
        "status": pallet.status,
        "is_current": pallet.is_current,
        "version": pallet.version,
    }
    evidence["location_occupancy"] = {
        "location_id": location.id if location is not None else None,
        "expected_current_pallet_id": pallet.id,
    }
    evidence["pallet_items"] = [
        _pallet_item_evidence(item)
        for item in sorted(pallet.items, key=lambda row: row.id)
    ]
    if pallet.status != "active" or not pallet.is_current:
        _append_once(errors, "PALLET_INACTIVE")
        return
    if location is not None and pallet.location_id != location.id:
        _append_once(errors, "PALLET_LOCATION_MISMATCH")
        return

    formal_matches: list[tuple[InventoryPalletItem, InventoryLot]] = []
    snapshot_matches: list[InventoryPalletItem] = []
    snapshot_quantity_unit_mismatch = False
    semi_snapshot_present = False
    for item in pallet.items:
        if item.inventory_lot_id is not None:
            lot = db.get(InventoryLot, item.inventory_lot_id)
            if lot is not None and _formal_pallet_item_matches(
                line, item, lot
            ):
                formal_matches.append((item, lot))
        elif line.inventory_type == "finished":
            if _finished_snapshot_item_matches(line, item):
                snapshot_matches.append(item)
            elif _finished_snapshot_identity_matches(line, item):
                snapshot_quantity_unit_mismatch = True
        elif item.item_type == "semi_finished":
            semi_snapshot_present = True

    if len(formal_matches) + len(snapshot_matches) > 1:
        _append_once(errors, "PALLET_MULTIPLE_MATCHING_FACTS")
        return
    if formal_matches:
        _item, lot = formal_matches[0]
        line.existing_lot_id = lot.id
        evidence["existing_lot"] = _lot_evidence(lot)
        line.action_decision = (
            "route_n035"
            if line.inventory_type == "finished"
            else "route_semi_adjust"
        )
        return
    if snapshot_matches:
        line.existing_lot_id = None
        line.action_decision = "route_snapshot_conversion"
        return
    if snapshot_quantity_unit_mismatch:
        _append_once(errors, "SNAPSHOT_QUANTITY_UNIT_MISMATCH")
    elif semi_snapshot_present:
        _append_once(errors, "SEMI_PALLET_SNAPSHOT_UNSUPPORTED")
    elif any(item.match_status != "matched" for item in pallet.items):
        _append_once(errors, "PALLET_ITEM_UNMATCHED")
    else:
        _append_once(errors, "PALLET_CONTENT_MISMATCH")


def match_onboarding_line(
    db: Session,
    *,
    batch: InventoryOnboardingBatch,
    line: InventoryOnboardingLine,
) -> InventoryOnboardingLine:
    # Matching performs several read queries. Keep the whole interpretation
    # inside one no-autoflush window so the database sees exactly one
    # versioned UPDATE for this draft line.
    with db.no_autoflush:
        return _match_onboarding_line(db, batch=batch, line=line)


def _match_onboarding_line(
    db: Session,
    *,
    batch: InventoryOnboardingBatch,
    line: InventoryOnboardingLine,
) -> InventoryOnboardingLine:
    errors: list[str] = []
    warnings: list[str] = []
    evidence: dict[str, object] = {
        "source_file_sha256": batch.source_file_sha256,
        "source_row_hash": line.source_row_hash,
    }
    if line.action_decision == "exclude":
        line.match_status = "excluded"
        line.error_codes_json = []
        line.warning_codes_json = []
        line.match_evidence_json = evidence
        line.existing_lot_id = None
        line.existing_pallet_id = None
        return line

    if line.stocktake_date is None:
        line.stocktake_date = beijing_today()
    if line.stocktake_date is None:
        _append_once(errors, "STOCKTAKE_DATE_REQUIRED")
    if not line.stocktaker_name:
        line.stocktaker_name = "系统代填"
    if line.inventory_type not in {"finished", "semi_finished"}:
        _append_once(errors, "INVENTORY_TYPE_REQUIRED")
    if line.ownership_type not in {"customer_specific", "general"}:
        _append_once(errors, "OWNERSHIP_TYPE_REQUIRED")
    if line.quantity is None or line.quantity < 0:
        _append_once(errors, "QUANTITY_INVALID")
    expected_unit = {
        "finished": "boxes",
        "semi_finished": "sheets",
    }.get(line.inventory_type)
    if expected_unit and line.unit != expected_unit:
        _append_once(errors, "UNIT_INVENTORY_TYPE_MISMATCH")

    if line.stock_date_accuracy == "unknown" and line.stock_date is None:
        line.stock_date = line.stocktake_date
        _append_once(warnings, "UNKNOWN_DATE_USES_STOCKTAKE_TECHNICAL_DATE")
    if line.stock_date is None:
        _append_once(errors, "STOCK_DATE_REQUIRED")
    elif line.stock_date_accuracy in {"exact", "estimated", "unknown"}:
        try:
            (
                line.stock_date_accuracy,
                line.stock_date_original_text,
            ) = normalize_stock_date_metadata(
                stock_date=line.stock_date,
                stock_date_accuracy=line.stock_date_accuracy,
                stock_date_original_text=line.stock_date_original_text,
            )
        except WarehouseInventoryError:
            _append_once(errors, "STOCK_DATE_METADATA_INVALID")
    else:
        _append_once(errors, "STOCK_DATE_ACCURACY_REQUIRED")

    exported_lot = _resolve_exported_existing_lot(
        db,
        line,
        errors,
        warnings,
        evidence,
    )
    location: WarehouseLocation | None = None
    if exported_lot is None:
        if line.quantity == 0:
            _append_once(errors, "QUANTITY_INVALID")
        location = _resolve_location(db, batch, line, errors, evidence)
        customer = _resolve_customer(db, line, errors, evidence)
        product = _resolve_product(db, line, customer, errors, evidence)
        if (
            product is not None
            and _customer_hint_allows_product_inference(line, db)
        ):
            errors = [
                code
                for code in errors
                if code not in CUSTOMER_RESOLUTION_ERRORS
            ]
        _resolve_material(db, line, warnings, errors, evidence)
    else:
        location = db.get(WarehouseLocation, exported_lot.warehouse_location_id)
        customer = (
            db.get(Customer, line.customer_id)
            if line.customer_id is not None
            else None
        )

    if line.inventory_type == "semi_finished":
        if line.layer_count not in SEMI_FINISHED_FLUTES_BY_LAYER:
            _append_once(errors, "SEMI_LAYER_INVALID")
        elif line.flute_type not in SEMI_FINISHED_FLUTES_BY_LAYER[line.layer_count]:
            _append_once(errors, "SEMI_FLUTE_INVALID")
        if not line.board_length_mm or line.board_length_mm <= 0:
            _append_once(errors, "SEMI_LENGTH_INVALID")
        if not line.board_width_mm or line.board_width_mm <= 0:
            _append_once(errors, "SEMI_WIDTH_INVALID")
        if line.sheet_type not in {
            "raw_board",
            "net_sheet",
            "creased_sheet",
        }:
            _append_once(errors, "SEMI_SHEET_TYPE_INVALID")
        if line.component_type not in {"whole", "cover", "base"}:
            _append_once(errors, "SEMI_COMPONENT_INVALID")
        if not line.pieces_per_box or line.pieces_per_box <= 0:
            _append_once(errors, "SEMI_PIECES_PER_BOX_INVALID")
        if not line.stock_yield_per_sheet or line.stock_yield_per_sheet <= 0:
            _append_once(errors, "SEMI_STOCK_YIELD_INVALID")
        if line.ownership_type == "customer_specific" and customer is None:
            _append_once(errors, "SEMI_CUSTOMER_REQUIRED")

    if exported_lot is None:
        _resolve_pallet(
            db,
            batch,
            line,
            location,
            errors,
            evidence,
        )
    line.error_codes_json = errors
    line.warning_codes_json = warnings
    line.match_evidence_json = evidence
    if errors:
        line.match_status = "blocked"
        if line.action_decision in {"pending", "create_new"}:
            line.action_decision = "pending"
    elif line.action_decision.startswith("route_"):
        line.match_status = "routed"
    else:
        line.action_decision = "create_new"
        line.match_status = "ready"
    return line


def _invalidate_dry_run(batch: InventoryOnboardingBatch) -> None:
    batch.dry_run_fingerprint = None
    batch.dry_run_summary_json = None
    batch.dry_run_by = None
    batch.dry_run_at = None


def _rematch_batch_lines(
    db: Session,
    batch: InventoryOnboardingBatch,
    *,
    bump_versions: bool,
    lines: list[InventoryOnboardingLine] | None = None,
) -> list[InventoryOnboardingLine]:
    if lines is None:
        with db.no_autoflush:
            lines = _batch_lines(db, batch.id)
    for line in lines:
        if bump_versions:
            line.version += 1
        match_onboarding_line(db, batch=batch, line=line)

    active = [
        line
        for line in lines
        if line.action_decision != "exclude"
    ]
    areas = {
        (line.floor_snapshot, line.area_code_snapshot)
        for line in active
        if line.area_code_snapshot
    }
    duplicate_groups: dict[tuple[object, ...], list[InventoryOnboardingLine]] = {}
    for line in active:
        key = (
            line.inventory_type,
            line.location_id,
            line.pallet_code,
            line.product_id,
            line.material_code_snapshot,
            line.quantity,
            line.unit,
        )
        duplicate_groups.setdefault(key, []).append(line)
    for duplicates in duplicate_groups.values():
        source_hashes = {line.source_row_hash for line in duplicates}
        if len(duplicates) > 1 and len(source_hashes) > 1:
            for line in duplicates:
                _append_once(
                    line.error_codes_json,
                    "DUPLICATE_PHYSICAL_LINE",
                )
                line.match_status = "blocked"

    pallet_locations: dict[str, dict[int, list[InventoryOnboardingLine]]] = {}
    for line in active:
        if line.pallet_code and line.location_id is not None:
            pallet_locations.setdefault(line.pallet_code, {}).setdefault(
                line.location_id, []
            ).append(line)
    for location_groups in pallet_locations.values():
        if len(location_groups) <= 1:
            continue
        for grouped_lines in location_groups.values():
            for line in grouped_lines:
                _append_once(
                    line.error_codes_json,
                    "BATCH_PALLET_MULTIPLE_LOCATIONS",
                )
                line.match_status = "blocked"
                if line.action_decision == "create_new":
                    line.action_decision = "pending"

    new_pallets_by_location: dict[
        int, dict[str, list[InventoryOnboardingLine]]
    ] = {}
    for line in active:
        if (
            line.action_decision == "create_new"
            and line.location_id is not None
            and line.pallet_code
        ):
            new_pallets_by_location.setdefault(
                line.location_id, {}
            ).setdefault(line.pallet_code, []).append(line)
    for pallet_groups in new_pallets_by_location.values():
        if len(pallet_groups) <= 1:
            continue
        for grouped_lines in pallet_groups.values():
            for line in grouped_lines:
                _append_once(
                    line.error_codes_json,
                    "BATCH_LOCATION_MULTIPLE_NEW_PALLETS",
                )
                line.match_status = "blocked"
                line.action_decision = "pending"

    if len(areas) == 1:
        floor, area = next(iter(areas))
        batch.resolved_floor = floor
        batch.resolved_area_code = area
    elif len(areas) > 1:
        batch.resolved_floor = None
        batch.resolved_area_code = "多区域盘点"
    else:
        batch.resolved_floor = None
        batch.resolved_area_code = None
    return lines


def create_onboarding_draft(
    db: Session,
    *,
    upload: StoredInventoryOnboardingUpload,
    creator: User,
) -> tuple[InventoryOnboardingBatch, bool]:
    existing = db.scalar(
        select(InventoryOnboardingBatch).where(
            InventoryOnboardingBatch.source_file_sha256 == upload.sha256
        )
    )
    if existing is not None:
        return existing, False
    batch = InventoryOnboardingBatch(
        batch_number=_batch_number(upload.sha256),
        status="draft",
        version=1,
        source_file_reference=upload.private_reference,
        source_file_sha256=upload.sha256,
        source_original_filename=upload.filename,
        source_content_type=upload.content_type,
        source_size=upload.size,
        source_format=upload.format,
        source_encoding=upload.encoding,
        created_by=creator.id,
        updated_by=creator.id,
    )
    db.add(batch)
    db.flush()
    data_count = 0
    imported_lines: list[InventoryOnboardingLine] = []
    for sheet in upload.sheets:
        if not sheet.rows:
            continue
        header_row = sheet.rows[0]
        headers = tuple(
            _canonical_header(value, index)
            for index, value in enumerate(
                header_row.original_values,
                start=1,
            )
        )
        if len(headers) != len(set(headers)):
            raise InventoryOnboardingError(
                f"工作表 {sheet.name} 存在重复字段名",
                400,
                "INVENTORY_ONBOARDING_DUPLICATE_HEADERS",
            )
        for row in sheet.rows[1:]:
            line = _line_from_source(
                batch=batch,
                upload_sha256=upload.sha256,
                row=row,
                headers=headers,
                updated_by=creator.id,
            )
            if not line.stocktaker_name:
                line.stocktaker_name = creator.real_name or creator.username
            if line.stocktake_date is None:
                line.stocktake_date = beijing_today()
            db.add(line)
            imported_lines.append(line)
            data_count += 1
    if data_count == 0:
        raise InventoryOnboardingError(
            "盘点文件只有表头，没有数据行",
            400,
            "INVENTORY_ONBOARDING_EMPTY",
        )
    # First persist the immutable source rows at version 1, then perform one
    # guarded interpretation update to version 2.
    db.flush()
    lines = _rematch_batch_lines(
        db,
        batch,
        bump_versions=True,
        lines=imported_lines,
    )
    batch.version += 1
    batch.updated_by = creator.id
    db.add(
        _operation_log(
            user=creator,
            batch=batch,
            action="IMPORT",
            description="导入库存建账私有草稿",
            details={
                "batch_number": batch.batch_number,
                "source_file_sha256": batch.source_file_sha256,
                "line_count": len(lines),
            },
        )
    )
    db.flush()
    return batch, True


def get_onboarding_batch(
    db: Session,
    batch_id: int,
) -> InventoryOnboardingBatch:
    batch = db.get(InventoryOnboardingBatch, batch_id)
    if batch is None:
        raise InventoryOnboardingError(
            "库存建账批次不存在",
            404,
            "INVENTORY_ONBOARDING_NOT_FOUND",
        )
    return batch


def list_onboarding_batches(db: Session) -> list[InventoryOnboardingBatch]:
    return db.scalars(
        select(InventoryOnboardingBatch).order_by(
            InventoryOnboardingBatch.created_at.desc(),
            InventoryOnboardingBatch.id.desc(),
        )
    ).all()


def _require_draft(batch: InventoryOnboardingBatch) -> None:
    if batch.status != "draft":
        raise InventoryOnboardingError(
            "库存建账批次已提交冻结，不能再修改",
            409,
            "INVENTORY_ONBOARDING_FROZEN",
        )


def _parse_edit_date(value: object, field: str) -> date | None:
    if value is None or value == "":
        return None
    parsed = _date(value)
    if parsed is None:
        raise InventoryOnboardingError(
            f"{field} 必须使用 YYYY-MM-DD",
            400,
            "INVENTORY_ONBOARDING_DATE_INVALID",
        )
    return parsed


def _parse_edit_integer(
    value: object,
    field: str,
    *,
    allow_none: bool = True,
) -> int | None:
    if value is None or value == "":
        if allow_none:
            return None
        raise InventoryOnboardingError(f"{field} 不能为空")
    parsed = _integer(value)
    if parsed is None:
        raise InventoryOnboardingError(f"{field} 必须为整数")
    return parsed


def update_onboarding_line(
    db: Session,
    *,
    batch_id: int,
    line_id: int,
    expected_version: int,
    values: dict[str, object],
    operator: User,
    batch_expected_version: int | None = None,
) -> InventoryOnboardingLine:
    batch = get_onboarding_batch(db, batch_id)
    _require_draft(batch)
    if batch_expected_version is not None and batch.version != batch_expected_version:
        raise InventoryOnboardingError(
            "批次已被其他操作更新，请刷新后重试",
            409,
            "INVENTORY_ONBOARDING_VERSION_CONFLICT",
        )
    line = db.scalar(
        select(InventoryOnboardingLine).where(
            InventoryOnboardingLine.id == line_id,
            InventoryOnboardingLine.batch_id == batch.id,
        )
    )
    if line is None:
        raise InventoryOnboardingError(
            "库存建账明细不存在",
            404,
            "INVENTORY_ONBOARDING_LINE_NOT_FOUND",
        )
    if line.version != expected_version:
        raise InventoryOnboardingError(
            "明细已被其他操作更新，请刷新后重试",
            409,
            "INVENTORY_ONBOARDING_VERSION_CONFLICT",
        )
    line_version_before = line.version
    batch_version_before = batch.version

    direct_text_fields = {
        "stocktaker_name",
        "pallet_code",
        "supplier_name",
        "flute_type",
        "stock_date_original_text",
        "crease_type",
        "cutting_note",
        "remarks",
    }
    snapshot_fields = {
        "location_code": ("location_code_snapshot", "location_id"),
        "inventory_code": ("inventory_code_snapshot", "product_id"),
        "product_name": ("product_name_snapshot", "product_id"),
        "material_code": ("material_code_snapshot", "material_id"),
    }
    integer_fields = {
        "quantity",
        "layer_count",
        "board_length_mm",
        "board_width_mm",
        "pieces_per_box",
        "stock_yield_per_sheet",
        "crease_left_mm",
        "crease_middle_mm",
        "crease_right_mm",
    }
    for field, value in values.items():
        if field in direct_text_fields:
            setattr(line, field, _text(value))
        elif field in {"customer_code", "customer_name"}:
            target = (
                "customer_code_snapshot"
                if field == "customer_code"
                else "customer_name_snapshot"
            )
            setattr(line, target, _text(value))
            line.customer_id = None
            line.product_id = None
        elif field in snapshot_fields:
            target, reset = snapshot_fields[field]
            setattr(line, target, _text(value))
            setattr(line, reset, None)
        elif field == "location_id":
            line.location_id = _parse_edit_integer(value, field)
            line.location_code_snapshot = None
        elif field == "customer_id":
            line.customer_id = _parse_edit_integer(value, field)
            line.customer_code_snapshot = None
            line.customer_name_snapshot = None
            line.product_id = None
        elif field == "product_id":
            line.product_id = _parse_edit_integer(value, field)
            line.inventory_code_snapshot = None
            line.product_name_snapshot = None
        elif field == "material_id":
            line.material_id = _parse_edit_integer(value, field)
            line.material_code_snapshot = None
        elif field in integer_fields:
            setattr(line, field, _parse_edit_integer(value, field))
        elif field in {"stocktake_date", "stock_date"}:
            setattr(line, field, _parse_edit_date(value, field))
        elif field == "inventory_type":
            normalized = _enum(value, INVENTORY_TYPE_VALUES)
            if normalized is None:
                raise InventoryOnboardingError("库存类型无效")
            line.inventory_type = normalized
            line.unit = "boxes" if normalized == "finished" else "sheets"
            if normalized == "finished":
                line.material_id = None
                line.material_code_snapshot = None
                line.supplier_name = None
                line.layer_count = None
                line.flute_type = None
                line.board_length_mm = None
                line.board_width_mm = None
                line.sheet_type = None
                line.component_type = None
                line.pieces_per_box = None
                line.stock_yield_per_sheet = None
                line.crease_type = None
                line.crease_left_mm = None
                line.crease_middle_mm = None
                line.crease_right_mm = None
                line.cutting_note = None
            else:
                line.product_id = None
                line.inventory_code_snapshot = None
                line.product_name_snapshot = None
        elif field == "ownership_type":
            normalized = _enum(value, OWNERSHIP_VALUES)
            if normalized is None:
                raise InventoryOnboardingError("库存归属无效")
            line.ownership_type = normalized
            if (
                normalized == "general"
                and line.inventory_type == "semi_finished"
            ):
                line.customer_id = None
                line.customer_code_snapshot = None
                line.customer_name_snapshot = None
        elif field == "unit":
            normalized = _enum(value, UNIT_VALUES)
            if normalized is None:
                raise InventoryOnboardingError("库存单位无效")
            line.unit = normalized
        elif field == "stock_date_accuracy":
            normalized = _enum(value, DATE_ACCURACY_VALUES)
            if normalized is None:
                raise InventoryOnboardingError("日期可信度无效")
            line.stock_date_accuracy = normalized
        elif field == "sheet_type":
            normalized = _enum(value, SHEET_TYPE_VALUES)
            if normalized is None:
                raise InventoryOnboardingError("片料类型无效")
            line.sheet_type = normalized
        elif field == "component_type":
            normalized = _enum(value, COMPONENT_VALUES)
            if normalized is None:
                raise InventoryOnboardingError("组件类型无效")
            line.component_type = normalized
        elif field == "action_decision":
            action = _text(value)
            if action not in EDITABLE_ACTIONS:
                raise InventoryOnboardingError(
                    "处理决定只能为 pending、create_new 或 exclude"
                )
            line.action_decision = action
        else:
            raise InventoryOnboardingError(f"字段 {field} 不允许修改")

    line.version += 1
    line.updated_by = operator.id
    match_onboarding_line(db, batch=batch, line=line)
    batch.version += 1
    batch.updated_by = operator.id
    _invalidate_dry_run(batch)
    db.add(
        _operation_log(
            user=operator,
            batch=batch,
            action="LINE_UPDATE",
            description=(
                "库存建账草稿明细标记为不计入本次盘点"
                if line.action_decision == "exclude"
                else "修正库存建账草稿明细"
            ),
            details={
                "batch_number": batch.batch_number,
                "line_id": line.id,
                "source_row_number": line.source_row_number,
                "changed_fields": sorted(values),
                "line_version_before": line_version_before,
                "line_version_after": line.version,
                "batch_version_before": batch_version_before,
                "batch_version_after": batch.version,
                "match_status": line.match_status,
                "action_decision": line.action_decision,
            },
        )
    )
    db.flush()
    return line


def rematch_onboarding_batch(
    db: Session,
    *,
    batch_id: int,
    expected_version: int,
    operator: User,
) -> list[InventoryOnboardingLine]:
    batch = get_onboarding_batch(db, batch_id)
    _require_draft(batch)
    if batch.version != expected_version:
        raise InventoryOnboardingError(
            "批次已被其他操作更新，请刷新后重试",
            409,
            "INVENTORY_ONBOARDING_VERSION_CONFLICT",
        )
    batch_version_before = batch.version
    batch.version += 1
    _invalidate_dry_run(batch)
    lines = _rematch_batch_lines(db, batch, bump_versions=True)
    batch.updated_by = operator.id
    db.add(
        _operation_log(
            user=operator,
            batch=batch,
            action="REMATCH",
            description="重新匹配库存建账草稿",
            details={
                "batch_number": batch.batch_number,
                "batch_version_before": batch_version_before,
                "batch_version_after": batch.version,
                "line_count": len(lines),
                "blocked_rows": sum(
                    line.match_status == "blocked" for line in lines
                ),
            },
        )
    )
    db.flush()
    return lines


def _fingerprint_payload(
    batch: InventoryOnboardingBatch,
    lines: Iterable[InventoryOnboardingLine],
) -> dict[str, object]:
    fields = (
        "source_row_hash",
        "stocktake_date",
        "stocktaker_name",
        "inventory_type",
        "ownership_type",
        "location_id",
        "location_code_snapshot",
        "floor_snapshot",
        "warehouse_name_snapshot",
        "area_code_snapshot",
        "pallet_code",
        "customer_id",
        "customer_code_snapshot",
        "customer_name_snapshot",
        "product_id",
        "inventory_code_snapshot",
        "product_name_snapshot",
        "material_id",
        "material_code_snapshot",
        "quantity",
        "unit",
        "source_type",
        "stock_date",
        "stock_date_accuracy",
        "stock_date_original_text",
        "supplier_name",
        "layer_count",
        "flute_type",
        "board_length_mm",
        "board_width_mm",
        "sheet_type",
        "component_type",
        "pieces_per_box",
        "stock_yield_per_sheet",
        "crease_type",
        "crease_left_mm",
        "crease_middle_mm",
        "crease_right_mm",
        "cutting_note",
        "remarks",
        "action_decision",
        "existing_lot_id",
        "existing_pallet_id",
        "match_status",
        "version",
    )
    rows: list[dict[str, object]] = []
    for line in lines:
        row: dict[str, object] = {}
        for field in fields:
            value = getattr(line, field)
            row[field] = value.isoformat() if isinstance(value, date) else value
        row["errors"] = list(line.error_codes_json or [])
        row["warnings"] = list(line.warning_codes_json or [])
        row["evidence"] = line.match_evidence_json or {}
        rows.append(row)
    return {
        "source_file_sha256": batch.source_file_sha256,
        "rows": rows,
    }


def _dry_run_summary(
    batch: InventoryOnboardingBatch,
    lines: list[InventoryOnboardingLine],
) -> dict[str, object]:
    return {
        "total_rows": len(lines),
        "create_new_rows": sum(
            line.action_decision == "create_new" for line in lines
        ),
        "route_n035_rows": sum(
            line.action_decision == "route_n035" for line in lines
        ),
        "route_semi_adjust_rows": sum(
            line.action_decision == "route_semi_adjust" for line in lines
        ),
        "route_snapshot_conversion_rows": sum(
            line.action_decision == "route_snapshot_conversion"
            for line in lines
        ),
        "excluded_rows": sum(
            line.action_decision == "exclude" for line in lines
        ),
        "blocked_rows": sum(line.match_status == "blocked" for line in lines),
        "error_count": sum(
            len(line.error_codes_json or []) for line in lines
        ),
        "warning_count": sum(
            len(line.warning_codes_json or []) for line in lines
        ),
        "resolved_floor": batch.resolved_floor,
        "resolved_area_code": batch.resolved_area_code,
        "source_file_sha256": batch.source_file_sha256,
    }


def dry_run_onboarding_batch(
    db: Session,
    *,
    batch_id: int,
    expected_version: int,
    operator: User,
) -> list[InventoryOnboardingLine]:
    batch = get_onboarding_batch(db, batch_id)
    _require_draft(batch)
    if batch.version != expected_version:
        raise InventoryOnboardingError(
            "批次已被其他操作更新，请刷新后重试",
            409,
            "INVENTORY_ONBOARDING_VERSION_CONFLICT",
        )
    lines = _rematch_batch_lines(db, batch, bump_versions=True)
    payload = _fingerprint_payload(batch, lines)
    fingerprint = sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    batch.dry_run_fingerprint = fingerprint
    batch.dry_run_summary_json = _dry_run_summary(batch, lines)
    batch.dry_run_by = operator.id
    batch.dry_run_at = utc_now_naive()
    batch.version += 1
    batch.updated_by = operator.id
    db.add(
        _operation_log(
            user=operator,
            batch=batch,
            action="DRY_RUN",
            description="执行库存建账草稿 dry-run",
            details={
                "batch_number": batch.batch_number,
                "dry_run_fingerprint": fingerprint,
                "summary": batch.dry_run_summary_json,
            },
        )
    )
    db.flush()
    return lines


def submit_onboarding_batch(
    db: Session,
    *,
    batch_id: int,
    expected_version: int,
    dry_run_fingerprint: str,
    idempotency_key: str,
    confirmed: bool,
    operator: User,
) -> list[InventoryOnboardingLine]:
    key = idempotency_key.strip()
    if not key:
        raise InventoryOnboardingError("幂等键不能为空")
    replay = db.scalar(
        select(InventoryOnboardingBatch).where(
            InventoryOnboardingBatch.submit_idempotency_key == key
        )
    )
    if replay is not None:
        if (
            replay.id != batch_id
            or replay.dry_run_fingerprint != dry_run_fingerprint
        ):
            raise InventoryOnboardingError(
                "幂等键已用于不同的库存建账提交",
                409,
                "INVENTORY_ONBOARDING_IDEMPOTENCY_CONFLICT",
            )
        return _batch_lines(db, replay.id)
    if not confirmed:
        raise InventoryOnboardingError(
            "必须明确确认仅冻结 B1 草稿且不会生成正式库存",
            422,
            "INVENTORY_ONBOARDING_CONFIRMATION_REQUIRED",
        )
    batch = get_onboarding_batch(db, batch_id)
    _require_draft(batch)
    if batch.version != expected_version:
        raise InventoryOnboardingError(
            "批次已被其他操作更新，请刷新后重试",
            409,
            "INVENTORY_ONBOARDING_VERSION_CONFLICT",
        )
    if (
        not batch.dry_run_fingerprint
        or batch.dry_run_fingerprint != dry_run_fingerprint
    ):
        raise InventoryOnboardingError(
            "dry-run 已过期，请重新执行",
            409,
            "INVENTORY_ONBOARDING_DRY_RUN_STALE",
        )
    if not _source_file_is_current(batch):
        raise InventoryOnboardingError(
            "私有盘点源文件缺失、大小变化或 SHA-256 不一致，不能提交冻结",
            409,
            "INVENTORY_ONBOARDING_SOURCE_FILE_STALE",
        )
    lines = _batch_lines(db, batch.id)
    if (
        _fingerprint_for(batch, lines) != batch.dry_run_fingerprint
        or not _evidence_is_current(db, lines)
    ):
        raise InventoryOnboardingError(
            "dry-run 后主数据、库位、栈板或库存事实已变化，请重新执行",
            409,
            "INVENTORY_ONBOARDING_DRY_RUN_STALE",
        )
    summary = batch.dry_run_summary_json or {}
    if int(summary.get("error_count", 1)) != 0:
        raise InventoryOnboardingError(
            "仍有阻断错误，不能提交冻结",
            409,
            "INVENTORY_ONBOARDING_BLOCKED",
        )
    if not any(line.action_decision != "exclude" for line in lines):
        raise InventoryOnboardingError(
            "不能提交全部排除的空批次",
            409,
            "INVENTORY_ONBOARDING_EMPTY",
        )
    if batch.resolved_area_code is None:
        raise InventoryOnboardingError(
            "非排除行必须且只能属于一个区域",
            409,
            "INVENTORY_ONBOARDING_AREA_REQUIRED",
        )
    allowed_statuses = {"ready", "routed", "excluded"}
    if any(line.match_status not in allowed_statuses for line in lines):
        raise InventoryOnboardingError(
            "仍有未完成匹配的明细",
            409,
            "INVENTORY_ONBOARDING_BLOCKED",
        )
    batch.status = "submitted"
    batch.submit_idempotency_key = key
    batch.submitted_by = operator.id
    batch.submitted_at = utc_now_naive()
    batch.version += 1
    batch.updated_by = operator.id
    db.add(
        _operation_log(
            user=operator,
            batch=batch,
            action="SUBMIT",
            description="提交并冻结库存建账草稿",
            details={
                "batch_number": batch.batch_number,
                "dry_run_fingerprint": dry_run_fingerprint,
                "idempotency_key": key,
                "summary": summary,
                "formal_inventory_created": False,
            },
        )
    )
    db.flush()
    return lines


def _fingerprint_for(
    batch: InventoryOnboardingBatch,
    lines: Iterable[InventoryOnboardingLine],
) -> str:
    return sha256(
        json.dumps(
            _fingerprint_payload(batch, lines),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def validate_submitted_onboarding_batch(
    db: Session,
    *,
    batch_id: int,
) -> tuple[InventoryOnboardingBatch, list[InventoryOnboardingLine]]:
    """Return an immutable B1 batch only while every frozen fact is current."""

    batch = get_onboarding_batch(db, batch_id)
    if batch.status != "submitted":
        raise InventoryOnboardingError(
            "只有已提交冻结的库存建账批次可以正式入账",
            409,
            "INVENTORY_ONBOARDING_NOT_SUBMITTED",
        )
    lines = _batch_lines(db, batch.id)
    if (
        not batch.dry_run_fingerprint
        or _fingerprint_for(batch, lines) != batch.dry_run_fingerprint
        or not _source_file_is_current(batch)
        or not _evidence_is_current(db, lines)
    ):
        raise InventoryOnboardingError(
            "库存建账批次的源文件、指纹或现场事实已变化，请重新建批",
            409,
            "INVENTORY_ONBOARDING_SUBMITTED_EVIDENCE_STALE",
        )
    return batch, lines


def _source_file_is_current(batch: InventoryOnboardingBatch) -> bool:
    reference = batch.source_file_reference
    if not reference.startswith(PRIVATE_REFERENCE_PREFIX):
        return False
    try:
        path = resolve_stored_reference(reference)
        digest = sha256()
        size = 0
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                size += len(block)
                digest.update(block)
    except (FileNotFoundError, OSError):
        return False
    return (
        size == batch.source_size
        and digest.hexdigest() == batch.source_file_sha256
    )


def _evidence_is_current(
    db: Session,
    lines: Iterable[InventoryOnboardingLine],
) -> bool:
    for line in lines:
        evidence = line.match_evidence_json or {}
        for key, model in (
            ("customer", Customer),
            ("product", Product),
            ("material", Material),
        ):
            snapshot = evidence.get(key)
            if not isinstance(snapshot, dict):
                continue
            entity_id = snapshot.get("id")
            current = db.get(model, entity_id) if entity_id is not None else None
            if (
                current is None
                or snapshot.get("version") != getattr(current, "version", None)
            ):
                return False
        location_snapshot = evidence.get("location")
        if isinstance(location_snapshot, dict):
            location = db.get(WarehouseLocation, location_snapshot.get("id"))
            if location is None or any(
                (
                    location_snapshot.get("code") != location.location_code,
                    location_snapshot.get("floor") != location.warehouse_floor,
                    location_snapshot.get("area_code") != location.area_code,
                    location_snapshot.get("warehouse_type")
                    != location.warehouse_type,
                    location_snapshot.get("placement_status")
                    != location.placement_status,
                    location_snapshot.get("is_active") != location.is_active,
                )
            ):
                return False
        pallet_lookup = evidence.get("pallet_lookup")
        if isinstance(pallet_lookup, dict):
            code = pallet_lookup.get("code")
            current = (
                db.scalar(
                    select(InventoryPallet).where(
                        InventoryPallet.pallet_code == code
                    )
                )
                if code
                else None
            )
            if pallet_lookup.get("expected_id") != (
                current.id if current is not None else None
            ):
                return False
        occupancy_snapshot = evidence.get("location_occupancy")
        if isinstance(occupancy_snapshot, dict):
            location_id = occupancy_snapshot.get("location_id")
            current = (
                db.scalar(
                    select(InventoryPallet).where(
                        InventoryPallet.location_id == location_id,
                        InventoryPallet.is_current.is_(True),
                    )
                )
                if location_id is not None
                else None
            )
            if occupancy_snapshot.get("expected_current_pallet_id") != (
                current.id if current is not None else None
            ):
                return False
        pallet_snapshot = evidence.get("pallet")
        if isinstance(pallet_snapshot, dict):
            pallet = db.get(InventoryPallet, pallet_snapshot.get("id"))
            if pallet is None or any(
                (
                    pallet_snapshot.get("code") != pallet.pallet_code,
                    pallet_snapshot.get("location_id") != pallet.location_id,
                    pallet_snapshot.get("status") != pallet.status,
                    pallet_snapshot.get("is_current") != pallet.is_current,
                    pallet_snapshot.get("version") != pallet.version,
                )
            ):
                return False
            item_snapshots = evidence.get("pallet_items")
            if isinstance(item_snapshots, list):
                current_items = db.scalars(
                    select(InventoryPalletItem)
                    .where(InventoryPalletItem.pallet_id == pallet.id)
                    .order_by(InventoryPalletItem.id)
                ).all()
                if item_snapshots != [
                    _pallet_item_evidence(item) for item in current_items
                ]:
                    return False
        lot_snapshot = evidence.get("existing_lot")
        if isinstance(lot_snapshot, dict):
            lot = db.get(InventoryLot, lot_snapshot.get("id"))
            if lot is None or lot_snapshot != _lot_evidence(lot):
                return False
    return True


def _operation_log(
    *,
    user: User,
    batch: InventoryOnboardingBatch,
    action: str,
    description: str,
    details: dict[str, object],
) -> OperationLog:
    return OperationLog(
        user_id=user.id,
        action=f"N081_B1_{action}",
        resource="InventoryOnboardingBatch",
        details=json.dumps(details, ensure_ascii=False, sort_keys=True),
        username=user.username,
        role=user.role,
        entity_type="inventory_onboarding_batch",
        entity_id=batch.id,
        description=description,
    )


def batch_payload(batch: InventoryOnboardingBatch) -> dict[str, object]:
    summary = batch.dry_run_summary_json
    return {
        "id": batch.id,
        "batch_number": batch.batch_number,
        "status": batch.status,
        "version": batch.version,
        "source_file_reference": batch.source_file_reference,
        "source_file_sha256": batch.source_file_sha256,
        "source_original_filename": batch.source_original_filename,
        "source_content_type": batch.source_content_type,
        "source_size": batch.source_size,
        "source_format": batch.source_format,
        "source_encoding": batch.source_encoding,
        "resolved_floor": batch.resolved_floor,
        "resolved_area_code": batch.resolved_area_code,
        "dry_run_fingerprint": batch.dry_run_fingerprint,
        "dry_run_summary": summary,
        "dry_run_at": (
            utc_naive_to_api(batch.dry_run_at)
            if batch.dry_run_at is not None
            else None
        ),
        "submitted_at": (
            utc_naive_to_api(batch.submitted_at)
            if batch.submitted_at is not None
            else None
        ),
        "created_at": (
            utc_naive_to_api(batch.created_at)
            if batch.created_at is not None
            else None
        ),
        "updated_at": (
            utc_naive_to_api(batch.updated_at)
            if batch.updated_at is not None
            else None
        ),
    }


def line_payload(line: InventoryOnboardingLine) -> dict[str, object]:
    return {
        "id": line.id,
        "batch_id": line.batch_id,
        "source_sheet_name": line.source_sheet_name,
        "source_row_number": line.source_row_number,
        "source_row_hash": line.source_row_hash,
        "raw_row_text": line.raw_row_text,
        "original_values": line.original_values_json,
        "stocktake_date": line.stocktake_date,
        "stocktaker_name": line.stocktaker_name,
        "inventory_type": line.inventory_type,
        "ownership_type": line.ownership_type,
        "location_id": line.location_id,
        "location_code": line.location_code_snapshot,
        "floor": line.floor_snapshot,
        "warehouse_name": line.warehouse_name_snapshot,
        "area_code": line.area_code_snapshot,
        "pallet_code": line.pallet_code,
        "customer_id": line.customer_id,
        "customer_code": line.customer_code_snapshot,
        "customer_name": line.customer_name_snapshot,
        "product_id": line.product_id,
        "inventory_code": line.inventory_code_snapshot,
        "product_name": line.product_name_snapshot,
        "material_id": line.material_id,
        "material_code": line.material_code_snapshot,
        "quantity": line.quantity,
        "unit": line.unit,
        "source_type": line.source_type,
        "stock_date": line.stock_date,
        "stock_date_accuracy": line.stock_date_accuracy,
        "stock_date_original_text": line.stock_date_original_text,
        "supplier_name": line.supplier_name,
        "layer_count": line.layer_count,
        "flute_type": line.flute_type,
        "board_length_mm": line.board_length_mm,
        "board_width_mm": line.board_width_mm,
        "sheet_type": line.sheet_type,
        "component_type": line.component_type,
        "pieces_per_box": line.pieces_per_box,
        "stock_yield_per_sheet": line.stock_yield_per_sheet,
        "crease_type": line.crease_type,
        "crease_left_mm": line.crease_left_mm,
        "crease_middle_mm": line.crease_middle_mm,
        "crease_right_mm": line.crease_right_mm,
        "cutting_note": line.cutting_note,
        "remarks": line.remarks,
        "action_decision": line.action_decision,
        "existing_lot_id": line.existing_lot_id,
        "existing_pallet_id": line.existing_pallet_id,
        "match_status": line.match_status,
        "error_codes": list(line.error_codes_json or []),
        "warning_codes": list(line.warning_codes_json or []),
        "match_evidence": line.match_evidence_json or {},
        "version": line.version,
        "updated_at": (
            utc_naive_to_api(line.updated_at)
            if line.updated_at is not None
            else None
        ),
    }


def error_csv_bytes(lines: Iterable[InventoryOnboardingLine]) -> bytes:
    stream = StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(
        (
            "工作表",
            "原始行号",
            "处理状态",
            "错误代码",
            "警告代码",
            "库位",
            "栈板号",
            "客户",
            "存货编码",
            "产品名称",
            "数量",
        )
    )

    def safe(value: object) -> object:
        if not isinstance(value, str):
            return value
        return f"'{value}" if value.lstrip().startswith(("=", "+", "-", "@")) else value

    for line in lines:
        if not line.error_codes_json and not line.warning_codes_json:
            continue
        writer.writerow(
            tuple(
                safe(value)
                for value in (
                    line.source_sheet_name,
                    line.source_row_number,
                    line.match_status,
                    "|".join(line.error_codes_json or []),
                    "|".join(line.warning_codes_json or []),
                    line.location_code_snapshot,
                    line.pallet_code,
                    line.customer_name_snapshot,
                    line.inventory_code_snapshot,
                    line.product_name_snapshot,
                    line.quantity,
                )
            )
        )
    return ("\ufeff" + stream.getvalue()).encode("utf-8")
