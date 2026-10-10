from __future__ import annotations

from hashlib import sha256
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
    _changed_updates,
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
from app.models.material import Material
from app.models.mold_tool import MoldTool
from app.models.product import Product
from app.models.product_drawing import ProductDrawing
from app.models.user import User
from app.services.master_data_versioning import (
    apply_versioned_update,
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


def _find_exact_material(db: Session, code: str) -> Material | None:
    return db.scalar(
        select(Material).where(func.upper(Material.code) == code.upper()).limit(1)
    )


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
    exclude_id: int | None = None,
) -> Product | None:
    statement = select(Product).where(
        Product.customer_id == customer_id,
        or_(
            Product.product_code == clean_code(product_code),
            Product.customer_material_code == clean_code(customer_material_code),
        ),
    )
    if exclude_id is not None:
        statement = statement.where(Product.id != exclude_id)
    return db.scalar(statement.limit(1))


def _base_product_payload(customer_id: int, row: dict) -> dict:
    return {
        "customer_id": customer_id,
        "product_code": row["product_code"],
        "customer_material_code": row["customer_material_code"],
        "product_name": row["product_name"],
        "material_id": None,
        "mold_tool_id": None,
        "legacy_material_text": None,
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
        "default_cutting_mode": "一开一",
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
    ):
        data[field_name] = imported[field_name]
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
) -> dict:
    product: Product | None = None
    if row["action"] == "更新":
        product = db.get(Product, row["system_id"])
        if product is None:
            raise ValueError("系统ID对应的常用箱不存在")
        if product.customer_id != customer_id:
            raise ValueError("系统ID不属于模板锁定客户")
        if product.version != row["version"]:
            raise ValueError(
                f"版本已变化：模板为 {row['version']}，当前为 {product.version}，请重新下载模板"
            )
        data = _overlay_import_fields(_product_payload_snapshot(product), row)
    else:
        data = _base_product_payload(customer_id, row)

    conflict = _product_conflict(
        db,
        customer_id=customer_id,
        product_code=row["product_code"],
        customer_material_code=row["customer_material_code"],
        exclude_id=product.id if product is not None else None,
    )
    if conflict is not None:
        raise ValueError(
            f"存货编码或客户料号与现有常用箱冲突（系统ID {conflict.id}）"
        )

    material: Material | None = None
    if row["material_code"]:
        material = _find_exact_material(db, row["material_code"])
        if material is None:
            raise ValueError(f"材质代码不存在：{row['material_code']}")
        if not material.is_active:
            raise ValueError(f"材质代码已停用：{row['material_code']}")
        data["material_id"] = material.id
    else:
        data["material_id"] = None

    mold: MoldTool | None = None
    mold_code = row["mold_code"]
    if mold_code:
        mold = _find_exact_mold(db, mold_code)
        if mold is not None and not mold.is_active:
            raise ValueError(f"模具已停用：{mold_code}")
        if mold is None and mold_code not in new_mold_codes:
            raise ValueError(
                f"模具不存在：{mold_code}；如需新增，请在“模具档案”页填写新增资料"
            )
        data["mold_tool_id"] = mold.id if mold is not None else None
    else:
        data["mold_tool_id"] = None

    payload = ProductPayload.model_validate(data)
    if row["mold_code"] in new_mold_codes:
        # The new mold receives a real ID during apply.  Use a positive
        # placeholder so the same die-cut binding rule is checked at preview.
        payload.mold_tool_id = 1
    _normalize_product_mold_binding(payload)
    if mold_code and payload.mold_tool_id is None:
        raise ValueError("填写模具编号时，生产工艺必须包含“模切”")
    if row["mold_code"] not in new_mold_codes:
        _validate_references(
            db,
            customer_id=customer_id,
            material_id=payload.material_id,
            mold_tool_id=payload.mold_tool_id,
        )
    _validate_product_material_flute(db, payload)
    if product is None:
        _validate_product_crease_widths(payload)
    else:
        _validate_changed_product_crease_widths(payload, product)

    payload_data = payload.model_dump()
    if row["mold_code"] in new_mold_codes:
        payload_data["mold_tool_id"] = None
    missing_fields = _completion_missing_fields(payload, material=material)
    return {
        "row_number": row["row_number"],
        "action": row["action"],
        "system_id": product.id if product is not None else None,
        "expected_version": product.version if product is not None else None,
        "sample_id": row["sample_id"],
        "payload": payload_data,
        "material_id": material.id if material is not None else None,
        "material_version": material.version if material is not None else None,
        "mold_code": mold_code,
        "existing_mold_id": mold.id if mold is not None else None,
        "drawing_filenames": row["drawing_filenames"],
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


@router.post("/import/preview")
async def preview_product_import(
    customer_id: int = Form(...),
    file: UploadFile = File(...),
    drawings: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    require_customer_access(customer_id, current_user=user, db=db)
    if db.get(Customer, customer_id) is None:
        raise HTTPException(status_code=404, detail="客户不存在")
    try:
        workbook_upload = await read_validated_upload(file, EXCEL_POLICY)
        (
            workbook_customer_id,
            product_rows,
            mold_rows,
            errors,
            embedded_drawings,
        ) = read_product_import_workbook(workbook_upload.content)
    except (UploadValidationError, ProductImportWorkbookError) as error:
        if isinstance(error, ProductImportWorkbookError):
            raise _workbook_error(error) from error
        raise HTTPException(status_code=400, detail=str(error)) from error

    if workbook_customer_id != customer_id:
        errors.append(
            {
                "sheet": "导入信息",
                "row_number": 2,
                "message": "Excel 锁定客户与页面当前客户不一致，请重新下载模板",
            }
        )

    mold_items: list[dict] = []
    new_mold_codes = {row["mold_code"] for row in mold_rows}
    for row in mold_rows:
        existing = _find_exact_mold(db, row["mold_code"])
        if existing is not None:
            _append_error(
                errors,
                sheet=MOLD_SHEET,
                row_number=row["row_number"],
                message=f"模具编号已存在：{row['mold_code']}，请改为引用已有模具",
            )
        else:
            mold_items.append(row)

    product_items: list[dict] = []
    seen_ids: set[int] = set()
    seen_codes: set[str] = set()
    seen_customer_codes: set[str] = set()
    for row in product_rows:
        try:
            if row["system_id"] is not None:
                if row["system_id"] in seen_ids:
                    raise ValueError("同一系统ID在本批次重复")
                seen_ids.add(row["system_id"])
            normalized_product_code = clean_code(row["product_code"])
            normalized_customer_code = clean_code(row["customer_material_code"])
            if normalized_product_code in seen_codes:
                raise ValueError("存货编码在本批次重复")
            if normalized_customer_code in seen_customer_codes:
                raise ValueError("客户料号在本批次重复")
            seen_codes.add(normalized_product_code)
            seen_customer_codes.add(normalized_customer_code)
            product_items.append(
                _validated_plan(
                    db,
                    customer_id=customer_id,
                    row=row,
                    new_mold_codes=new_mold_codes,
                )
            )
        except (ValueError, ValidationError, HTTPException) as error:
            _append_error(
                errors,
                sheet=PRODUCT_SHEET,
                row_number=row["row_number"],
                message=_error_message(error),
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
        images = embedded_drawings.get(row_number, ())
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
            embedded_by_name[filename.casefold()] = image.content

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
        "create_products": sum(item["action"] == "新增" for item in product_items),
        "update_products": sum(item["action"] == "更新" for item in product_items),
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
        source_sha256=workbook_upload.sha256,
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
        if item["action"] == "更新":
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
                or not material.is_active
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


def _create_product(
    db: Session,
    *,
    payload: ProductPayload,
    user: User,
) -> Product:
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
) -> Product:
    _normalize_product_mold_binding(payload)
    _validate_references(
        db,
        customer_id=payload.customer_id,
        material_id=payload.material_id,
        mold_tool_id=payload.mold_tool_id,
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
    changed = _changed_updates(product, updates)
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
        drawing_evidence: list[dict] = []
        for item in preview.product_items:
            product = products_by_row[item["row_number"]]
            for filename in item["drawing_filenames"]:
                token = preview.drawing_tokens[filename]
                stored = temporary_token_file(token, owner_id=preview.owner_id)
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
                    }
                )
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
                "summary": {**preview.summary, "drawings": drawing_count},
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
        },
        "message": "常用箱、模具和图纸已按整批事务导入",
    }
