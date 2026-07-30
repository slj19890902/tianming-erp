from __future__ import annotations

from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO
import re
from typing import Any, Iterable

from fastapi import UploadFile
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.time_contract import beijing_now_naive
from app.models.customer import Customer
from app.models.material import Material
from app.models.mold_tool import MoldTool
from app.models.product import Product
from app.models.user import User
from app.services.flute_mapping import (
    normalize_flute_type,
    validate_flute_for_write,
)
from app.services.inventory_onboarding_uploads import (
    _preflight_xlsx_container,
)
from app.services.master_data_versioning import (
    canonical_json,
    serialize_versioned_entity,
)
from app.services.secure_uploads import (
    DRAWING_POLICY,
    EXCEL_POLICY,
    UploadValidationError,
    ValidatedUpload,
    read_validated_upload,
    validate_upload_bytes,
)
from app.services.supplier_master import SupplierLookupError, resolve_supplier


TEMPLATE_VERSION = "P1-22-v2"
INSTRUCTIONS_SHEET = "使用说明"
PRODUCT_SHEET = "样品录入"
MOLD_SHEET = "模具档案"
INFO_SHEET = "导入信息"
EXPECTED_SHEETS = (
    INSTRUCTIONS_SHEET,
    PRODUCT_SHEET,
    MOLD_SHEET,
    INFO_SHEET,
)
PRODUCT_HEADERS = (
    "样品号",
    "手写清单型号",
    "尺寸(mm)",
    "楞型",
    "成型方式",
    "是否印刷",
    "印刷颜色",
    "结合方式",
    "是否二次粘合",
    "图纸文件名",
    "已知模具编号",
    "备注",
    "操作",
    "系统ID",
    "当前版本",
    "ERP客户料号",
    "产品名称",
    "材质代码",
    "层数",
    "默认含税单价",
    "报料长(mm)",
    "报料宽(mm)",
    "启用状态",
)
MOLD_HEADERS = ("模具编号", "模具名称", "固定位置", "备注")
INFO_HEADERS = ("项目", "值")
MAX_PRODUCT_ROWS = 1_000
MAX_MOLD_ROWS = 500
MAX_DRAWING_FILES = 100
MAX_DRAWING_TOTAL_BYTES = 100 * 1024 * 1024
BLANK_SAMPLE_ROWS = 22
KNOWN_PROCESSES = frozenset({"印刷", "模切", "开槽", "打钉", "粘合", "二次粘合"})
MAPPING_PENDING_MESSAGE = (
    "手写型号与客户正式 ERP 存货编码的映射尚未确认，"
    "当前只允许整理和预览，禁止正式导入"
)
MAX_EMBEDDED_IMAGE_PIXELS = 40_000_000


class ProductWorkbookError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _cell_text(value: object) -> str:
    return str(value or "").strip()


def _process_tokens(value: str | None) -> list[str]:
    return [
        item.strip()
        for item in re.split(r"[,，、]", str(value or ""))
        if item.strip()
    ]


def _forming_from_product(product: Product) -> str:
    tokens = set(_process_tokens(product.production_process))
    if "模切" in tokens or product.box_category == "die_cut":
        return "模切"
    if "开槽" in tokens:
        return "开槽"
    return "无需"


def _printing_from_product(product: Product) -> str:
    tokens = set(_process_tokens(product.production_process))
    content = str(product.print_content or "").strip()
    return "是" if "印刷" in tokens or (content and content != "无印刷") else "否"


def _joining_from_product(product: Product) -> str:
    tokens = set(_process_tokens(product.production_process))
    if "打钉" in tokens:
        return "打钉"
    if "粘合" in tokens:
        return "粘合"
    return "无需结合"


def _dimensions_text(product: Product) -> str:
    values = (product.length_mm, product.width_mm, product.height_mm)
    if not all(value is not None for value in values):
        return ""
    rendered = []
    for value in values:
        decimal = Decimal(str(value))
        rendered.append(
            str(int(decimal)) if decimal == decimal.to_integral_value() else str(decimal)
        )
    return "×".join(rendered)


def _drawing_original_names(product: Product) -> str:
    # Existing drawings stay versioned and are not silently copied into a new
    # import batch.  The column is deliberately blank on export so a user must
    # choose the real source files again before adding drawing versions.
    del product
    return ""


def _style_header(sheet, *, start: int, end: int, color: str) -> None:
    fill = PatternFill("solid", fgColor=color)
    for column in range(start, end + 1):
        cell = sheet.cell(row=1, column=column)
        cell.font = Font(bold=True)
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _add_list_validation(sheet, column: str, values: Iterable[str]) -> None:
    validation = DataValidation(
        type="list",
        formula1='"' + ",".join(values) + '"',
        allow_blank=True,
    )
    validation.error = "请从下拉列表选择"
    validation.errorTitle = "填写不符合模板规则"
    validation.prompt = "请使用模板下拉选项"
    validation.promptTitle = "天明 ERP"
    sheet.add_data_validation(validation)
    validation.add(f"{column}2:{column}{MAX_PRODUCT_ROWS + 1}")


def build_product_workbook(
    db: Session,
    *,
    customer: Customer,
) -> bytes:
    products = list(
        db.scalars(
            select(Product)
            .options(
                selectinload(Product.material),
                selectinload(Product.mold_tool),
                selectinload(Product.drawings),
            )
            .where(
                Product.customer_id == customer.id,
                Product.deleted_at.is_(None),
                Product.purged_at.is_(None),
            )
            .order_by(Product.product_code, Product.id)
        ).all()
    )

    workbook = Workbook()
    instructions = workbook.active
    instructions.title = INSTRUCTIONS_SHEET
    instructions.append(("天明 ERP 样品常用箱 XLSX 整理与预览",))
    instructions.append(("1", "本模板只能回导下载时锁定的客户，不能跨客户使用。"))
    instructions.append(("2", "现场只填写样品录入 A:L；手写型号目前只用于样品核对，不会写成 ERP 存货编码。"))
    instructions.append(("3", "默认把每款两张压缩后的 JPG/PNG 图片从对应行 J 列开始横向内嵌，只上传这一份 Excel。"))
    instructions.append(("4", "每个新增行必须恰好两张图片；少图、多图、错行、错列或损坏都会阻断。"))
    instructions.append(("5", "办公室可在同一行 M:W 补资料；材质、价格、报料缺项会显示为待完善。"))
    instructions.append(("6", "模切必须精确核对启用模具；新模具需在模具档案填写编号、名称和固定位置。"))
    instructions.append(("7", MAPPING_PENDING_MESSAGE + "。"))
    instructions.append(("8", "旧模板可在 J 列填两个文件名，并从页面“旧模板兼容”同时选择图纸原文件。"))
    instructions.column_dimensions["A"].width = 8
    instructions.column_dimensions["B"].width = 100
    instructions["A1"].font = Font(bold=True, size=15)
    instructions.merge_cells("A1:B1")

    sheet = workbook.create_sheet(PRODUCT_SHEET)
    sheet.append(PRODUCT_HEADERS)
    _style_header(sheet, start=1, end=12, color="DCEAF7")
    _style_header(sheet, start=13, end=23, color="E2F0D9")
    widths = (
        11, 18, 17, 9, 11, 10, 12, 12, 13, 25, 17, 25,
        10, 10, 10, 18, 24, 16, 9, 15, 14, 14, 10,
    )
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    blank_sample_rows = max(
        0,
        min(BLANK_SAMPLE_ROWS, MAX_PRODUCT_ROWS - len(products)),
    )
    last_template_row = max(2, len(products) + blank_sample_rows + 1)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:W{last_template_row}"
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.orientation = "landscape"
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.print_area = f"A1:I{last_template_row}"
    sheet.print_title_rows = "1:1"

    for product in products:
        tokens = set(_process_tokens(product.production_process))
        sheet.append(
            (
                "",
                product.product_code,
                _dimensions_text(product),
                product.flute_type or "",
                _forming_from_product(product),
                _printing_from_product(product),
                product.printing_colors or (
                    "黑色" if _printing_from_product(product) == "是" else ""
                ),
                _joining_from_product(product),
                "是" if "二次粘合" in tokens else "否",
                _drawing_original_names(product),
                product.mold_tool.mold_code if product.mold_tool else "",
                product.remark or "",
                "不变",
                product.id,
                product.version,
                product.customer_material_code,
                product.product_name,
                product.material.code if product.material else "",
                product.layer_count or "",
                product.sale_unit_price,
                product.report_length_mm,
                product.report_width_mm,
                "是" if product.is_active else "否",
            )
        )

    for index in range(1, blank_sample_rows + 1):
        sheet.append(
            (
                f"YP{index:03d}",
                "",
                "",
                "",
                "",
                "",
                "黑色",
                "",
                "否",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "是",
            )
        )

    _add_list_validation(sheet, "D", ("A", "B", "E", "AB", "BE", "AAA", "ABC"))
    _add_list_validation(sheet, "E", ("模切", "开槽", "无需"))
    _add_list_validation(sheet, "F", ("是", "否"))
    _add_list_validation(sheet, "G", ("黑色", "红色", "其他"))
    _add_list_validation(sheet, "H", ("打钉", "粘合", "无需结合"))
    _add_list_validation(sheet, "I", ("是", "否"))
    _add_list_validation(sheet, "M", ("新增", "更新", "不变"))
    _add_list_validation(sheet, "S", ("3", "5", "7"))
    _add_list_validation(sheet, "W", ("是", "否"))

    mold_sheet = workbook.create_sheet(MOLD_SHEET)
    mold_sheet.append(MOLD_HEADERS)
    _style_header(mold_sheet, start=1, end=4, color="FCE4D6")
    for index, width in enumerate((18, 28, 30, 35), start=1):
        mold_sheet.column_dimensions[get_column_letter(index)].width = width
    mold_sheet.freeze_panes = "A2"
    mold_sheet.auto_filter.ref = "A1:D2"

    info = workbook.create_sheet(INFO_SHEET)
    info.append(INFO_HEADERS)
    info.append(("模板版本", TEMPLATE_VERSION))
    info.append(("客户ID", customer.id))
    info.append(("客户名称", customer.name))
    info.append(("生成时间", beijing_now_naive().isoformat(timespec="seconds")))
    info.sheet_state = "hidden"

    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def _positive_int(value: object, label: str, *, optional: bool = False) -> int | None:
    text = _cell_text(value)
    if optional and not text:
        return None
    try:
        decimal = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label}必须是正整数") from None
    if decimal <= 0 or decimal != decimal.to_integral_value():
        raise ValueError(f"{label}必须是正整数")
    return int(decimal)


def _optional_decimal(value: object, label: str) -> Decimal | None:
    text = _cell_text(value)
    if not text:
        return None
    try:
        result = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label}必须是有效数字") from None
    if not result.is_finite() or result < 0:
        raise ValueError(f"{label}必须是大于等于0的有效数字")
    return result


def _yes_no(value: object, label: str, *, default: bool | None = None) -> bool:
    text = _cell_text(value)
    if not text and default is not None:
        return default
    if text == "是":
        return True
    if text == "否":
        return False
    raise ValueError(f"{label}只能填写是或否")


def _dimensions(value: object) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    text = _cell_text(value)
    if not text:
        return None, None, None
    normalized = (
        text.lower()
        .replace("毫米", "")
        .replace("mm", "")
        .replace("*", "×")
        .replace("x", "×")
        .replace("X", "×")
    )
    parts = [part.strip() for part in normalized.split("×")]
    if len(parts) != 3:
        raise ValueError("尺寸必须按 长×宽×高 填写，例如 380×260×220")
    values: list[Decimal] = []
    for part in parts:
        try:
            number = Decimal(part)
        except (InvalidOperation, ValueError):
            raise ValueError("尺寸必须是三个有效数字") from None
        if not number.is_finite() or number <= 0:
            raise ValueError("尺寸必须是三个大于0的有效数字")
        values.append(number)
    return values[0], values[1], values[2]


def _drawing_names(value: object) -> tuple[str, ...]:
    raw = _cell_text(value)
    if not raw:
        return ()
    names = tuple(
        item.strip()
        for item in re.split(r"[;；,，\n\r]+", raw)
        if item.strip()
    )
    folded = [name.casefold() for name in names]
    if len(folded) != len(set(folded)):
        raise ValueError("同一行图纸文件名重复")
    return names


def _append_error(
    errors: list[dict[str, Any]],
    *,
    sheet: str,
    row_number: int,
    message: str,
) -> None:
    if len(errors) < 300:
        errors.append(
            {"sheet": sheet, "row_number": row_number, "message": message}
        )


def _strict_headers(sheet, expected: tuple[str, ...]) -> None:
    actual = tuple(_cell_text(cell.value) for cell in sheet[1])
    if actual != expected:
        raise ProductWorkbookError(
            "PRODUCT_WORKBOOK_HEADERS_INVALID",
            f"工作表“{sheet.title}”表头不匹配，请重新下载 ERP 模板",
        )


def _workbook_rows(sheet, *, max_columns: int) -> Iterable[tuple[int, list[object]]]:
    max_row = int(sheet.max_row or 0)
    for row_number, cells in enumerate(
        sheet.iter_rows(min_row=2, max_row=max_row, max_col=max_columns),
        start=2,
    ):
        values: list[object] = []
        for column_number, cell in enumerate(cells, start=1):
            if cell.data_type == "f":
                raise ProductWorkbookError(
                    "PRODUCT_WORKBOOK_FORMULA_FORBIDDEN",
                    (
                        f"工作表“{sheet.title}”第 {row_number} 行第 "
                        f"{column_number} 列包含公式"
                    ),
                )
            if cell.data_type == "e":
                raise ProductWorkbookError(
                    "PRODUCT_WORKBOOK_CELL_ERROR",
                    f"工作表“{sheet.title}”第 {row_number} 行包含错误值",
                )
            values.append(cell.value)
        yield row_number, values


def _validated_embedded_image(
    image: object,
    *,
    row_number: int,
    image_number: int,
    sample_no: str,
) -> ValidatedUpload:
    image_format = str(getattr(image, "format", "") or "").lower()
    if image_format == "jpg":
        image_format = "jpeg"
    type_by_format = {
        "jpeg": (".jpg", "image/jpeg"),
        "png": (".png", "image/png"),
    }
    if image_format not in type_by_format:
        raise ProductWorkbookError(
            "PRODUCT_WORKBOOK_EMBEDDED_IMAGE_INVALID",
            f"样品录入第 {row_number} 行内嵌图片只允许 JPG、JPEG 或 PNG",
        )
    try:
        content = image._data()
    except Exception as error:
        raise ProductWorkbookError(
            "PRODUCT_WORKBOOK_EMBEDDED_IMAGE_INVALID",
            f"样品录入第 {row_number} 行第 {image_number} 张图片无法读取",
        ) from error
    extension, content_type = type_by_format[image_format]
    filename = (
        f"{sample_no or '未编号'}_第{row_number}行_"
        f"内嵌图{image_number}{extension}"
    )
    try:
        upload = validate_upload_bytes(
            content=content,
            filename=filename,
            content_type=content_type,
            policy=DRAWING_POLICY,
        )
        with Image.open(BytesIO(content)) as decoded:
            width, height = decoded.size
            if (
                width <= 0
                or height <= 0
                or width > 20_000
                or height > 20_000
                or width * height > MAX_EMBEDDED_IMAGE_PIXELS
            ):
                raise UploadValidationError("内嵌图片像素尺寸过大")
            decoded.verify()
        with Image.open(BytesIO(content)) as decoded:
            decoded.load()
    except UploadValidationError as error:
        raise ProductWorkbookError(
            "PRODUCT_WORKBOOK_EMBEDDED_IMAGE_INVALID",
            f"样品录入第 {row_number} 行第 {image_number} 张图片：{error}",
        ) from error
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
    ) as error:
        raise ProductWorkbookError(
            "PRODUCT_WORKBOOK_EMBEDDED_IMAGE_INVALID",
            f"样品录入第 {row_number} 行第 {image_number} 张图片损坏或不安全",
        ) from error
    return upload


def _extract_embedded_images(workbook) -> dict[int, tuple[ValidatedUpload, ...]]:
    for worksheet in workbook.worksheets:
        if worksheet.title != PRODUCT_SHEET and getattr(worksheet, "_images", []):
            raise ProductWorkbookError(
                "PRODUCT_WORKBOOK_EMBEDDED_IMAGE_MISPLACED",
                f"图片只能放在“{PRODUCT_SHEET}”工作表对应产品行",
            )

    sheet = workbook[PRODUCT_SHEET]
    ordered: dict[int, list[tuple[int, int, int, object]]] = {}
    for original_index, image in enumerate(getattr(sheet, "_images", []), start=1):
        marker = getattr(getattr(image, "anchor", None), "_from", None)
        if marker is None:
            raise ProductWorkbookError(
                "PRODUCT_WORKBOOK_EMBEDDED_IMAGE_MISPLACED",
                "工作簿包含无法确定产品行的图片",
            )
        row_number = int(marker.row) + 1
        column_number = int(marker.col) + 1
        column_offset = int(getattr(marker, "colOff", 0) or 0)
        if row_number < 2 or column_number != 10:
            raise ProductWorkbookError(
                "PRODUCT_WORKBOOK_EMBEDDED_IMAGE_MISPLACED",
                (
                    f"图片必须从“{PRODUCT_SHEET}”对应数据行的 J 列图片区域开始；"
                    f"发现第 {row_number} 行第 {column_number} 列"
                ),
            )
        ordered.setdefault(row_number, []).append(
            (column_number, column_offset, original_index, image)
        )

    result: dict[int, tuple[ValidatedUpload, ...]] = {}
    total_size = 0
    total_files = 0
    for row_number, row_images in sorted(ordered.items()):
        row_images.sort(key=lambda item: item[:3])
        sample_no = _cell_text(sheet.cell(row=row_number, column=1).value)
        uploads = []
        for image_number, (_, _, _, image) in enumerate(row_images, start=1):
            upload = _validated_embedded_image(
                image,
                row_number=row_number,
                image_number=image_number,
                sample_no=sample_no,
            )
            uploads.append(upload)
            total_size += upload.size
            total_files += 1
        result[row_number] = tuple(uploads)
    if total_files > MAX_DRAWING_FILES:
        raise ProductWorkbookError(
            "PRODUCT_WORKBOOK_DRAWINGS_EXCEEDED",
            f"单批最多允许 {MAX_DRAWING_FILES} 张内嵌图片",
        )
    if total_size > MAX_DRAWING_TOTAL_BYTES:
        raise ProductWorkbookError(
            "PRODUCT_WORKBOOK_DRAWINGS_EXCEEDED",
            "单批内嵌图片总大小不能超过100MB",
        )
    return result


def _read_workbook(
    content: bytes,
    *,
    expected_customer_id: int,
    expected_customer_name: str,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[int, tuple[ValidatedUpload, ...]],
]:
    try:
        _preflight_xlsx_container(content, allow_worksheet_images=True)
    except UploadValidationError as error:
        raise ProductWorkbookError(
            "PRODUCT_WORKBOOK_CONTAINER_INVALID",
            str(error),
        ) from error
    try:
        workbook = load_workbook(
            BytesIO(content),
            read_only=False,
            data_only=False,
            keep_links=False,
        )
    except Exception as error:
        raise ProductWorkbookError(
            "PRODUCT_WORKBOOK_INVALID",
            "Excel 工作簿无法安全读取",
        ) from error
    try:
        if tuple(workbook.sheetnames) != EXPECTED_SHEETS:
            raise ProductWorkbookError(
                "PRODUCT_WORKBOOK_SHEETS_INVALID",
                "Excel 工作表结构不匹配，请重新下载 ERP 模板",
            )
        product_sheet = workbook[PRODUCT_SHEET]
        mold_sheet = workbook[MOLD_SHEET]
        info_sheet = workbook[INFO_SHEET]
        embedded_images = _extract_embedded_images(workbook)
        if int(product_sheet.max_row or 0) > MAX_PRODUCT_ROWS + 1:
            raise ProductWorkbookError(
                "PRODUCT_WORKBOOK_ROWS_EXCEEDED",
                f"样品录入最多允许 {MAX_PRODUCT_ROWS} 行",
            )
        if int(mold_sheet.max_row or 0) > MAX_MOLD_ROWS + 1:
            raise ProductWorkbookError(
                "PRODUCT_WORKBOOK_MOLD_ROWS_EXCEEDED",
                f"模具档案最多允许 {MAX_MOLD_ROWS} 行",
            )
        _strict_headers(product_sheet, PRODUCT_HEADERS)
        _strict_headers(mold_sheet, MOLD_HEADERS)
        _strict_headers(info_sheet, INFO_HEADERS)
        info_values = {
            _cell_text(row[0].value): _cell_text(row[1].value)
            for row in info_sheet.iter_rows(min_row=2, max_col=2)
            if _cell_text(row[0].value)
        }
        if info_values.get("模板版本") != TEMPLATE_VERSION:
            raise ProductWorkbookError(
                "PRODUCT_WORKBOOK_VERSION_INVALID",
                "模板版本不匹配，请重新下载 ERP 模板",
            )
        if info_values.get("客户ID") != str(expected_customer_id):
            raise ProductWorkbookError(
                "PRODUCT_WORKBOOK_CUSTOMER_MISMATCH",
                "Excel 锁定客户与当前页面客户不一致，禁止跨客户导入",
                status_code=409,
            )
        if info_values.get("客户名称") != expected_customer_name:
            raise ProductWorkbookError(
                "PRODUCT_WORKBOOK_CUSTOMER_STALE",
                "客户名称已变化，请重新下载模板",
                status_code=409,
            )

        errors: list[dict[str, Any]] = []
        raw_products: list[dict[str, Any]] = []
        for row_number, values in _workbook_rows(
            product_sheet,
            max_columns=len(PRODUCT_HEADERS),
        ):
            model_code = _cell_text(values[1])
            operation = _cell_text(values[12])
            has_user_data = any(
                _cell_text(values[index])
                for index in (
                    1, 2, 3, 4, 5, 7, 9, 10, 11, 12, 13, 14, 15, 16, 17,
                    18, 19, 20, 21,
                )
            )
            # Downloaded blank YP rows contain only sample/default cells.
            if not model_code and not operation and not has_user_data:
                continue
            if model_code and not operation:
                _append_error(
                    errors,
                    sheet=PRODUCT_SHEET,
                    row_number=row_number,
                    message="请在操作列选择新增、更新或不变",
                )
                continue
            try:
                if operation not in {"新增", "更新", "不变"}:
                    raise ValueError("操作只能选择新增、更新或不变")
                length, width, height = _dimensions(values[2])
                flute = normalize_flute_type(_cell_text(values[3]) or None)
                forming = _cell_text(values[4])
                if forming not in {"模切", "开槽", "无需"}:
                    raise ValueError("成型方式只能选择模切、开槽或无需")
                printed = _yes_no(values[5], "是否印刷")
                print_color = _cell_text(values[6])
                if printed and not print_color:
                    print_color = "黑色"
                if not printed and print_color not in {"", "无"}:
                    raise ValueError("无印刷时不要填写印刷颜色")
                joining = _cell_text(values[7])
                if joining not in {"打钉", "粘合", "无需结合"}:
                    raise ValueError("结合方式只能选择打钉、粘合或无需结合")
                secondary = _yes_no(values[8], "是否二次粘合", default=False)
                if secondary and not (forming == "模切" and joining == "粘合"):
                    raise ValueError("二次粘合只允许用于模切内盒且结合方式为粘合")
                system_id = _positive_int(
                    values[13], "系统ID", optional=True
                )
                current_version = _positive_int(
                    values[14], "当前版本", optional=True
                )
                layer_count = _positive_int(
                    values[18], "层数", optional=True
                )
                if layer_count not in {None, 3, 5, 7}:
                    raise ValueError("层数只能填写3、5或7")
                report_length = _positive_int(
                    values[20], "报料长", optional=True
                )
                report_width = _positive_int(
                    values[21], "报料宽", optional=True
                )
                raw_products.append(
                    {
                        "row_number": row_number,
                        "sample_no": _cell_text(values[0]) or None,
                        "product_code": model_code,
                        "length_mm": length,
                        "width_mm": width,
                        "height_mm": height,
                        "flute_type": flute,
                        "forming": forming,
                        "printed": printed,
                        "print_color": print_color or None,
                        "joining": joining,
                        "secondary_gluing": secondary,
                        "drawing_names": _drawing_names(values[9]),
                        "mold_code": _cell_text(values[10]) or None,
                        "remark": _cell_text(values[11]) or None,
                        "action": operation,
                        "system_id": system_id,
                        "file_version": current_version,
                        "customer_material_code": _cell_text(values[15]),
                        "product_name": _cell_text(values[16]),
                        "material_code": _cell_text(values[17]).upper(),
                        "layer_count": layer_count,
                        "sale_unit_price": _optional_decimal(
                            values[19], "默认含税单价"
                        ),
                        "report_length_mm": report_length,
                        "report_width_mm": report_width,
                        "is_active": _yes_no(
                            values[22], "启用状态", default=True
                        ),
                    }
                )
            except ValueError as error:
                _append_error(
                    errors,
                    sheet=PRODUCT_SHEET,
                    row_number=row_number,
                    message=str(error),
                )

        raw_molds: list[dict[str, Any]] = []
        seen_molds: set[str] = set()
        for row_number, values in _workbook_rows(
            mold_sheet,
            max_columns=len(MOLD_HEADERS),
        ):
            if not any(_cell_text(value) for value in values):
                continue
            code = _cell_text(values[0])
            if not code:
                _append_error(
                    errors,
                    sheet=MOLD_SHEET,
                    row_number=row_number,
                    message="模具编号不能为空",
                )
                continue
            folded = code.casefold()
            if folded in seen_molds:
                _append_error(
                    errors,
                    sheet=MOLD_SHEET,
                    row_number=row_number,
                    message=f"模具编号 {code} 在表内重复",
                )
                continue
            seen_molds.add(folded)
            raw_molds.append(
                {
                    "row_number": row_number,
                    "mold_code": code,
                    "mold_name": _cell_text(values[1]),
                    "rack_location": _cell_text(values[2]),
                    "remarks": _cell_text(values[3]) or None,
                }
            )
        parsed_rows = {row["row_number"] for row in raw_products}
        for row_number in sorted(set(embedded_images) - parsed_rows):
            _append_error(
                errors,
                sheet=PRODUCT_SHEET,
                row_number=row_number,
                message="图片所在行没有可识别的产品资料",
            )
        for raw in raw_products:
            uploads = embedded_images.get(raw["row_number"], ())
            if uploads and raw["drawing_names"]:
                _append_error(
                    errors,
                    sheet=PRODUCT_SHEET,
                    row_number=raw["row_number"],
                    message="同一行不能同时使用内嵌图片和外部图纸文件名",
                )
            if uploads:
                raw["drawing_names"] = tuple(
                    upload.original_filename for upload in uploads
                )
                raw["drawing_mode"] = "embedded"
            else:
                raw["drawing_mode"] = (
                    "external" if raw["drawing_names"] else "none"
                )
            drawing_count = len(raw["drawing_names"])
            if len(
                {name.casefold() for name in raw["drawing_names"]}
            ) != drawing_count:
                _append_error(
                    errors,
                    sheet=PRODUCT_SHEET,
                    row_number=raw["row_number"],
                    message="同一产品行的两张图片不能使用重复文件名",
                )
            if raw["action"] == "新增" and drawing_count != 2:
                _append_error(
                    errors,
                    sheet=PRODUCT_SHEET,
                    row_number=raw["row_number"],
                    message="每个新增产品必须恰好对应两张有效图片",
                )
            if raw["action"] == "更新" and drawing_count not in {0, 2}:
                _append_error(
                    errors,
                    sheet=PRODUCT_SHEET,
                    row_number=raw["row_number"],
                    message="更新产品如补图，必须一次提供恰好两张图片",
                )
        return raw_products, raw_molds, errors, embedded_images
    finally:
        workbook.close()


def _mold_snapshot(row: MoldTool) -> dict[str, Any]:
    return {
        "id": row.id,
        "mold_code": row.mold_code,
        "mold_name": row.mold_name,
        "rack_location": row.rack_location,
        "location_version": row.location_version,
        "is_active": row.is_active,
    }


def _fingerprint(value: dict[str, Any]) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _build_process(
    raw: dict[str, Any],
    *,
    existing: Product | None,
) -> str | None:
    selected: list[str] = []
    if raw["printed"]:
        selected.append("印刷")
    if raw["forming"] != "无需":
        selected.append(raw["forming"])
    if raw["joining"] != "无需结合":
        selected.append(raw["joining"])
    if raw["secondary_gluing"]:
        selected.append("二次粘合")
    if existing is not None:
        current = _process_tokens(existing.production_process)
        current_projection = [item for item in current if item in KNOWN_PROCESSES]
        if current_projection == selected:
            return existing.production_process
        selected.extend(item for item in current if item not in KNOWN_PROCESSES)
    return "、".join(dict.fromkeys(selected)) or None


def _resolve_plan(
    db: Session,
    *,
    customer: Customer,
    raw_products: list[dict[str, Any]],
    raw_molds: list[dict[str, Any]],
    uploaded_names: set[str],
    errors: list[dict[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    tuple[dict[str, Any], ...],
    tuple[dict[str, Any], ...],
    dict[str, int],
]:
    products = list(
        db.scalars(
            select(Product)
            .options(
                selectinload(Product.material),
                selectinload(Product.mold_tool),
            )
            .where(
                Product.customer_id == customer.id,
                Product.deleted_at.is_(None),
                Product.purged_at.is_(None),
            )
        ).all()
    )
    products_by_id = {row.id: row for row in products}
    products_by_code = {row.product_code.casefold(): row for row in products}
    products_by_customer_code = {
        row.customer_material_code.casefold(): row for row in products
    }
    materials = list(db.scalars(select(Material)).all())
    materials_by_code = {row.code.upper(): row for row in materials}
    molds = list(db.scalars(select(MoldTool)).all())
    molds_by_code = {row.mold_code.casefold(): row for row in molds}

    mold_rows_by_code = {
        row["mold_code"].casefold(): row for row in raw_molds
    }
    mold_items: list[dict[str, Any]] = []
    mold_item_codes: set[str] = set()
    for raw in raw_molds:
        existing = molds_by_code.get(raw["mold_code"].casefold())
        if existing is not None:
            if not existing.is_active:
                _append_error(
                    errors,
                    sheet=MOLD_SHEET,
                    row_number=raw["row_number"],
                    message=f"模具 {existing.mold_code} 已停用",
                )
                continue
            if (
                raw["mold_name"]
                and raw["mold_name"] != existing.mold_name
            ) or (
                raw["rack_location"]
                and raw["rack_location"] != existing.rack_location
            ):
                _append_error(
                    errors,
                    sheet=MOLD_SHEET,
                    row_number=raw["row_number"],
                    message=(
                        f"模具 {existing.mold_code} 已存在；首期模板禁止改名或移位"
                    ),
                )
                continue
            mold_items.append(
                {
                    "row_number": raw["row_number"],
                    "action": "unchanged",
                    "existing_id": existing.id,
                    "before_fingerprint": _fingerprint(_mold_snapshot(existing)),
                    "updates": {},
                    "mold_code": existing.mold_code,
                }
            )
            mold_item_codes.add(existing.mold_code.casefold())
            continue
        if not raw["mold_name"] or not raw["rack_location"]:
            _append_error(
                errors,
                sheet=MOLD_SHEET,
                row_number=raw["row_number"],
                message="新增模具必须同时填写模具名称和固定位置",
            )
            continue
        mold_items.append(
            {
                "row_number": raw["row_number"],
                "action": "create",
                "existing_id": None,
                "before_fingerprint": None,
                "mold_code": raw["mold_code"],
                "updates": {
                    "mold_code": raw["mold_code"],
                    "mold_name": raw["mold_name"],
                    "rack_location": raw["rack_location"],
                    "remarks": raw["remarks"],
                    "is_active": True,
                },
            }
        )
        mold_item_codes.add(raw["mold_code"].casefold())

    material_references: dict[int, dict[str, Any]] = {}
    supplier_references: dict[int, dict[str, Any]] = {}
    items: list[dict[str, Any]] = []
    seen_product_ids: set[int] = set()
    seen_codes: set[str] = set()
    seen_customer_codes: set[str] = set()
    referenced_drawings: set[str] = set()

    for raw in raw_products:
        row_number = raw["row_number"]
        if raw["action"] == "不变":
            if raw["drawing_names"]:
                _append_error(
                    errors,
                    sheet=PRODUCT_SHEET,
                    row_number=row_number,
                    message="不变行不能新增图纸；请把操作改为更新",
                )
            items.append(
                {
                    "row_number": row_number,
                    "action": "unchanged",
                    "changed": False,
                    "existing_id": raw["system_id"],
                    "expected_version": raw["file_version"],
                    "updates": {},
                    "missing_fields": [],
                    "drawing_names": (),
                    "mold_code": raw["mold_code"],
                }
            )
            continue

        existing: Product | None
        if raw["action"] == "更新":
            if raw["system_id"] is None or raw["file_version"] is None:
                _append_error(
                    errors,
                    sheet=PRODUCT_SHEET,
                    row_number=row_number,
                    message="更新必须填写系统ID和当前版本",
                )
                continue
            existing = products_by_id.get(raw["system_id"])
            if existing is None:
                _append_error(
                    errors,
                    sheet=PRODUCT_SHEET,
                    row_number=row_number,
                    message=f"系统ID {raw['system_id']} 不属于当前客户",
                )
                continue
            if existing.version != raw["file_version"]:
                _append_error(
                    errors,
                    sheet=PRODUCT_SHEET,
                    row_number=row_number,
                    message=(
                        f"产品 {existing.product_code} 当前为 v{existing.version}，"
                        f"模板仍是 v{raw['file_version']}，请重新下载"
                    ),
                )
                continue
            if existing.id in seen_product_ids:
                _append_error(
                    errors,
                    sheet=PRODUCT_SHEET,
                    row_number=row_number,
                    message=f"产品 ID {existing.id} 在模板中重复",
                )
                continue
            seen_product_ids.add(existing.id)
        else:
            existing = None
            if raw["system_id"] is not None or raw["file_version"] is not None:
                _append_error(
                    errors,
                    sheet=PRODUCT_SHEET,
                    row_number=row_number,
                    message="新增行不能填写系统ID或当前版本",
                )
                continue

        if not raw["product_code"]:
            _append_error(
                errors,
                sheet=PRODUCT_SHEET,
                row_number=row_number,
                message="手写清单型号不能为空；当前仅用于样品与合作厂型号核对",
            )
            continue
        if not raw["customer_material_code"]:
            _append_error(
                errors,
                sheet=PRODUCT_SHEET,
                row_number=row_number,
                message="ERP 客户料号不能为空，且不能由手写型号自动代填",
            )
            continue
        if not raw["product_name"]:
            _append_error(
                errors,
                sheet=PRODUCT_SHEET,
                row_number=row_number,
                message="产品名称不能为空",
            )
            continue

        code_key = raw["product_code"].casefold()
        customer_code_key = raw["customer_material_code"].casefold()
        if code_key in seen_codes:
            _append_error(
                errors,
                sheet=PRODUCT_SHEET,
                row_number=row_number,
                message=f"手写清单型号 {raw['product_code']} 在模板内重复",
            )
            continue
        if customer_code_key in seen_customer_codes:
            _append_error(
                errors,
                sheet=PRODUCT_SHEET,
                row_number=row_number,
                message=f"客户料号 {raw['customer_material_code']} 在模板内重复",
            )
            continue
        seen_codes.add(code_key)
        seen_customer_codes.add(customer_code_key)
        code_conflict = products_by_code.get(code_key)
        customer_code_conflict = products_by_customer_code.get(customer_code_key)
        if code_conflict is not None and (
            existing is None or code_conflict.id != existing.id
        ):
            _append_error(
                errors,
                sheet=PRODUCT_SHEET,
                row_number=row_number,
                message=(
                    f"手写型号 {raw['product_code']} 与已有 ERP 存货编码相同，"
                    "仅作为人工核对提示"
                ),
            )
        if customer_code_conflict is not None and (
            existing is None or customer_code_conflict.id != existing.id
        ):
            _append_error(
                errors,
                sheet=PRODUCT_SHEET,
                row_number=row_number,
                message=f"客户料号 {raw['customer_material_code']} 已被当前客户使用",
            )
            continue

        material: Material | None = None
        if raw["material_code"]:
            material = materials_by_code.get(raw["material_code"])
            if material is None or not material.is_active:
                _append_error(
                    errors,
                    sheet=PRODUCT_SHEET,
                    row_number=row_number,
                    message=f"材质代码 {raw['material_code']} 不存在或已停用",
                )
                continue
            try:
                supplier = resolve_supplier(
                    db,
                    material.supplier_name,
                    require_active=True,
                )
            except SupplierLookupError as error:
                _append_error(
                    errors,
                    sheet=PRODUCT_SHEET,
                    row_number=row_number,
                    message=error.message,
                )
                continue
            material_references[material.id] = {
                "id": material.id,
                "code": material.code,
                "version": material.version,
                "is_active": material.is_active,
            }
            supplier_references[supplier.id] = {
                "id": supplier.id,
                "standard_name": supplier.standard_name,
                "version": supplier.version,
            }
        elif existing is not None:
            material = existing.material

        effective_layer = (
            material.layer_count if material is not None else raw["layer_count"]
        )
        if (
            material is not None
            and raw["layer_count"] is not None
            and raw["layer_count"] != material.layer_count
        ):
            _append_error(
                errors,
                sheet=PRODUCT_SHEET,
                row_number=row_number,
                message="模板层数与所选材质真实层数不一致",
            )
            continue
        flute_error = validate_flute_for_write(
            raw["flute_type"],
            effective_layer,
        )
        if flute_error:
            _append_error(
                errors,
                sheet=PRODUCT_SHEET,
                row_number=row_number,
                message=flute_error,
            )
            continue

        mold_code = raw["mold_code"]
        if raw["forming"] == "模切":
            if not mold_code:
                _append_error(
                    errors,
                    sheet=PRODUCT_SHEET,
                    row_number=row_number,
                    message="模切产品必须填写已知模具编号",
                )
                continue
            mold = molds_by_code.get(mold_code.casefold())
            planned_mold = mold_rows_by_code.get(mold_code.casefold())
            if mold is not None and not mold.is_active:
                _append_error(
                    errors,
                    sheet=PRODUCT_SHEET,
                    row_number=row_number,
                    message=f"模具 {mold_code} 已停用",
                )
                continue
            if mold is not None and mold.mold_code.casefold() not in mold_item_codes:
                mold_items.append(
                    {
                        "row_number": row_number,
                        "action": "unchanged",
                        "existing_id": mold.id,
                        "before_fingerprint": _fingerprint(_mold_snapshot(mold)),
                        "updates": {},
                        "mold_code": mold.mold_code,
                    }
                )
                mold_item_codes.add(mold.mold_code.casefold())
            if mold is None and planned_mold is None:
                _append_error(
                    errors,
                    sheet=PRODUCT_SHEET,
                    row_number=row_number,
                    message=(
                        f"模具 {mold_code} 不存在；请在模具档案填写名称和固定位置"
                    ),
                )
                continue
        elif mold_code:
            _append_error(
                errors,
                sheet=PRODUCT_SHEET,
                row_number=row_number,
                message="非模切产品不能绑定模具",
            )
            continue

        for name in raw["drawing_names"]:
            referenced_drawings.add(name.casefold())

        production_process = _build_process(raw, existing=existing)
        forming_changed = existing is None or raw["forming"] != _forming_from_product(
            existing
        )
        if raw["forming"] == "模切":
            box_category = "die_cut"
            box_style = (
                existing.box_style
                if existing is not None and not forming_changed
                else "模切内盒"
            )
        elif raw["forming"] == "开槽":
            box_category = "normal"
            box_style = (
                existing.box_style
                if existing is not None and not forming_changed
                else "A1"
            )
        else:
            box_category = "normal"
            box_style = (
                existing.box_style
                if existing is not None and not forming_changed
                else None
            )

        updates: dict[str, Any] = {
            "customer_id": customer.id,
            "product_code": raw["product_code"],
            "customer_material_code": raw["customer_material_code"],
            "product_name": raw["product_name"],
            "material_id": material.id if material is not None else None,
            "length_mm": raw["length_mm"],
            "width_mm": raw["width_mm"],
            "height_mm": raw["height_mm"],
            "box_category": box_category,
            "box_style": box_style,
            "print_content": "印刷" if raw["printed"] else "无印刷",
            "printing_colors": raw["print_color"] if raw["printed"] else None,
            "production_process": production_process,
            "unit": existing.unit if existing is not None else "只",
            "sale_unit_price": raw["sale_unit_price"],
            "remark": raw["remark"],
            "is_active": raw["is_active"],
            "flute_type": raw["flute_type"],
            "layer_count": effective_layer,
            "report_length_mm": raw["report_length_mm"],
            "report_width_mm": raw["report_width_mm"],
            "default_cutting_mode": (
                existing.default_cutting_mode
                if existing is not None
                else "一开一"
            ),
            "splice_mode": existing.splice_mode if existing is not None else "single",
            "pieces_per_box": existing.pieces_per_box if existing is not None else 1,
            "flap_mm": existing.flap_mm if existing is not None else 30,
            "combination_mode": (
                existing.combination_mode
                if existing is not None
                else "parent_priced_set"
            ),
        }
        if existing is not None:
            # Empty optional office fields do not silently clear existing master
            # data.  Clearing master data remains a deliberate single-record edit.
            for field in (
                "material_id",
                "length_mm",
                "width_mm",
                "height_mm",
                "sale_unit_price",
                "flute_type",
                "layer_count",
                "report_length_mm",
                "report_width_mm",
            ):
                if updates[field] is None:
                    updates[field] = getattr(existing, field)
        version_fields = set(serialize_versioned_entity("product", existing)) if existing else set()
        changed = (
            existing is None
            or any(
                getattr(existing, key, None) != value
                for key, value in updates.items()
                if key in version_fields
            )
        )

        missing: list[str] = []
        if not all(
            value is not None
            for value in (
                updates["length_mm"],
                updates["width_mm"],
                updates["height_mm"],
            )
        ):
            missing.append("可见尺寸")
        if updates["material_id"] is None:
            missing.append("材质代码")
        if updates["layer_count"] is None:
            missing.append("层数")
        if not updates["flute_type"]:
            missing.append("楞型")
        if updates["sale_unit_price"] is None:
            missing.append("默认含税单价")
        if updates["report_length_mm"] is None:
            missing.append("报料长")
        if updates["report_width_mm"] is None:
            missing.append("报料宽")
        if existing is None and updates["report_width_mm"] is not None:
            missing.append("压线类型")

        items.append(
            {
                "row_number": row_number,
                "sample_no": raw["sample_no"],
                "action": "create" if existing is None else "update",
                "changed": changed,
                "existing_id": existing.id if existing else None,
                "expected_version": existing.version if existing else None,
                "updates": updates,
                "missing_fields": tuple(dict.fromkeys(missing)),
                "drawing_names": raw["drawing_names"],
                "mold_code": mold_code,
                "before_fingerprint": (
                    _fingerprint(serialize_versioned_entity("product", existing))
                    if existing is not None
                    else None
                ),
            }
        )

    if referenced_drawings != uploaded_names:
        missing = sorted(referenced_drawings - uploaded_names)
        extra = sorted(uploaded_names - referenced_drawings)
        if missing:
            _append_error(
                errors,
                sheet=PRODUCT_SHEET,
                row_number=1,
                message="缺少图纸原文件：" + "、".join(missing),
            )
        if extra:
            _append_error(
                errors,
                sheet=PRODUCT_SHEET,
                row_number=1,
                message="上传了 Excel 未引用的图纸文件：" + "、".join(extra),
            )

    summary = {
        "create": sum(item["action"] == "create" for item in items),
        "update": sum(
            item["action"] == "update" and item["changed"] for item in items
        ),
        "unchanged": sum(
            item["action"] == "unchanged"
            or (item["action"] == "update" and not item["changed"] and not item["drawing_names"])
            for item in items
        ),
        "needs_completion": sum(bool(item["missing_fields"]) for item in items),
        "mold_create": sum(item["action"] == "create" for item in mold_items),
        "drawing_files": len(uploaded_names),
    }
    return (
        items,
        mold_items,
        tuple(material_references[key] for key in sorted(material_references)),
        tuple(supplier_references[key] for key in sorted(supplier_references)),
        summary,
    )


async def preview_product_workbook(
    db: Session,
    *,
    customer_id: int,
    workbook_upload: UploadFile,
    drawing_files: list[UploadFile],
    user: User,
) -> dict[str, Any]:
    del user
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise ProductWorkbookError(
            "PRODUCT_WORKBOOK_CUSTOMER_NOT_FOUND",
            "客户不存在",
            status_code=404,
        )
    try:
        validated_workbook = await read_validated_upload(
            workbook_upload,
            EXCEL_POLICY,
        )
        validated_drawings: list[ValidatedUpload] = []
        if len(drawing_files) > MAX_DRAWING_FILES:
            raise UploadValidationError(
                f"单批最多上传 {MAX_DRAWING_FILES} 个图纸原文件"
            )
        total_size = 0
        seen_names: set[str] = set()
        for file in drawing_files:
            drawing = await read_validated_upload(file, DRAWING_POLICY)
            folded = drawing.original_filename.casefold()
            if folded in seen_names:
                raise UploadValidationError(
                    f"图纸文件名重复：{drawing.original_filename}"
                )
            seen_names.add(folded)
            total_size += drawing.size
            if total_size > MAX_DRAWING_TOTAL_BYTES:
                raise UploadValidationError("单批图纸总大小不能超过100MB")
            validated_drawings.append(drawing)
    except UploadValidationError as error:
        raise ProductWorkbookError(
            "PRODUCT_WORKBOOK_UPLOAD_INVALID",
            str(error),
        ) from error

    raw_products, raw_molds, errors, embedded_images = _read_workbook(
        validated_workbook.content,
        expected_customer_id=customer.id,
        expected_customer_name=customer.name,
    )
    embedded_drawings = [
        upload
        for row_number in sorted(embedded_images)
        for upload in embedded_images[row_number]
    ]
    if embedded_drawings and validated_drawings:
        _append_error(
            errors,
            sheet=PRODUCT_SHEET,
            row_number=1,
            message="内嵌图片模式不能同时上传外部图纸文件",
        )
    effective_drawings = (
        embedded_drawings if embedded_drawings else validated_drawings
    )
    (
        items,
        mold_items,
        material_references,
        supplier_references,
        summary,
    ) = _resolve_plan(
        db,
        customer=customer,
        raw_products=raw_products,
        raw_molds=raw_molds,
        uploaded_names={
            item.original_filename.casefold() for item in effective_drawings
        },
        errors=errors,
    )

    result: dict[str, Any] = {
        "valid": not errors,
        "import_allowed": False,
        "write_blocked_reason": MAPPING_PENDING_MESSAGE,
        "filename": validated_workbook.original_filename,
        "sha256": validated_workbook.sha256,
        "customer_id": customer.id,
        "customer_name": customer.name,
        "summary": summary,
        "errors": errors,
        "items": [
            {
                "row_number": item["row_number"],
                "sample_no": item.get("sample_no"),
                "action": item["action"],
                "changed": item["changed"],
                "candidate_model": item["updates"].get("product_code"),
                "customer_material_code": item["updates"].get(
                    "customer_material_code"
                ),
                "product_name": item["updates"].get("product_name"),
                "missing_fields": list(item["missing_fields"]),
                "drawing_files": list(item["drawing_names"]),
                "drawing_mode": next(
                    (
                        raw["drawing_mode"]
                        for raw in raw_products
                        if raw["row_number"] == item["row_number"]
                    ),
                    "none",
                ),
                "mold_code": item["mold_code"],
            }
            for item in items
        ],
        "molds": [
            {
                "row_number": item["row_number"],
                "action": item["action"],
                "mold_code": item["mold_code"],
                **item["updates"],
            }
            for item in mold_items
        ],
    }
    return result


def apply_product_workbook(
    db: Session,
    *,
    preview_token: str,
    user: User,
) -> dict[str, Any]:
    del db, preview_token, user
    raise ProductWorkbookError(
        "PRODUCT_WORKBOOK_MAPPING_PENDING",
        MAPPING_PENDING_MESSAGE,
        status_code=409,
    )
