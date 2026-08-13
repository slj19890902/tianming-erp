from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
import json
import re
import time
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import (
    PermissionChecker,
    RoleChecker,
    get_db,
    require_customer_access,
)
from app.api.master_data_common import audit_master_change, clean_code
from app.api.products import (
    ProductPayload,
    _normalize_product_mold_binding,
    _product_payload_snapshot,
    _product_write_data,
    _validate_changed_product_crease_widths,
    _validate_product_crease_widths,
    _validate_product_material_flute,
    _validate_references,
)
from app.core.time_contract import beijing_now_naive
from app.models.customer import Customer
from app.models.audit import OperationLog
from app.models.material import Material
from app.models.mold_tool import MoldTool
from app.models.product import Product
from app.models.product_drawing import ProductDrawing
from app.models.user import User
from app.services.master_data_versioning import (
    apply_versioned_update,
    normalize_json_value,
    record_versioned_create,
    serialize_versioned_entity,
)
from app.services.product_drawings import (
    DrawingValidationError,
    remove_drawing_files,
    save_product_drawing_files,
    validate_product_drawing_upload,
)
from app.services.product_import_workbook import (
    EmbeddedProductDrawing,
    GLUE_PROCESS_TOKENS,
    MOLD_SHEET,
    NAIL_PROCESS_TOKENS,
    PRODUCT_SHEET,
    SINGLE_PHOTO_CONFIRMED_REMARK,
    ProductImportPreview,
    ProductImportWorkbookError,
    build_product_import_workbook,
    consume_preview,
    get_preview,
    project_workbook_managed_process_tokens,
    read_product_import_workbook,
    single_photo_requirement_confirmed,
    store_preview,
)
from app.services.mixed_sample_import import (
    MIXED_PRODUCT_SHEET,
    MixedSampleImportPreview,
    build_mixed_sample_workbook,
    candidate_view,
    consume_mixed_preview,
    get_mixed_preview,
    merge_registration,
    normalize_sample_code,
    process_from_registration,
    read_mixed_reference_workbook,
    read_mixed_sample_workbook,
    select_reference_candidates,
    store_mixed_preview,
)
from app.services.secure_uploads import (
    DRAWING_POLICY,
    EXCEL_POLICY,
    UploadTokenError,
    UploadValidationError,
    ValidatedUpload,
    create_temporary_token,
    discard_temporary_token,
    read_validated_upload,
    temporary_token_file,
    validate_upload_bytes,
)


router = APIRouter()
can_read = PermissionChecker("products.view")
admin_only = RoleChecker(["admin"])


class ProductImportApplyPayload(BaseModel):
    preview_token: str = Field(min_length=20, max_length=200)


def _changed_import_updates(product: Product, updates: dict) -> dict:
    """Compare workbook values without treating Decimal database values as changes."""

    changed: dict = {}
    for key, value in updates.items():
        current = getattr(product, key, None)
        if isinstance(current, Decimal) and isinstance(value, (Decimal, int, float)):
            if current == Decimal(str(value)):
                continue
        elif normalize_json_value(current) == normalize_json_value(value):
            continue
        changed[key] = value
    return changed


def _workbook_error(error: ProductImportWorkbookError) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": error.message},
    )


def _append_error(
    errors: list[dict],
    *,
    sheet: str,
    row_number: int,
    message: str,
) -> None:
    if len(errors) < 300:
        errors.append(
            {"sheet": sheet, "row_number": row_number, "message": message}
        )


def _error_message(error: Exception) -> str:
    if isinstance(error, HTTPException):
        if isinstance(error.detail, dict):
            return str(error.detail.get("message") or error.detail.get("code") or error.detail)
        return str(error.detail)
    if isinstance(error, ValidationError):
        messages = [
            str(item.get("msg", "")).removeprefix("Value error, ")
            for item in error.errors()
        ]
        return "；".join(message for message in messages if message) or "字段校验失败"
    return str(error)


def _find_exact_material(db: Session, reference: str) -> Material | None:
    supplier_name: str | None = None
    code = reference.strip()
    if "|" in code:
        supplier_name, code = (part.strip() for part in code.split("|", 1))
        if not supplier_name or not code:
            raise ValueError("材质请填写“供应商|材质代码”，两部分都不能为空")
    statement = select(Material).where(func.upper(Material.code) == code.upper())
    if supplier_name:
        statement = statement.where(
            func.upper(func.trim(Material.supplier_name)) == supplier_name.upper()
        )
    rows = db.scalars(statement).all()
    if not rows:
        return None
    if len(rows) > 1:
        suppliers = "、".join(
            sorted({(row.supplier_name or "未标供应商").strip() for row in rows})
        )
        raise ValueError(
            f"材质代码 {code} 在多个供应商下存在（{suppliers}），"
            "请填写“供应商|材质代码”"
        )
    return rows[0]


def _find_exact_mold(db: Session, code: str) -> MoldTool | None:
    return db.scalar(
        select(MoldTool)
        .where(func.upper(MoldTool.mold_code) == code.upper())
        .limit(1)
    )


def _product_conflict(
    db: Session,
    *,
    customer_id: int,
    product_code: str,
    customer_material_code: str,
    product_name: str,
    exclude_id: int | None = None,
) -> Product | None:
    statement = select(Product).where(
        Product.customer_id == customer_id,
        Product.product_name == product_name.strip(),
        or_(
            Product.product_code == clean_code(product_code),
            Product.customer_material_code == clean_code(customer_material_code),
        ),
    )
    if exclude_id is not None:
        statement = statement.where(Product.id != exclude_id)
    return db.scalar(statement.limit(1))


def _find_exact_product_for_import(
    db: Session,
    *,
    customer_id: int,
    product_code: str,
    product_name: str,
) -> Product | None:
    code = clean_code(product_code)
    rows = db.scalars(
        select(Product).where(
            Product.customer_id == customer_id,
            Product.deleted_at.is_(None),
            or_(
                Product.product_code == code,
                Product.customer_material_code == code,
            ),
        )
    ).all()
    target_name = product_name.strip()
    exact = {row.id: row for row in rows if row.product_name == target_name}
    if len(exact) > 1:
        raise ValueError(
            f"存货编码 {code} 与产品名称 {target_name} 命中多个常用箱，请先整理重复主档"
        )
    return next(iter(exact.values()), None)


def _base_product_payload(customer_id: int, row: dict) -> dict:
    return {
        "customer_id": customer_id,
        "product_code": row["product_code"],
        "customer_material_code": row["customer_material_code"],
        "product_name": row["product_name"],
        "material_id": None,
        "mold_tool_id": None,
        "legacy_material_text": row.get("legacy_material_text"),
        "length_mm": row["length_mm"],
        "width_mm": row["width_mm"],
        "height_mm": row["height_mm"],
        "box_category": row["box_category"],
        "box_style": row["box_style"],
        "print_content": row["print_content"],
        "printing_colors": row["printing_colors"],
        "production_process": row["production_process"],
        "unit": row["unit"],
        "sale_unit_price": row["sale_unit_price"],
        "sale_unit_price_no_tax": None,
        "cost_unit_price": None,
        "board_price": None,
        "suggested_price": None,
        "die_cut_path": None,
        "remark": row["remark"],
        "is_active": row["is_active"],
        "flute_type": row["flute_type"],
        "layer_count": row["layer_count"],
        "surface_paper_type": None,
        "report_length_mm": row["report_length_mm"],
        "report_width_mm": row["report_width_mm"],
        "crease_type": row["crease_type"],
        "crease_left_mm": row["crease_left_mm"],
        "crease_middle_mm": row["crease_middle_mm"],
        "crease_right_mm": row["crease_right_mm"],
        "report_notes": row["report_notes"],
        "base_report_length_mm": None,
        "base_report_width_mm": None,
        "base_crease_type": None,
        "base_crease_left_mm": None,
        "base_crease_middle_mm": None,
        "base_crease_right_mm": None,
        "base_report_notes": None,
        "splice_mode": "single",
        "pieces_per_box": None,
        "default_cutting_mode": row.get("default_cutting_mode") or "一开一",
        "flap_mm": 30,
        "combination_mode": "parent_priced_set",
    }


def _overlay_import_fields(data: dict, row: dict) -> dict:
    existing_projection = project_workbook_managed_process_tokens(
        box_category=data.get("box_category"),
        print_content=data.get("print_content"),
        printing_colors=data.get("printing_colors"),
        production_process=data.get("production_process"),
    )
    desired_projection = tuple(row["managed_process_tokens"])
    existing_projection_set = set(existing_projection)
    desired_projection_set = set(desired_projection)
    process_dimensions = (
        ({"模切", "开槽"}, ("模切", "开槽")),
        ({"印刷"}, ("印刷",)),
        (
            {*NAIL_PROCESS_TOKENS, *GLUE_PROCESS_TOKENS},
            ("打钉", "粘合"),
        ),
        ({"二次粘合"}, ("二次粘合",)),
    )
    changed_dimensions: list[tuple[set[str], tuple[str, ...]]] = []
    for stored_tokens, canonical_tokens in process_dimensions:
        existing_choice = tuple(
            token for token in canonical_tokens if token in existing_projection_set
        )
        desired_choice = tuple(
            token for token in canonical_tokens if token in desired_projection_set
        )
        if existing_choice != desired_choice:
            changed_dimensions.append((stored_tokens, desired_choice))
    imported = _base_product_payload(int(data["customer_id"]), row)
    for field_name in (
        "product_code",
        "customer_material_code",
        "product_name",
        "length_mm",
        "width_mm",
        "height_mm",
        "box_category",
        "printing_colors",
        "unit",
        "sale_unit_price",
        "remark",
        "is_active",
        "report_length_mm",
        "report_width_mm",
        "legacy_material_text",
        "default_cutting_mode",
    ):
        value = imported[field_name]
        if field_name == "is_active" and row.get("action") == "自动":
            continue
        if field_name in {"product_code"} or value not in {None, ""}:
            data[field_name] = value
    if not row["flute_was_blank"]:
        data["flute_type"] = imported["flute_type"]
        data["layer_count"] = imported["layer_count"]
    if row["box_style"] is not None:
        data["box_style"] = row["box_style"]
    elif data["box_category"] != "die_cut" and data.get("box_style") == "模切内盒":
        data["box_style"] = None
    if not changed_dimensions:
        return data
    if any("印刷" in stored_tokens for stored_tokens, _choice in changed_dimensions):
        data["print_content"] = row["print_content"]
        data["printing_colors"] = row["printing_colors"]
    existing_tokens = [
        item.strip()
        for item in re.split(r"[,，、;；]+", str(data.get("production_process") or ""))
        if item.strip()
    ]
    removed_tokens = set().union(
        *(stored_tokens for stored_tokens, _choice in changed_dimensions)
    )
    combined_tokens = [
        item for item in existing_tokens if item not in removed_tokens
    ]
    for _stored_tokens, desired_choice in changed_dimensions:
        for token in desired_choice:
            if token not in combined_tokens:
                combined_tokens.append(token)
    data["production_process"] = "、".join(combined_tokens) or None
    return data


def _completion_missing_fields(
    payload: ProductPayload,
    *,
    material: Material | None,
) -> tuple[str, ...]:
    """Return non-blocking master-data gaps shown during workbook preview."""
    missing: list[str] = []
    if material is None:
        missing.append("材质代码")
    elif not (material.supplier_name or "").strip():
        missing.append("材质供应商")
    if payload.layer_count not in {3, 5, 7}:
        missing.append("材质层数")
    if not (payload.flute_type or "").strip():
        missing.append("楞型")
    if payload.report_length_mm is None:
        missing.append("报料长")
    if payload.report_width_mm is None:
        missing.append("报料宽")
    if not (payload.crease_type or "").strip():
        missing.append("压线类型")
    if payload.sale_unit_price is None:
        missing.append("默认含税单价")
    return tuple(missing)


def _validated_plan(
    db: Session,
    *,
    customer_id: int,
    row: dict,
    new_mold_codes: set[str],
    allow_unregistered_sample_mold: bool = False,
) -> dict:
    product: Product | None = None
    requested_action = row["action"]
    if requested_action == "更新" or (
        requested_action == "自动" and row.get("system_id") is not None
    ):
        product = db.get(Product, row["system_id"])
        if product is None:
            raise ValueError("系统ID对应的常用箱不存在")
        if product.customer_id != customer_id:
            raise ValueError("系统ID不属于模板锁定客户")
        if row.get("version") is not None and product.version != row["version"]:
            raise ValueError(
                f"版本已变化：模板为 {row['version']}，当前为 {product.version}，请重新下载模板"
            )
    elif requested_action == "自动":
        product = _find_exact_product_for_import(
            db,
            customer_id=customer_id,
            product_code=row["product_code"],
            product_name=row.get("product_name") or "",
        )

    normalized_row = dict(row)
    if product is not None:
        normalized_row["customer_material_code"] = (
            normalized_row.get("customer_material_code")
            or product.customer_material_code
        )
        normalized_row["product_name"] = (
            normalized_row.get("product_name") or product.product_name
        )
        data = _overlay_import_fields(
            _product_payload_snapshot(product),
            normalized_row,
        )
    else:
        if not normalized_row.get("customer_material_code"):
            raise ValueError("新产品必须填写客户料号")
        if not normalized_row.get("product_name"):
            raise ValueError("新产品必须填写产品名称")
        data = _base_product_payload(customer_id, normalized_row)

    conflict = _product_conflict(
        db,
        customer_id=customer_id,
        product_code=normalized_row["product_code"],
        customer_material_code=normalized_row["customer_material_code"],
        product_name=normalized_row["product_name"],
        exclude_id=product.id if product is not None else None,
    )
    if conflict is not None:
        raise ValueError(
            f"存货编码或客户料号与现有常用箱冲突（系统ID {conflict.id}）"
        )

    material: Material | None = None
    if normalized_row["material_code"]:
        material = _find_exact_material(db, normalized_row["material_code"])
        if material is None:
            raise ValueError(f"材质代码不存在：{normalized_row['material_code']}")
        if not material.is_active:
            raise ValueError(f"材质代码已停用：{normalized_row['material_code']}")
        data["material_id"] = material.id
    elif product is not None and product.material_id is not None:
        material = db.get(Material, product.material_id)
        data["material_id"] = product.material_id
    else:
        data["material_id"] = None

    mold: MoldTool | None = None
    mold_code = normalized_row["mold_code"]
    if mold_code:
        mold = _find_exact_mold(db, mold_code)
        if mold is not None and not mold.is_active:
            raise ValueError(f"模具已停用：{mold_code}")
        if mold is None and mold_code not in new_mold_codes:
            raise ValueError(
                f"模具不存在：{mold_code}；如需新增，请在“模具档案”页填写新增资料"
            )
        data["mold_tool_id"] = mold.id if mold is not None else None
    elif product is not None and product.mold_tool_id is not None:
        mold = db.get(MoldTool, product.mold_tool_id)
        mold_code = mold.mold_code if mold is not None else None
        data["mold_tool_id"] = product.mold_tool_id
    else:
        data["mold_tool_id"] = None

    payload = ProductPayload.model_validate(data)
    if normalized_row["mold_code"] in new_mold_codes:
        # The new mold receives a real ID during apply.  Use a positive
        # placeholder so the same die-cut binding rule is checked at preview.
        payload.mold_tool_id = 1
    if not allow_unregistered_sample_mold:
        _normalize_product_mold_binding(payload)
    if mold_code and payload.mold_tool_id is None:
        raise ValueError("填写模具编号时，生产工艺必须包含“模切”")
    _validate_references(
        db,
        customer_id=customer_id,
        material_id=payload.material_id,
        mold_tool_id=(
            None
            if normalized_row["mold_code"] in new_mold_codes
            else payload.mold_tool_id
        ),
        historical_material_id=(product.material_id if product is not None else None),
    )
    _validate_product_material_flute(db, payload)
    if product is None:
        _validate_product_crease_widths(payload)
    else:
        _validate_changed_product_crease_widths(payload, product)

    payload_data = payload.model_dump()
    # ``external_supply`` is an API-only write projection. Its persisted facts
    # are the explicit external-packaging snapshot columns already present in
    # ``payload_data``; comparing it to the ORM object creates a false update.
    payload_data.pop("external_supply", None)
    if normalized_row["mold_code"] in new_mold_codes:
        payload_data["mold_tool_id"] = None
    missing_fields = _completion_missing_fields(payload, material=material)
    if (
        allow_unregistered_sample_mold
        and payload.box_category == "die_cut"
        and payload.mold_tool_id is None
    ):
        missing_fields = (*missing_fields, "生产模具")
    changed_fields = (
        tuple(sorted(_changed_import_updates(product, payload_data)))
        if product is not None
        else tuple(sorted(payload_data))
    )
    effective_action = (
        "新增" if product is None else ("更新" if changed_fields else "补图")
    )
    return {
        "row_number": row["row_number"],
        "action": effective_action,
        "requested_action": requested_action,
        "system_id": product.id if product is not None else None,
        "expected_version": product.version if product is not None else None,
        "sample_id": normalized_row["sample_id"],
        "payload": payload_data,
        "material_id": material.id if material is not None else None,
        "material_version": material.version if material is not None else None,
        "historical_material_id": product.material_id if product is not None else None,
        "mold_code": mold_code,
        "existing_mold_id": mold.id if mold is not None else None,
        "drawing_filenames": normalized_row["drawing_filenames"],
        "_embedded_drawings": tuple(normalized_row.get("_embedded_drawings") or ()),
        "source_rows": tuple(normalized_row.get("_source_rows") or ()),
        "changed_fields": changed_fields,
        "completion_status": "待完善" if missing_fields else "资料已完善",
        "missing_fields": missing_fields,
    }


def _staged_upload(stored) -> ValidatedUpload:
    content = stored.path.read_bytes()
    if len(content) != stored.size or sha256(content).hexdigest() != stored.sha256:
        raise UploadTokenError("图纸临时文件校验失败")
    extension = stored.path.suffix.lower()
    return ValidatedUpload(
        content=content,
        original_filename=stored.original_filename,
        extension=extension,
        content_type=stored.content_type,
        size=stored.size,
        sha256=stored.sha256,
    )


def _existing_drawing_source_hashes(db: Session, product_id: int) -> set[str]:
    hashes: set[str] = set()
    logs = db.scalars(
        select(OperationLog).where(
            OperationLog.resource == "Product",
            OperationLog.entity_id == product_id,
            OperationLog.action.in_({"UPLOAD_DRAWING", "IMPORT_REFERENCE_IMAGE"}),
        )
    ).all()
    for log in logs:
        try:
            details = json.loads(log.details or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        for field_name in ("sha256", "source_sha256"):
            value = str(details.get(field_name) or "").strip().lower()
            if re.fullmatch(r"[a-f0-9]{64}", value):
                hashes.add(value)
    return hashes


def _product_row_key(row: dict) -> tuple[str, str]:
    if row.get("system_id") is not None:
        return ("id", str(row["system_id"]))
    return ("code", clean_code(str(row.get("product_code") or "")).casefold())


def _blank_merge_value(value: object) -> bool:
    return value is None or value == "" or value == ()


def _merge_product_row(target: dict, incoming: dict) -> str | None:
    source = f"{incoming.get('_source_file')} 第 {incoming.get('_source_row')} 行"
    target_action = str(target.get("action") or "自动")
    incoming_action = str(incoming.get("action") or "自动")
    if target_action == "自动" and incoming_action != "自动":
        target["action"] = incoming_action
    elif (
        incoming_action != "自动"
        and target_action != "自动"
        and incoming_action != target_action
    ):
        return f"同一常用箱在不同卷的操作冲突：{target_action}/{incoming_action}（{source}）"

    ignored = {
        "row_number",
        "action",
        "drawing_filenames",
        "_embedded_drawings",
        "_source_file",
        "_source_row",
        "_source_rows",
    }
    for field_name, value in incoming.items():
        if field_name in ignored:
            continue
        current = target.get(field_name)
        if _blank_merge_value(current) and not _blank_merge_value(value):
            target[field_name] = value
        elif (
            not _blank_merge_value(value)
            and not _blank_merge_value(current)
            and current != value
        ):
            return f"同一常用箱在不同卷的字段“{field_name}”不一致（{source}）"

    names = list(target.get("drawing_filenames") or ())
    seen_names = {name.casefold() for name in names}
    for name in incoming.get("drawing_filenames") or ():
        if name.casefold() not in seen_names:
            names.append(name)
            seen_names.add(name.casefold())
    target["drawing_filenames"] = tuple(names)
    merged_drawings = list(target.get("_embedded_drawings") or ())
    drawing_hashes = {sha256(image.content).hexdigest() for image in merged_drawings}
    for image in incoming.get("_embedded_drawings") or ():
        digest = sha256(image.content).hexdigest()
        if digest not in drawing_hashes:
            merged_drawings.append(image)
            drawing_hashes.add(digest)
    target["_embedded_drawings"] = tuple(merged_drawings)
    target.setdefault("_source_rows", []).append(
        {
            "file": incoming.get("_source_file"),
            "row_number": incoming.get("_source_row"),
        }
    )
    return None


def _merge_mold_row(target: dict, incoming: dict) -> str | None:
    for field_name in ("mold_name", "rack_location", "remarks"):
        current = target.get(field_name)
        value = incoming.get(field_name)
        if _blank_merge_value(current) and not _blank_merge_value(value):
            target[field_name] = value
        elif (
            not _blank_merge_value(current)
            and not _blank_merge_value(value)
            and current != value
        ):
            return f"模具 {target['mold_code']} 在不同卷的{field_name}不一致"
    return None


def _mixed_customer_by_code(db: Session, code: str) -> Customer | None:
    return db.scalar(
        select(Customer)
        .where(func.upper(func.trim(Customer.customer_code)) == code.upper())
        .limit(1)
    )


def _mixed_layer_count(flute_type: str | None) -> int | None:
    if flute_type in {"A", "B", "E"}:
        return 3
    if flute_type in {"AB", "BE"}:
        return 5
    if flute_type in {"AAA", "ABC"}:
        return 7
    return None


def _mixed_registration_row(
    *,
    item_number: int,
    registration: dict,
    reference: dict,
    customer_id: int,
) -> tuple[dict, list[str]]:
    (
        box_category,
        box_style,
        _joining_method,
        printed,
        managed_tokens,
        unresolved,
    ) = process_from_registration(registration, reference)
    reference_code = normalize_sample_code(reference["product_code"])
    handwritten_code = normalize_sample_code(registration["product_code"])
    product_code = (
        handwritten_code
        if handwritten_code.startswith(reference_code) and handwritten_code != reference_code
        else reference_code
    )
    flute_type = registration.get("flute_type") or reference.get("flute_type")
    layer_count = _mixed_layer_count(flute_type) or reference.get("layer_count")
    colors = str(registration.get("printing_colors") or "").strip()
    if printed and (not colors or colors == "无"):
        colors = "黑色"
    if not printed:
        colors = ""
    production_process = "、".join(managed_tokens) or None
    row = {
        "row_number": item_number,
        "action": "自动",
        "system_id": None,
        "version": None,
        "sample_id": registration["sample_id"],
        "product_code": product_code,
        "customer_material_code": product_code,
        "product_name": reference["product_name"],
        "box_category": box_category,
        "box_style": box_style,
        "length_mm": reference.get("length_mm"),
        "width_mm": reference.get("width_mm"),
        "height_mm": reference.get("height_mm"),
        "material_code": None,
        "legacy_material_text": reference.get("legacy_material_text"),
        "layer_count": layer_count,
        "flute_type": flute_type,
        "flute_was_blank": flute_type is None,
        "unit": "只",
        "sale_unit_price": reference.get("sale_unit_price"),
        "report_length_mm": reference.get("report_length_mm"),
        "report_width_mm": reference.get("report_width_mm"),
        "crease_type": reference.get("crease_type"),
        "crease_left_mm": reference.get("crease_left_mm"),
        "crease_middle_mm": reference.get("crease_middle_mm"),
        "crease_right_mm": reference.get("crease_right_mm"),
        "production_process": production_process,
        "managed_process_tokens": managed_tokens,
        "is_printed": printed,
        "print_content": "单色印刷" if printed else "无印刷",
        "printing_colors": colors or None,
        "mold_code": None,
        "drawing_filenames": tuple(registration.get("drawing_filenames") or ()),
        "report_notes": None,
        "remark": None,
        "is_active": True,
        "default_cutting_mode": reference.get("default_cutting_mode") or "一开一",
        "_embedded_drawings": tuple(registration.get("_embedded_drawings") or ()),
        "_source_rows": list(registration.get("_source_rows") or ()),
        "_mixed_customer_id": customer_id,
        "_mixed_customer_code": reference["customer_code"],
        "_reference_key": reference["key"],
    }
    return row, unresolved


def _mixed_photo_prefix(filename: str) -> str:
    stem = filename.rsplit(".", 1)[0]
    return normalize_sample_code(stem)


def _mixed_filename_matches(value: str, prefix: str) -> bool:
    if value == prefix:
        return True
    return any(value.startswith(f"{prefix}{separator}") for separator in ("_", "-", "（", "("))


@router.get("/import-template.xlsx")
def download_product_import_template(
    customer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> Response:
    require_customer_access(customer_id, current_user=user, db=db)
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="客户不存在")
    content = build_product_import_workbook(db, customer=customer)
    filename = f"常用箱导入导出模板_{customer.name}.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
        },
    )


@router.get("/mixed-import-template.xlsx")
def download_mixed_sample_import_template(
    _user: User = Depends(admin_only),
) -> Response:
    content = build_mixed_sample_workbook()
    filename = "混合客户样品现场登记与图片导入模板.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
        },
    )


@router.post("/mixed-import/preview")
async def preview_mixed_sample_import(
    reference_file: UploadFile = File(...),
    files: list[UploadFile] = File(default=[]),
    drawings: list[UploadFile] = File(default=[]),
    overrides: str = Form("{}"),
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    if not files:
        raise HTTPException(status_code=400, detail="请至少选择一份样品 Excel")
    if len(files) > 20:
        raise HTTPException(status_code=400, detail="一次最多上传20份样品 Excel")
    if len(drawings) > 400:
        raise HTTPException(status_code=400, detail="一次最多上传400张样品照片")
    try:
        parsed_overrides = json.loads(overrides or "{}")
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=400, detail="待确认客户选择格式错误") from error
    if not isinstance(parsed_overrides, dict):
        raise HTTPException(status_code=400, detail="待确认客户选择格式错误")
    normalized_overrides: dict[str, list[str]] = {}
    for key, value in parsed_overrides.items():
        if not isinstance(key, str):
            raise HTTPException(status_code=400, detail="待确认客户选择格式错误")
        if isinstance(value, str):
            normalized_overrides[key] = [value] if value else []
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            normalized_overrides[key] = value
        else:
            raise HTTPException(status_code=400, detail="待确认客户选择格式错误")

    errors: list[dict] = []
    try:
        reference_upload = await read_validated_upload(reference_file, EXCEL_POLICY)
        reference_records, reference_errors = read_mixed_reference_workbook(
            reference_upload.content
        )
        errors.extend(reference_errors)
    except (UploadValidationError, ProductImportWorkbookError) as error:
        message = error.message if isinstance(error, ProductImportWorkbookError) else str(error)
        return {
            "valid": False,
            "errors": [{"sheet": "三客户基础资料", "row_number": 1, "message": message}],
            "unresolved": [],
            "summary": {"workbooks": 0, "samples": 0, "drawings": 0},
        }

    registrations: dict[str, dict] = {}
    source_files: list[dict] = []
    total_excel_bytes = 0
    for upload in files:
        filename = (upload.filename or "样品Excel").strip() or "样品Excel"
        try:
            workbook_upload = await read_validated_upload(upload, EXCEL_POLICY)
            total_excel_bytes += workbook_upload.size
            if total_excel_bytes > 160 * 1024 * 1024:
                raise UploadValidationError("本批次样品 Excel 合计不能超过160MB")
            rows, workbook_errors = read_mixed_sample_workbook(workbook_upload.content)
        except (UploadValidationError, ProductImportWorkbookError) as error:
            message = error.message if isinstance(error, ProductImportWorkbookError) else str(error)
            errors.append({"sheet": filename, "row_number": 1, "message": message})
            continue
        source_files.append(
            {"filename": filename, "sha256": workbook_upload.sha256, "size": workbook_upload.size}
        )
        for workbook_error in workbook_errors:
            errors.append(
                {
                    **workbook_error,
                    "sheet": f"{filename} / {workbook_error['sheet']}",
                }
            )
        for row in rows:
            incoming = dict(row)
            incoming["_source_rows"] = [
                {"file": filename, "row_number": row["row_number"]}
            ]
            key = str(row["sample_id"]).casefold()
            existing = registrations.get(key)
            if existing is None:
                registrations[key] = incoming
                continue
            merge_error = merge_registration(existing, incoming)
            existing.setdefault("_source_rows", []).append(
                {"file": filename, "row_number": row["row_number"]}
            )
            if merge_error:
                errors.append(
                    {
                        "sheet": filename,
                        "row_number": row["row_number"],
                        "message": merge_error,
                    }
                )

    external_uploads: dict[str, ValidatedUpload] = {}
    total_drawing_bytes = 0
    for upload in drawings:
        filename = (upload.filename or "").strip()
        try:
            validated = await read_validated_upload(upload, DRAWING_POLICY)
            total_drawing_bytes += validated.size
            if total_drawing_bytes > 500 * 1024 * 1024:
                raise UploadValidationError("本批次照片合计不能超过500MB")
        except UploadValidationError as error:
            errors.append({"sheet": "照片", "row_number": 1, "message": str(error)})
            continue
        key = filename.casefold()
        if not filename:
            errors.append({"sheet": "照片", "row_number": 1, "message": "存在空照片文件名"})
        elif key in external_uploads:
            errors.append({"sheet": "照片", "row_number": 1, "message": f"照片文件名重复：{filename}"})
        else:
            external_uploads[key] = validated

    product_code_counts: dict[str, int] = {}
    for registration in registrations.values():
        code = normalize_sample_code(registration["product_code"])
        product_code_counts[code] = product_code_counts.get(code, 0) + 1
    used_external: set[str] = set()
    for registration in registrations.values():
        names = list(registration.get("drawing_filenames") or ())
        images = list(registration.get("_embedded_drawings") or ())
        embedded_count = len(images)
        if not embedded_count and names:
            matched_names: list[str] = []
            matched_images: list[EmbeddedProductDrawing] = []
            for position, filename in enumerate(names, start=1):
                validated = external_uploads.get(filename.casefold())
                if validated is None:
                    errors.append(
                        {
                            "sheet": MIXED_PRODUCT_SHEET,
                            "row_number": registration["row_number"],
                            "message": f"没有同时选择照片：{filename}",
                        }
                    )
                    continue
                matched_names.append(validated.original_filename)
                matched_images.append(
                    EmbeddedProductDrawing(
                        row_number=registration["row_number"],
                        column_number=10,
                        column_offset=position,
                        content=validated.content,
                    )
                )
                used_external.add(filename.casefold())
            names, images = matched_names, matched_images

        sample_prefix = normalize_sample_code(registration["sample_id"])
        code_prefix = normalize_sample_code(registration["product_code"])
        for key, validated in external_uploads.items():
            if key in used_external:
                continue
            file_prefix = _mixed_photo_prefix(validated.original_filename)
            if _mixed_filename_matches(file_prefix, sample_prefix) or (
                product_code_counts.get(code_prefix) == 1
                and _mixed_filename_matches(file_prefix, code_prefix)
            ):
                names.append(validated.original_filename)
                images.append(
                    EmbeddedProductDrawing(
                        row_number=registration["row_number"],
                        column_number=10,
                        column_offset=len(images) + 1,
                        content=validated.content,
                    )
                )
                used_external.add(key)
        registration["drawing_filenames"] = tuple(names)
        registration["_embedded_drawings"] = tuple(images)
        if not images:
            errors.append(
                {
                    "sheet": MIXED_PRODUCT_SHEET,
                    "row_number": registration["row_number"],
                    "message": f"{registration['sample_id']} 没有匹配到照片；请把照片命名为“样品号_序号.jpg”",
                }
            )
        elif len(images) > 4:
            errors.append(
                {
                    "sheet": MIXED_PRODUCT_SHEET,
                    "row_number": registration["row_number"],
                    "message": f"{registration['sample_id']} 一次最多导入4张照片",
                }
            )
    for key, validated in external_uploads.items():
        if key not in used_external:
            errors.append(
                {
                    "sheet": "照片",
                    "row_number": 1,
                    "message": f"照片未匹配任何样品：{validated.original_filename}",
                }
            )

    unresolved: list[dict] = []
    resolved: list[tuple[dict, dict, Customer]] = []
    customer_cache: dict[str, Customer | None] = {}
    for registration in registrations.values():
        sample_id = str(registration["sample_id"])
        override_keys = normalized_overrides.get(sample_id)
        selected_references, candidates, selection_error = select_reference_candidates(
            reference_records,
            registration,
            override_keys=override_keys,
        )
        if not selected_references:
            unresolved.append(
                {
                    "sample_id": registration["sample_id"],
                    "product_code": registration["product_code"],
                    "reason": (
                        selection_error
                        or (
                            "基础资料中找不到该型号"
                            if not candidates
                            else "该型号对应多个客户或多款资料，请明确选择；跨客户通用可同时勾选"
                        )
                    ),
                    "candidates": [candidate_view(candidate) for candidate in candidates],
                }
            )
            continue
        for reference in selected_references:
            customer_code = reference["customer_code"]
            if customer_code not in customer_cache:
                customer_cache[customer_code] = _mixed_customer_by_code(db, customer_code)
            customer = customer_cache[customer_code]
            if customer is None:
                errors.append(
                    {
                        "sheet": "客户主档",
                        "row_number": registration["row_number"],
                        "message": f"ERP 中不存在客户代码 {customer_code}",
                    }
                )
                continue
            require_customer_access(customer.id, current_user=user, db=db)
            resolved.append((registration, reference, customer))

    product_items: list[dict] = []
    seen_products: set[tuple[int, str]] = set()
    for item_number, (registration, reference, customer) in enumerate(resolved, start=1):
        try:
            row, unresolved_fields = _mixed_registration_row(
                item_number=item_number,
                registration=registration,
                reference=reference,
                customer_id=customer.id,
            )
            if unresolved_fields:
                raise ValueError(f"请在现场表确认：{'、'.join(unresolved_fields)}")
            product_key = (customer.id, clean_code(row["product_code"]).casefold())
            if product_key in seen_products:
                raise ValueError("同一客户的同一存货编码在本批次对应多个样品")
            seen_products.add(product_key)
            plan = _validated_plan(
                db,
                customer_id=customer.id,
                row=row,
                new_mold_codes=set(),
                allow_unregistered_sample_mold=True,
            )
            plan["customer_id"] = customer.id
            plan["customer_code"] = reference["customer_code"]
            plan["reference_key"] = reference["key"]
            if reference.get("product_name_was_blank"):
                plan["missing_fields"] = (
                    *plan["missing_fields"],
                    "产品名称（基础资料原空）",
                )
                plan["completion_status"] = "待完善"
            product_items.append(plan)
        except (ValueError, ValidationError, HTTPException) as error:
            _append_error(
                errors,
                sheet=MIXED_PRODUCT_SHEET,
                row_number=registration["row_number"],
                message=f"{registration['sample_id']}：{_error_message(error)}",
            )

    required_names: dict[str, tuple[str, bytes, int]] = {}
    for item in product_items:
        names = tuple(item.get("drawing_filenames") or ())
        images = tuple(item.get("_embedded_drawings") or ())
        if len(names) != len(images):
            _append_error(
                errors,
                sheet=MIXED_PRODUCT_SHEET,
                row_number=item["row_number"],
                message="图片文件名与图片数量不一致",
            )
            continue
        for filename, image in zip(names, images, strict=True):
            key = filename.casefold()
            existing = required_names.get(key)
            if existing is not None:
                if sha256(existing[1]).digest() == sha256(image.content).digest():
                    continue
                _append_error(
                    errors,
                    sheet=MIXED_PRODUCT_SHEET,
                    row_number=item["row_number"],
                    message=f"照片同名但内容不同：{filename}",
                )
                continue
            required_names[key] = (filename, image.content, item["row_number"])

    summary = {
        "workbooks": len(source_files),
        "samples": len(registrations),
        "resolved_samples": len(
            {str(item["sample_id"]).casefold() for item in product_items}
        ),
        "resolved_products": len(product_items),
        "unresolved_samples": len(unresolved),
        "create_products": sum(item["action"] == "新增" for item in product_items),
        "update_products": sum(item["action"] == "更新" for item in product_items),
        "drawing_only_products": sum(item["action"] == "补图" for item in product_items),
        "drawings": sum(
            len(registration.get("_embedded_drawings") or ())
            for registration in registrations.values()
        ),
        "resolved_drawings": len(required_names),
        "needs_completion": sum(bool(item["missing_fields"]) for item in product_items),
    }
    groups = [
        {
            "customer_code": code,
            "customer_name": customer_cache[code].name if customer_cache.get(code) else code,
            "count": sum(item["customer_code"] == code for item in product_items),
            "create_products": sum(item["customer_code"] == code and item["action"] == "新增" for item in product_items),
            "update_products": sum(item["customer_code"] == code and item["action"] == "更新" for item in product_items),
            "drawing_only_products": sum(item["customer_code"] == code and item["action"] == "补图" for item in product_items),
        }
        for code in ("YL", "YKE", "KEW")
        if any(item["customer_code"] == code for item in product_items)
    ]
    if errors or unresolved:
        return {
            "valid": False,
            "errors": errors,
            "unresolved": unresolved,
            "summary": summary,
            "groups": groups,
        }

    drawing_tokens: dict[str, str] = {}
    try:
        for _key, (filename, content, _row_number) in required_names.items():
            extension = filename.rsplit(".", 1)[-1].casefold()
            content_type = {
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "png": "image/png",
                "webp": "image/webp",
                "pdf": "application/pdf",
            }.get(extension, "application/octet-stream")
            validated = validate_upload_bytes(
                content=content,
                filename=filename,
                content_type=content_type,
                policy=DRAWING_POLICY,
            )
            validate_product_drawing_upload(validated)
            drawing_tokens[filename] = create_temporary_token(validated, owner_id=user.id)
    except (UploadValidationError, DrawingValidationError) as error:
        for token in drawing_tokens.values():
            try:
                discard_temporary_token(token, owner_id=user.id)
            except Exception:
                pass
        return {
            "valid": False,
            "errors": [{"sheet": "照片", "row_number": 1, "message": str(error)}],
            "unresolved": [],
            "summary": summary,
            "groups": groups,
        }

    source_digest = sha256()
    source_digest.update(reference_upload.sha256.encode("ascii"))
    for item in sorted(source_files, key=lambda row: (row["filename"], row["sha256"])):
        source_digest.update(f"{item['filename']}:{item['sha256']}\n".encode("utf-8"))
    for item in sorted(external_uploads.values(), key=lambda row: (row.original_filename, row.sha256)):
        source_digest.update(f"{item.original_filename}:{item.sha256}\n".encode("utf-8"))
    customer_ids = tuple(sorted({item["customer_id"] for item in product_items}))
    preview = MixedSampleImportPreview(
        actor=f"id:{user.id}",
        owner_id=user.id,
        expires_at=time.monotonic() + 10 * 60,
        customer_ids=customer_ids,
        source_sha256=source_digest.hexdigest(),
        reference_sha256=reference_upload.sha256,
        source_files=tuple(source_files),
        product_items=tuple(product_items),
        drawing_tokens=drawing_tokens,
        summary=summary,
    )
    token = store_mixed_preview(preview)
    return {
        "valid": True,
        "preview_token": token,
        "errors": [],
        "unresolved": [],
        "summary": summary,
        "groups": groups,
        "items": [
            {
                "customer_code": item["customer_code"],
                "row_number": item["row_number"],
                "action": item["action"],
                "sample_id": item["sample_id"],
                "product_code": item["payload"]["product_code"],
                "product_name": item["payload"]["product_name"],
                "missing_fields": list(item["missing_fields"]),
                "changed_fields": list(item["changed_fields"]),
                "source_rows": list(item["source_rows"]),
            }
            for item in product_items
        ],
        "expires_in_seconds": 600,
    }


@router.post("/import/preview")
async def preview_product_import(
    customer_id: int = Form(...),
    files: list[UploadFile] = File(default=[]),
    file: UploadFile | None = File(default=None),
    drawings: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    require_customer_access(customer_id, current_user=user, db=db)
    if db.get(Customer, customer_id) is None:
        raise HTTPException(status_code=404, detail="客户不存在")
    uploads = ([file] if file is not None else []) + list(files)
    if not uploads:
        raise HTTPException(status_code=400, detail="请至少选择一份样品 Excel")
    if len(uploads) > 20:
        raise HTTPException(status_code=400, detail="一次最多上传20卷样品 Excel")

    errors: list[dict] = []
    merged_products: dict[tuple[str, str], dict] = {}
    merged_molds: dict[str, dict] = {}
    source_files: list[dict] = []
    total_upload_bytes = 0
    for upload in uploads:
        filename = (upload.filename or "样品Excel").strip() or "样品Excel"
        try:
            workbook_upload = await read_validated_upload(upload, EXCEL_POLICY)
            total_upload_bytes += workbook_upload.size
            if total_upload_bytes > 160 * 1024 * 1024:
                raise UploadValidationError("本批次 Excel 合计不能超过160MB")
            (
                workbook_customer_id,
                workbook_product_rows,
                workbook_mold_rows,
                workbook_errors,
                embedded_drawings,
            ) = read_product_import_workbook(workbook_upload.content)
        except (UploadValidationError, ProductImportWorkbookError) as error:
            message = error.message if isinstance(error, ProductImportWorkbookError) else str(error)
            errors.append(
                {
                    "sheet": filename,
                    "row_number": 1,
                    "message": message,
                }
            )
            continue

        source_files.append(
            {
                "filename": filename,
                "sha256": workbook_upload.sha256,
                "size": workbook_upload.size,
            }
        )
        for workbook_error in workbook_errors:
            errors.append(
                {
                    **workbook_error,
                    "sheet": f"{filename} / {workbook_error['sheet']}",
                }
            )
        if workbook_customer_id != customer_id:
            errors.append(
                {
                    "sheet": f"{filename} / 导入信息",
                    "row_number": 2,
                    "message": "Excel 锁定客户与页面当前客户不一致，请从该客户页面下载模板",
                }
            )

        for row in workbook_product_rows:
            incoming = dict(row)
            incoming["_source_file"] = filename
            incoming["_source_row"] = row["row_number"]
            incoming["_source_rows"] = [
                {"file": filename, "row_number": row["row_number"]}
            ]
            incoming["_embedded_drawings"] = tuple(
                embedded_drawings.get(row["row_number"], ())
            )
            key = _product_row_key(incoming)
            if not key[1]:
                errors.append(
                    {
                        "sheet": f"{filename} / {PRODUCT_SHEET}",
                        "row_number": row["row_number"],
                        "message": "存货编码不能为空",
                    }
                )
                continue
            existing = merged_products.get(key)
            if existing is None:
                merged_products[key] = incoming
                continue
            merge_error = _merge_product_row(existing, incoming)
            if merge_error:
                errors.append(
                    {
                        "sheet": f"{filename} / {PRODUCT_SHEET}",
                        "row_number": row["row_number"],
                        "message": merge_error,
                    }
                )

        for row in workbook_mold_rows:
            key = row["mold_code"].casefold()
            existing = merged_molds.get(key)
            if existing is None:
                merged_molds[key] = dict(row)
                continue
            merge_error = _merge_mold_row(existing, row)
            if merge_error:
                errors.append(
                    {
                        "sheet": f"{filename} / {MOLD_SHEET}",
                        "row_number": row["row_number"],
                        "message": merge_error,
                    }
                )

    product_rows = list(merged_products.values())
    mold_rows = list(merged_molds.values())
    if len(product_rows) > 2_000:
        errors.append(
            {
                "sheet": PRODUCT_SHEET,
                "row_number": 1,
                "message": "合并后样品超过2000款，请拆成多个批次",
            }
        )
    source_digest = sha256()
    for item in sorted(source_files, key=lambda row: (row["filename"], row["sha256"])):
        source_digest.update(f"{item['filename']}:{item['sha256']}\n".encode("utf-8"))
    source_sha256 = source_digest.hexdigest()

    mold_items: list[dict] = []
    new_mold_codes: set[str] = set()
    for row in mold_rows:
        existing = _find_exact_mold(db, row["mold_code"])
        if existing is not None:
            conflicting_fields: list[str] = []
            for field_name, label in (
                ("mold_name", "模具名称"),
                ("rack_location", "存放位置"),
                ("remarks", "备注"),
            ):
                incoming_value = str(row.get(field_name) or "").strip()
                existing_value = str(getattr(existing, field_name, None) or "").strip()
                if incoming_value and incoming_value != existing_value:
                    conflicting_fields.append(label)
            if conflicting_fields:
                _append_error(
                    errors,
                    sheet=MOLD_SHEET,
                    row_number=row["row_number"],
                    message=(
                        f"模具编号已存在且{'、'.join(conflicting_fields)}不一致："
                        f"{row['mold_code']}，请核对后重试"
                    ),
                )
            continue
        mold_items.append(row)
        new_mold_codes.add(row["mold_code"])

    product_items: list[dict] = []
    seen_ids: set[int] = set()
    seen_codes: set[tuple[str, str]] = set()
    seen_customer_codes: set[tuple[str, str]] = set()
    for row in product_rows:
        try:
            if row["system_id"] is not None:
                if row["system_id"] in seen_ids:
                    raise ValueError("同一系统ID在本批次重复")
                seen_ids.add(row["system_id"])
            normalized_product_code = clean_code(row["product_code"])
            normalized_customer_code = clean_code(row["customer_material_code"])
            normalized_name = str(row.get("product_name") or "").strip()
            product_identity = (normalized_product_code, normalized_name)
            customer_identity = (normalized_customer_code, normalized_name)
            if product_identity in seen_codes:
                raise ValueError("存货编码与产品名称在本批次重复")
            if customer_identity in seen_customer_codes:
                raise ValueError("客户料号与产品名称在本批次重复")
            seen_codes.add(product_identity)
            seen_customer_codes.add(customer_identity)
            plan = _validated_plan(
                db,
                customer_id=customer_id,
                row=row,
                new_mold_codes=new_mold_codes,
            )
            if (
                plan["action"] == "补图"
                and not plan["drawing_filenames"]
                and not row.get("_embedded_drawings")
            ):
                continue
            product_items.append(plan)
        except (ValueError, ValidationError, HTTPException) as error:
            _append_error(
                errors,
                sheet=PRODUCT_SHEET,
                row_number=row["row_number"],
                message=_error_message(error),
            )

    if not product_items and not mold_items and not errors:
        errors.append(
            {
                "sheet": PRODUCT_SHEET,
                "row_number": 1,
                "message": "本批次没有新增产品、字段变化或新增图片",
            }
        )

    required_names: dict[str, str] = {}
    drawing_name_rows: dict[str, int] = {}
    for item in product_items:
        for name in item["drawing_filenames"]:
            key = name.casefold()
            if key in required_names:
                _append_error(
                    errors,
                    sheet=PRODUCT_SHEET,
                    row_number=item["row_number"],
                    message=f"图纸文件名在本批次重复：{name}",
                )
                continue
            required_names[key] = name
            drawing_name_rows[key] = item["row_number"]

    embedded_by_name: dict[str, bytes] = {}
    embedded_rows: set[int] = set()
    for item in product_items:
        row_number = item["row_number"]
        images = tuple(item.get("_embedded_drawings") or ())
        if not images:
            continue
        embedded_rows.add(row_number)
        filenames = item["drawing_filenames"]
        required_image_count = (
            1
            if single_photo_requirement_confirmed(
                item["payload"].get("remark")
            )
            else 2
        )
        if item["action"] == "新增" and len(images) != required_image_count:
            requirement = (
                f"恰好1张（现场备注须为“{SINGLE_PHOTO_CONFIRMED_REMARK}”）"
                if required_image_count == 1
                else "恰好2张"
            )
            _append_error(
                errors,
                sheet=PRODUCT_SHEET,
                row_number=row_number,
                message=(
                    f"内嵌图片模式的新增行必须{requirement}，"
                    f"当前为{len(images)}张"
                ),
            )
        if len(images) != len(filenames):
            _append_error(
                errors,
                sheet=PRODUCT_SHEET,
                row_number=row_number,
                message=(
                    f"J列文件名数量({len(filenames)})与本行内嵌图数量"
                    f"({len(images)})不一致"
                ),
            )
            continue
        for filename, image in zip(filenames, images, strict=True):
            extension = filename.rsplit(".", 1)[-1].casefold()
            if extension not in {"jpg", "jpeg", "png"}:
                _append_error(
                    errors,
                    sheet=PRODUCT_SHEET,
                    row_number=row_number,
                    message=f"内嵌图文件名只允许 JPG/JPEG/PNG：{filename}",
                )
                continue
            image_key = filename.casefold()
            existing_content = embedded_by_name.get(image_key)
            if existing_content is not None and existing_content != image.content:
                _append_error(
                    errors,
                    sheet=PRODUCT_SHEET,
                    row_number=row_number,
                    message=f"不同卷中的同名图片内容不一致：{filename}",
                )
                continue
            embedded_by_name[image_key] = image.content

    uploaded_by_name: dict[str, UploadFile] = {}
    for upload in drawings:
        filename = (upload.filename or "").strip()
        key = filename.casefold()
        if not filename:
            errors.append(
                {"sheet": PRODUCT_SHEET, "row_number": 1, "message": "上传图纸存在空文件名"}
            )
        elif key in uploaded_by_name:
            errors.append(
                {
                    "sheet": PRODUCT_SHEET,
                    "row_number": 1,
                    "message": f"上传图纸文件名重复：{filename}",
                }
            )
        else:
            uploaded_by_name[key] = upload
    for item in product_items:
        if item["action"] != "新增":
            continue
        required_image_count = (
            1
            if single_photo_requirement_confirmed(item["payload"].get("remark"))
            else 2
        )
        if len(item["drawing_filenames"]) != required_image_count:
            _append_error(
                errors,
                sheet=PRODUCT_SHEET,
                row_number=item["row_number"],
                message=(
                    f"新增产品必须提供{required_image_count}张图片文件名；"
                    "默认2张，明确单图时按现场备注规则填写"
                ),
            )
    for key, filename in required_names.items():
        if key not in uploaded_by_name and key not in embedded_by_name:
            errors.append(
                {
                    "sheet": PRODUCT_SHEET,
                    "row_number": drawing_name_rows[key],
                    "message": f"缺少本行内嵌图或同时上传的图纸原文件：{filename}",
                }
            )
    for key, upload in uploaded_by_name.items():
        if key not in required_names:
            errors.append(
                {
                    "sheet": PRODUCT_SHEET,
                    "row_number": 1,
                    "message": f"上传了 Excel 未引用的图纸：{upload.filename}",
                }
            )
        elif drawing_name_rows[key] in embedded_rows:
            errors.append(
                {
                    "sheet": PRODUCT_SHEET,
                    "row_number": drawing_name_rows[key],
                    "message": "同一行不能混用 Excel 内嵌图和外部上传图纸",
                }
            )
    if errors:
        return {
            "valid": False,
            "errors": errors,
            "summary": {
                "products": len(product_items),
                "molds": len(mold_items),
                "drawings": len(required_names),
                "needs_completion": sum(
                    bool(item["missing_fields"]) for item in product_items
                ),
            },
        }

    drawing_tokens: dict[str, str] = {}
    try:
        for key, filename in required_names.items():
            if key in embedded_by_name:
                extension = filename.rsplit(".", 1)[-1].casefold()
                validated = validate_upload_bytes(
                    content=embedded_by_name[key],
                    filename=filename,
                    content_type=(
                        "image/png" if extension == "png" else "image/jpeg"
                    ),
                    policy=DRAWING_POLICY,
                )
            else:
                validated = await read_validated_upload(
                    uploaded_by_name[key],
                    DRAWING_POLICY,
                )
            validate_product_drawing_upload(validated)
            drawing_tokens[filename] = create_temporary_token(
                validated,
                owner_id=user.id,
            )
    except (UploadValidationError, DrawingValidationError) as error:
        for token in drawing_tokens.values():
            try:
                discard_temporary_token(token, owner_id=user.id)
            except Exception:
                pass
        return {
            "valid": False,
            "errors": [
                {
                    "sheet": PRODUCT_SHEET,
                    "row_number": 1,
                    "message": str(error),
                }
            ],
            "summary": {
                "products": len(product_items),
                "molds": len(mold_items),
                "drawings": len(required_names),
                "needs_completion": sum(
                    bool(item["missing_fields"]) for item in product_items
                ),
            },
        }

    summary = {
        "workbooks": len(source_files),
        "create_products": sum(item["action"] == "新增" for item in product_items),
        "update_products": sum(item["action"] == "更新" for item in product_items),
        "drawing_only_products": sum(
            item["action"] == "补图" for item in product_items
        ),
        "create_molds": len(mold_items),
        "drawings": len(required_names),
        "needs_completion": sum(
            bool(item["missing_fields"]) for item in product_items
        ),
    }
    preview = ProductImportPreview(
        actor=f"id:{user.id}",
        owner_id=user.id,
        expires_at=time.monotonic() + 10 * 60,
        customer_id=customer_id,
        source_sha256=source_sha256,
        source_files=tuple(source_files),
        product_items=tuple(product_items),
        mold_items=tuple(mold_items),
        drawing_tokens=drawing_tokens,
        summary=summary,
    )
    token = store_preview(preview)
    return {
        "valid": True,
        "preview_token": token,
        "summary": summary,
        "items": [
            {
                "row_number": item["row_number"],
                "action": item["action"],
                "sample_id": item["sample_id"],
                "product_code": item["payload"]["product_code"],
                "product_name": item["payload"]["product_name"],
                "completion_status": item["completion_status"],
                "missing_fields": list(item["missing_fields"]),
                "changed_fields": list(item["changed_fields"]),
                "source_rows": list(item["source_rows"]),
            }
            for item in product_items
        ],
        "errors": [],
        "expires_in_seconds": 600,
    }


def _revalidate_preview(db: Session, preview: ProductImportPreview) -> None:
    for item in preview.mold_items:
        if _find_exact_mold(db, item["mold_code"]) is not None:
            raise ProductImportWorkbookError(
                "PRODUCT_IMPORT_CONFLICT",
                f"模具编号已被新增：{item['mold_code']}，请重新预检",
                status_code=409,
            )
    new_mold_codes = {item["mold_code"] for item in preview.mold_items}
    for item in preview.product_items:
        payload = item["payload"]
        product: Product | None = None
        if item["action"] in {"更新", "补图"}:
            product = db.get(Product, item["system_id"])
            if (
                product is None
                or product.customer_id != preview.customer_id
                or product.version != item["expected_version"]
            ):
                raise ProductImportWorkbookError(
                    "PRODUCT_IMPORT_CONFLICT",
                    f"常用箱系统ID {item['system_id']} 已变化，请重新下载模板并预检",
                    status_code=409,
                )
        conflict = _product_conflict(
            db,
            customer_id=preview.customer_id,
            product_code=payload["product_code"],
            customer_material_code=payload["customer_material_code"],
            product_name=payload["product_name"],
            exclude_id=product.id if product is not None else None,
        )
        if conflict is not None:
            raise ProductImportWorkbookError(
                "PRODUCT_IMPORT_CONFLICT",
                "存货编码或客户料号已被占用，请重新预检",
                status_code=409,
            )
        if item["material_id"] is not None:
            material = db.get(Material, item["material_id"])
            if (
                material is None
                or (
                    not material.is_active
                    and item["material_id"] != item.get("historical_material_id")
                )
                or material.version != item["material_version"]
            ):
                raise ProductImportWorkbookError(
                    "PRODUCT_IMPORT_CONFLICT",
                    "所选材质已变化，请重新预检",
                    status_code=409,
                )
        if item["mold_code"] and item["mold_code"] not in new_mold_codes:
            mold = db.get(MoldTool, item["existing_mold_id"])
            if (
                mold is None
                or not mold.is_active
                or mold.mold_code.upper() != item["mold_code"].upper()
            ):
                raise ProductImportWorkbookError(
                    "PRODUCT_IMPORT_CONFLICT",
                    "所选模具已变化，请重新预检",
                    status_code=409,
                )
    for token in preview.drawing_tokens.values():
        temporary_token_file(token, owner_id=preview.owner_id)


def _revalidate_mixed_preview(
    db: Session,
    preview: MixedSampleImportPreview,
) -> None:
    for item in preview.product_items:
        customer_id = int(item["customer_id"])
        payload = item["payload"]
        product: Product | None = None
        if item["action"] in {"更新", "补图"}:
            product = db.get(Product, item["system_id"])
            if (
                product is None
                or product.customer_id != customer_id
                or product.version != item["expected_version"]
            ):
                raise ProductImportWorkbookError(
                    "MIXED_SAMPLE_IMPORT_CONFLICT",
                    f"{item['customer_code']} / {payload['product_code']} 已变化，请重新预检",
                    status_code=409,
                )
        conflict = _product_conflict(
            db,
            customer_id=customer_id,
            product_code=payload["product_code"],
            customer_material_code=payload["customer_material_code"],
            product_name=payload["product_name"],
            exclude_id=product.id if product is not None else None,
        )
        if conflict is not None:
            raise ProductImportWorkbookError(
                "MIXED_SAMPLE_IMPORT_CONFLICT",
                f"{item['customer_code']} / {payload['product_code']} 编码已被占用，请重新预检",
                status_code=409,
            )
        if item["material_id"] is not None:
            material = db.get(Material, item["material_id"])
            if (
                material is None
                or (
                    not material.is_active
                    and item["material_id"] != item.get("historical_material_id")
                )
                or material.version != item["material_version"]
            ):
                raise ProductImportWorkbookError(
                    "MIXED_SAMPLE_IMPORT_CONFLICT",
                    f"{item['customer_code']} / {payload['product_code']} 的材质已变化，请重新预检",
                    status_code=409,
                )
    for token in preview.drawing_tokens.values():
        temporary_token_file(token, owner_id=preview.owner_id)


def _create_product(
    db: Session,
    *,
    payload: ProductPayload,
    user: User,
    allow_unregistered_sample_mold: bool = False,
) -> Product:
    if not allow_unregistered_sample_mold:
        _normalize_product_mold_binding(payload)
    _validate_references(
        db,
        customer_id=payload.customer_id,
        material_id=payload.material_id,
        mold_tool_id=payload.mold_tool_id,
    )
    _validate_product_material_flute(db, payload)
    _validate_product_crease_widths(payload)
    data = _product_write_data(payload, user)
    data.update(
        product_code=clean_code(payload.product_code),
        customer_material_code=clean_code(payload.customer_material_code),
        product_name=payload.product_name.strip(),
        manual_modified=True,
        manual_modified_at=beijing_now_naive(),
    )
    product = Product(**data)
    db.add(product)
    db.flush()
    record_versioned_create(
        db,
        object_type="product",
        entity=product,
        user=user,
        reason="管理员确认常用箱 Excel 批量导入",
        source="api.products.import.apply",
    )
    audit_master_change(
        db,
        user=user,
        action="CREATE",
        resource="Product",
        resource_id=product.id,
        details=data,
    )
    return product


def _update_product(
    db: Session,
    *,
    product: Product,
    payload: ProductPayload,
    expected_version: int,
    user: User,
    allow_unregistered_sample_mold: bool = False,
) -> Product:
    if not allow_unregistered_sample_mold:
        _normalize_product_mold_binding(payload)
    _validate_references(
        db,
        customer_id=payload.customer_id,
        material_id=payload.material_id,
        mold_tool_id=payload.mold_tool_id,
        historical_material_id=product.material_id,
    )
    _validate_product_material_flute(db, payload)
    _validate_changed_product_crease_widths(payload, product)
    before = _product_payload_snapshot(product)
    updates = _product_write_data(payload, user)
    versioned_fields = set(serialize_versioned_entity("product", product))
    updates = {
        key: value for key, value in updates.items() if key in versioned_fields
    }
    updates.update(
        product_code=clean_code(payload.product_code),
        customer_material_code=clean_code(payload.customer_material_code),
        product_name=payload.product_name.strip(),
    )
    changed = _changed_import_updates(product, updates)
    if not changed:
        return product
    apply_versioned_update(
        db,
        object_type="product",
        entity=product,
        updates=updates,
        expected_version=expected_version,
        user=user,
        reason="管理员确认常用箱 Excel 批量导入",
        source="api.products.import.apply",
        action="system_mapping",
    )
    product.manual_modified = True
    product.manual_modified_at = beijing_now_naive()
    if changed:
        audit_master_change(
            db,
            user=user,
            action="UPDATE",
            resource="Product",
            resource_id=product.id,
            details={"before": before, "after": updates},
        )
    return product


@router.post("/import/apply")
def apply_product_import(
    payload: ProductImportApplyPayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    try:
        preview = get_preview(payload.preview_token, user)
        require_customer_access(preview.customer_id, current_user=user, db=db)
        _revalidate_preview(db, preview)
    except ProductImportWorkbookError as error:
        raise _workbook_error(error) from error

    saved_paths: list[tuple[str, str]] = []
    try:
        molds_by_code: dict[str, MoldTool] = {}
        for item in preview.mold_items:
            mold = MoldTool(
                mold_code=item["mold_code"],
                mold_name=item["mold_name"],
                rack_location=item["rack_location"],
                remarks=item["remarks"],
                is_active=True,
                created_by=user.id,
                updated_by=user.id,
            )
            db.add(mold)
            db.flush()
            molds_by_code[item["mold_code"]] = mold
            audit_master_change(
                db,
                user=user,
                action="CREATE",
                resource="MoldTool",
                resource_id=mold.id,
                details={
                    "mold_code": mold.mold_code,
                    "mold_name": mold.mold_name,
                    "rack_location": mold.rack_location,
                    "source": "product_workbook_import",
                },
            )

        products_by_row: dict[int, Product] = {}
        for item in preview.product_items:
            data = dict(item["payload"])
            if item["mold_code"]:
                data["mold_tool_id"] = (
                    item["existing_mold_id"]
                    or molds_by_code[item["mold_code"]].id
                )
            product_payload = ProductPayload.model_validate(data)
            if item["action"] == "新增":
                product = _create_product(db, payload=product_payload, user=user)
            else:
                product = db.get(Product, item["system_id"])
                if product is None:
                    raise ProductImportWorkbookError(
                        "PRODUCT_IMPORT_CONFLICT",
                        "更新目标已不存在，请重新预检",
                        status_code=409,
                    )
                product = _update_product(
                    db,
                    product=product,
                    payload=product_payload,
                    expected_version=item["expected_version"],
                    user=user,
                )
            products_by_row[item["row_number"]] = product

        drawing_count = 0
        skipped_drawing_count = 0
        drawing_evidence: list[dict] = []
        for item in preview.product_items:
            product = products_by_row[item["row_number"]]
            existing_hashes = _existing_drawing_source_hashes(db, product.id)
            for filename in item["drawing_filenames"]:
                token = preview.drawing_tokens[filename]
                stored = temporary_token_file(token, owner_id=preview.owner_id)
                if stored.sha256.lower() in existing_hashes:
                    drawing_evidence.append(
                        {
                            "filename": filename,
                            "sha256": stored.sha256,
                            "product_id": product.id,
                            "status": "already_exists",
                        }
                    )
                    skipped_drawing_count += 1
                    continue
                saved = save_product_drawing_files(
                    product_id=product.id,
                    upload=_staged_upload(stored),
                )
                saved_paths.append((saved.image_path, saved.thumbnail_path))
                drawing = ProductDrawing(
                    product_id=product.id,
                    image_path=saved.image_path,
                    thumbnail_path=saved.thumbnail_path,
                    uploaded_by=user.id,
                )
                db.add(drawing)
                db.flush()
                audit_master_change(
                    db,
                    user=user,
                    action="UPLOAD_DRAWING",
                    resource="Product",
                    resource_id=product.id,
                    details={
                        "drawing_id": drawing.id,
                        "original_filename": filename,
                        "source": "product_workbook_import",
                        "sha256": stored.sha256,
                    },
                )
                drawing_evidence.append(
                    {
                        "filename": filename,
                        "sha256": stored.sha256,
                        "product_id": product.id,
                        "status": "created",
                    }
                )
                existing_hashes.add(stored.sha256.lower())
                drawing_count += 1
        audit_master_change(
            db,
            user=user,
            action="BATCH_IMPORT",
            resource="ProductImport",
            resource_id=preview.customer_id,
            details={
                "customer_id": preview.customer_id,
                "source_xlsx_sha256": preview.source_sha256,
                "source_files": list(preview.source_files),
                "summary": {
                    **preview.summary,
                    "drawings": drawing_count,
                    "skipped_drawings": skipped_drawing_count,
                },
                "drawing_files": drawing_evidence,
            },
        )
        db.commit()
    except ProductImportWorkbookError as error:
        db.rollback()
        for image_path, thumbnail_path in saved_paths:
            remove_drawing_files(image_path, thumbnail_path)
        raise _workbook_error(error) from error
    except IntegrityError as error:
        db.rollback()
        for image_path, thumbnail_path in saved_paths:
            remove_drawing_files(image_path, thumbnail_path)
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PRODUCT_IMPORT_CONFLICT",
                "message": "批量导入时出现编码冲突，整批未写入，请重新预检",
            },
        ) from error
    except Exception:
        db.rollback()
        for image_path, thumbnail_path in saved_paths:
            remove_drawing_files(image_path, thumbnail_path)
        raise

    for token in preview.drawing_tokens.values():
        try:
            discard_temporary_token(token, owner_id=preview.owner_id)
        except Exception:
            pass
    consume_preview(payload.preview_token, keep_files=True)
    return {
        "ok": True,
        "summary": {
            **preview.summary,
            "drawings": drawing_count,
            "skipped_drawings": skipped_drawing_count,
        },
        "message": (
            "常用箱、模具和图片已按整批事务导入"
            + (f"；已跳过 {skipped_drawing_count} 张重复图片" if skipped_drawing_count else "")
        ),
    }


@router.post("/mixed-import/apply")
def apply_mixed_sample_import(
    payload: ProductImportApplyPayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    try:
        preview = get_mixed_preview(payload.preview_token, user)
        for customer_id in preview.customer_ids:
            require_customer_access(customer_id, current_user=user, db=db)
        _revalidate_mixed_preview(db, preview)
    except ProductImportWorkbookError as error:
        raise _workbook_error(error) from error

    saved_paths: list[tuple[str, str]] = []
    try:
        products_by_row: dict[int, Product] = {}
        for item in preview.product_items:
            product_payload = ProductPayload.model_validate(dict(item["payload"]))
            if item["action"] == "新增":
                product = _create_product(
                    db,
                    payload=product_payload,
                    user=user,
                    allow_unregistered_sample_mold=True,
                )
            else:
                product = db.get(Product, item["system_id"])
                if product is None:
                    raise ProductImportWorkbookError(
                        "MIXED_SAMPLE_IMPORT_CONFLICT",
                        "更新目标已不存在，请重新预检",
                        status_code=409,
                    )
                product = _update_product(
                    db,
                    product=product,
                    payload=product_payload,
                    expected_version=item["expected_version"],
                    user=user,
                    allow_unregistered_sample_mold=True,
                )
            products_by_row[item["row_number"]] = product

        drawing_count = 0
        skipped_drawing_count = 0
        drawing_evidence: list[dict] = []
        for item in preview.product_items:
            product = products_by_row[item["row_number"]]
            existing_hashes = _existing_drawing_source_hashes(db, product.id)
            for filename in item["drawing_filenames"]:
                token = preview.drawing_tokens[filename]
                stored = temporary_token_file(token, owner_id=preview.owner_id)
                if stored.sha256.lower() in existing_hashes:
                    drawing_evidence.append(
                        {
                            "filename": filename,
                            "sha256": stored.sha256,
                            "product_id": product.id,
                            "customer_id": item["customer_id"],
                            "status": "already_exists",
                        }
                    )
                    skipped_drawing_count += 1
                    continue
                saved = save_product_drawing_files(
                    product_id=product.id,
                    upload=_staged_upload(stored),
                )
                saved_paths.append((saved.image_path, saved.thumbnail_path))
                drawing = ProductDrawing(
                    product_id=product.id,
                    image_path=saved.image_path,
                    thumbnail_path=saved.thumbnail_path,
                    uploaded_by=user.id,
                )
                db.add(drawing)
                db.flush()
                audit_master_change(
                    db,
                    user=user,
                    action="UPLOAD_DRAWING",
                    resource="Product",
                    resource_id=product.id,
                    details={
                        "drawing_id": drawing.id,
                        "original_filename": filename,
                        "source": "mixed_sample_workbook_import",
                        "sha256": stored.sha256,
                        "reference_sha256": preview.reference_sha256,
                    },
                )
                drawing_evidence.append(
                    {
                        "filename": filename,
                        "sha256": stored.sha256,
                        "product_id": product.id,
                        "customer_id": item["customer_id"],
                        "status": "created",
                    }
                )
                existing_hashes.add(stored.sha256.lower())
                drawing_count += 1

        for customer_id in preview.customer_ids:
            customer_items = [
                item for item in preview.product_items
                if item["customer_id"] == customer_id
            ]
            audit_master_change(
                db,
                user=user,
                action="BATCH_IMPORT",
                resource="MixedSampleImport",
                resource_id=customer_id,
                details={
                    "customer_id": customer_id,
                    "source_xlsx_sha256": preview.source_sha256,
                    "reference_xlsx_sha256": preview.reference_sha256,
                    "source_files": list(preview.source_files),
                    "summary": {
                        "products": len(customer_items),
                        "created": sum(item["action"] == "新增" for item in customer_items),
                        "updated": sum(item["action"] == "更新" for item in customer_items),
                        "drawing_only": sum(item["action"] == "补图" for item in customer_items),
                    },
                    "drawing_files": [
                        item for item in drawing_evidence
                        if item["customer_id"] == customer_id
                    ],
                },
            )
        db.commit()
    except ProductImportWorkbookError as error:
        db.rollback()
        for image_path, thumbnail_path in saved_paths:
            remove_drawing_files(image_path, thumbnail_path)
        raise _workbook_error(error) from error
    except HTTPException:
        db.rollback()
        for image_path, thumbnail_path in saved_paths:
            remove_drawing_files(image_path, thumbnail_path)
        raise
    except IntegrityError as error:
        db.rollback()
        for image_path, thumbnail_path in saved_paths:
            remove_drawing_files(image_path, thumbnail_path)
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MIXED_SAMPLE_IMPORT_CONFLICT",
                "message": "混合样品导入时出现编码冲突，三家客户均未写入，请重新预检",
            },
        ) from error
    except Exception:
        db.rollback()
        for image_path, thumbnail_path in saved_paths:
            remove_drawing_files(image_path, thumbnail_path)
        raise

    for token in preview.drawing_tokens.values():
        try:
            discard_temporary_token(token, owner_id=preview.owner_id)
        except Exception:
            pass
    consume_mixed_preview(payload.preview_token, keep_files=True)
    return {
        "ok": True,
        "summary": {
            **preview.summary,
            "drawings": drawing_count,
            "skipped_drawings": skipped_drawing_count,
        },
        "message": (
            "YL、YKE、KEW 混合样品已按客户分组并整批录入"
            + (f"；已跳过 {skipped_drawing_count} 张重复照片" if skipped_drawing_count else "")
        ),
    }
