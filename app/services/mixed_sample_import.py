from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO
import re
import secrets
from threading import RLock
import time
from typing import Any, Iterable

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from PIL import Image

from app.models.user import User
from app.services.product_import_workbook import (
    EmbeddedProductDrawing,
    PREVIEW_TTL_SECONDS,
    ProductImportWorkbookError,
    _extract_embedded_product_drawings,
    _preflight_product_xlsx_container,
)
from app.services.secure_uploads import discard_temporary_token


MIXED_TEMPLATE_VERSION = "P1-22-mixed-v1"
MIXED_INFO_SHEET = "导入信息"
MIXED_GUIDE_SHEET = "使用说明"
MIXED_PRODUCT_SHEET = "样品录入"
MIXED_HEADERS = (
    "样品号*",
    "手写型号*",
    "客户代码(可空)",
    "楞型",
    "成型方式*",
    "印刷*",
    "印刷颜色",
    "结合方式*",
    "二次粘合*",
    "图片文件名(可空)",
    "现场备注",
)
SUPPORTED_CUSTOMERS = ("YL", "YKE", "KEW")
_MIXED_PREVIEW_LOCK = RLock()
_MIXED_PREVIEWS: OrderedDict[str, "MixedSampleImportPreview"] = OrderedDict()
_MAX_MIXED_PREVIEWS = 32


@dataclass(frozen=True)
class MixedSampleImportPreview:
    actor: str
    owner_id: int
    expires_at: float
    customer_ids: tuple[int, ...]
    source_sha256: str
    reference_sha256: str
    source_files: tuple[dict[str, Any], ...]
    product_items: tuple[dict[str, Any], ...]
    drawing_tokens: dict[str, str]
    summary: dict[str, int]


def _actor(user: User) -> str:
    return f"id:{user.id}"


def _discard_preview_files(preview: MixedSampleImportPreview) -> None:
    for token in preview.drawing_tokens.values():
        try:
            discard_temporary_token(token, owner_id=preview.owner_id)
        except Exception:
            continue


def _purge_previews(now: float) -> None:
    expired = [
        token for token, item in _MIXED_PREVIEWS.items()
        if item.expires_at <= now
    ]
    for token in expired:
        item = _MIXED_PREVIEWS.pop(token, None)
        if item is not None:
            _discard_preview_files(item)
    while len(_MIXED_PREVIEWS) >= _MAX_MIXED_PREVIEWS:
        _token, item = _MIXED_PREVIEWS.popitem(last=False)
        _discard_preview_files(item)


def store_mixed_preview(preview: MixedSampleImportPreview) -> str:
    token = secrets.token_urlsafe(32)
    with _MIXED_PREVIEW_LOCK:
        _purge_previews(time.monotonic())
        _MIXED_PREVIEWS[token] = preview
    return token


def get_mixed_preview(token: str, user: User) -> MixedSampleImportPreview:
    with _MIXED_PREVIEW_LOCK:
        _purge_previews(time.monotonic())
        preview = _MIXED_PREVIEWS.get(token)
        if preview is None or preview.actor != _actor(user):
            raise ProductImportWorkbookError(
                "MIXED_SAMPLE_IMPORT_PREVIEW_STALE",
                "混合样品预检已过期或不属于当前操作人，请重新上传预检",
                status_code=409,
            )
        _MIXED_PREVIEWS.move_to_end(token)
        return preview


def consume_mixed_preview(token: str, *, keep_files: bool = True) -> None:
    with _MIXED_PREVIEW_LOCK:
        preview = _MIXED_PREVIEWS.pop(token, None)
    if preview is not None and not keep_files:
        _discard_preview_files(preview)


def _set_widths(worksheet, widths: Iterable[int]) -> None:
    for index, width in enumerate(widths, start=1):
        worksheet.column_dimensions[get_column_letter(index)].width = width


def _style_header(worksheet, row: int = 1) -> None:
    for cell in worksheet[row]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="2F75B5")
        cell.alignment = Alignment(
            horizontal="center", vertical="center", wrap_text=True
        )
    worksheet.row_dimensions[row].height = 32


def build_mixed_sample_workbook(*, blank_rows: int = 160) -> bytes:
    """Build the replacement for the old printable .xls registration sheet."""

    workbook = Workbook()
    guide = workbook.active
    guide.title = MIXED_GUIDE_SHEET
    guide.sheet_view.showGridLines = False
    guide.merge_cells("A1:F1")
    guide["A1"] = "天明 ERP｜混合客户样品现场登记"
    guide["A1"].font = Font(bold=True, color="FFFFFF", size=16)
    guide["A1"].fill = PatternFill("solid", fgColor="17365D")
    guide["A1"].alignment = Alignment(vertical="center")
    guide.row_dimensions[1].height = 30
    instructions = (
        ("最少填写", "样品号、手写型号，再从下拉框选择目测得到的楞型和工艺。客户代码可留空，由 ERP 用三客户基础资料自动分拣。"),
        ("照片命名", "直接把照片改为“样品号_序号.jpg”，例如 YP211_1.jpg、YP211_2.jpg；无需 CSV，也无需先压缩。"),
        ("上传方式", "在 ERP 的“混合样品整理”一次选择本表、三客户基础资料和照片；旧的带内嵌图片分卷 Excel 也可直接选择。"),
        ("基础资料", "产品名称、图号、成品尺寸、报料长宽、材质文字和价格以 YL/YKE/KEW 基础资料为准，不在现场重复抄写。"),
        ("同码客户", "同一型号同时属于 YKE/KEW 时，预检会显示候选客户；必须明确选择后才能保存，不会自动猜客户。"),
        ("批量修改", "已经录入 ERP 的常用箱仍从对应客户页下载样品 Excel，批量修改后回传；不需要逐个打开编辑。"),
        ("写入规则", "上传预检不会写数据；只有管理员点击一次“确认录入”才会把三家客户作为一个事务保存。"),
    )
    for label, detail in instructions:
        guide.append([label, detail])
    for row in range(2, 2 + len(instructions)):
        guide.cell(row=row, column=1).font = Font(bold=True, color="17365D")
        guide.cell(row=row, column=2).alignment = Alignment(
            wrap_text=True, vertical="top"
        )
        guide.row_dimensions[row].height = 34
    _set_widths(guide, (18, 92, 4, 4, 4, 4))

    sheet = workbook.create_sheet(MIXED_PRODUCT_SHEET)
    sheet.sheet_view.showGridLines = False
    sheet.append(MIXED_HEADERS)
    _style_header(sheet)
    for index in range(1, blank_rows + 1):
        sheet.append(
            [
                f"YP{index:03d}", "", "", "", "", "", "黑色", "", "否", "", ""
            ]
        )
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:K{sheet.max_row}"
    _set_widths(sheet, (12, 24, 18, 11, 14, 10, 14, 14, 13, 34, 30))
    for row in sheet.iter_rows(min_row=2):
        sheet.row_dimensions[row[0].row].height = 30
        for cell in row:
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        for cell in row[:9]:
            cell.fill = PatternFill("solid", fgColor="FFF2CC")
    validations = (
        (DataValidation(type="list", formula1='"YL,YKE,KEW"', allow_blank=True), f"C2:C{sheet.max_row}"),
        (DataValidation(type="list", formula1='"A,B,E,AB,BE,AAA,ABC,其他,不确定"', allow_blank=True), f"D2:D{sheet.max_row}"),
        (DataValidation(type="list", formula1='"模切,开槽,无需,不确定"', allow_blank=True), f"E2:E{sheet.max_row}"),
        (DataValidation(type="list", formula1='"是,否,不确定"', allow_blank=True), f"F2:F{sheet.max_row}"),
        (DataValidation(type="list", formula1='"黑色,红色,其他,无"', allow_blank=True), f"G2:G{sheet.max_row}"),
        (DataValidation(type="list", formula1='"打钉,粘合,无需结合,不确定"', allow_blank=True), f"H2:H{sheet.max_row}"),
        (DataValidation(type="list", formula1='"是,否"', allow_blank=True), f"I2:I{sheet.max_row}"),
    )
    for validation, cells in validations:
        sheet.add_data_validation(validation)
        validation.add(cells)
    sheet.print_title_rows = "1:1"
    sheet.print_area = f"A1:I{sheet.max_row}"
    sheet.page_setup.orientation = "landscape"
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.sheet_properties.pageSetUpPr.fitToPage = True

    info = workbook.create_sheet(MIXED_INFO_SHEET)
    info.sheet_state = "veryHidden"
    info.append(["template_version", MIXED_TEMPLATE_VERSION])
    info.append(["generated_at", datetime.now().isoformat(timespec="seconds")])
    info.append(["purpose", "mixed_customer_sample_registration"])

    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _decimal(value: object) -> Decimal | None:
    text = _text(value)
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _positive_int(value: object) -> int | None:
    number = _decimal(value)
    if number is None or number <= 0 or number != number.to_integral_value():
        return None
    return int(number)


def normalize_sample_code(value: object) -> str:
    text = re.sub(r"\s+", "", _text(value)).upper()
    text = text.replace("．", ".")
    return text


def _parse_dimensions(value: object) -> tuple[int | None, int | None, int | None]:
    text = _text(value).replace("×", "*").replace("X", "*").replace("x", "*")
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*\*\s*(\d+(?:\.\d+)?)\s*\*\s*(\d+(?:\.\d+)?)\s*", text)
    if match is None:
        return None, None, None
    values = tuple(int(Decimal(part)) for part in match.groups())
    return values[0], values[1], values[2]


def _cutting_mode(value: object) -> str:
    text = _text(value).replace("×", "*").replace("X", "*").replace("x", "*")
    match = re.search(r"=\s*([1-6])\s*(?:PCS)?\s*$", text, re.IGNORECASE)
    count = int(match.group(1)) if match else 1
    return {1: "一开一", 2: "一开二", 3: "一开三", 4: "一开四", 5: "一开五", 6: "一开六"}[count]


def _crease_values(value: object, report_width: int | None) -> tuple[str | None, int | None, int | None, int | None]:
    text = _text(value).replace("×", "*").replace("X", "*").replace("x", "*")
    match = re.fullmatch(r"\s*(\d+)\s*\*\s*(\d+)\s*\*\s*(\d+)\s*=\s*(\d+)\s*", text)
    if match:
        left, middle, right, total = (int(part) for part in match.groups())
        if left == right and left + middle + right == total and (report_width is None or total == report_width):
            return "压线", left, middle, right
    if re.search(r"\d+\s*[*×Xx]\s*\d+\s*=", text):
        return "净料", None, None, None
    return None, None, None, None


def _material_flute(value: object) -> tuple[str | None, int | None]:
    text = _text(value).upper().replace("（", "(").replace("）", ")")
    if not text or "灰底白板" in text or "白卡" in text:
        return None, None
    matches = re.findall(r"(?:/|\()\s*(ABC|AAA|AB|BE|A|B|E)\s*\)?", text)
    flute = matches[-1] if matches else None
    if flute in {"AB", "BE"}:
        return flute, 5
    if flute in {"AAA", "ABC"}:
        return flute, 7
    if flute in {"A", "B", "E"}:
        return flute, 3
    return None, None


def _reference_record(
    *,
    customer_code: str,
    source_sheet: str,
    source_row: int,
    product_code: object,
    product_name: object,
    drawing_number: object,
    dimensions_raw: object,
    report_raw: object,
    report_width: object,
    report_length: object,
    material_text: object,
    sale_unit_price: object,
    base_process: object,
) -> dict[str, Any] | None:
    code = normalize_sample_code(product_code)
    original_name = _text(product_name)
    name = original_name or _text(drawing_number) or code
    if not code:
        return None
    width = _positive_int(report_width)
    length = _positive_int(report_length)
    product_length, product_width, product_height = _parse_dimensions(dimensions_raw)
    flute_type, layer_count = _material_flute(material_text)
    crease_type, crease_left, crease_middle, crease_right = _crease_values(report_raw, width)
    return {
        "key": f"{customer_code}:{source_sheet}:{source_row}",
        "customer_code": customer_code,
        "source_sheet": source_sheet,
        "source_row": source_row,
        "product_code": code,
        "customer_material_code": code,
        "product_name": name,
        "product_name_was_blank": not original_name,
        "drawing_number": _text(drawing_number) or None,
        "length_mm": product_length,
        "width_mm": product_width,
        "height_mm": product_height,
        "box_category": "normal" if product_height is not None else "die_cut",
        "box_style": "A1" if product_height is not None else "模切内盒",
        "report_length_mm": length,
        "report_width_mm": width,
        "crease_type": crease_type,
        "crease_left_mm": crease_left,
        "crease_middle_mm": crease_middle,
        "crease_right_mm": crease_right,
        "default_cutting_mode": _cutting_mode(report_raw),
        "legacy_material_text": _text(material_text) or None,
        "flute_type": flute_type,
        "layer_count": layer_count,
        "sale_unit_price": _decimal(sale_unit_price),
        "base_process": _text(base_process),
        "dimensions_raw": _text(dimensions_raw),
        "report_raw": _text(report_raw),
    }


def read_mixed_reference_workbook(content: bytes) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    # The three-customer source workbook contains ordinary calculation cells.
    # We read cached values only; macros, external links and active objects stay blocked.
    _preflight_product_xlsx_container(
        content,
        allow_formulas=True,
        allow_reference_annotations=True,
    )
    try:
        workbook = load_workbook(BytesIO(content), read_only=True, data_only=True, keep_links=False)
    except Exception as error:
        raise ProductImportWorkbookError(
            "MIXED_SAMPLE_REFERENCE_INVALID",
            "三客户基础资料无法安全读取",
        ) from error
    errors: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    try:
        sheets = {re.sub(r"\s+", "", name).upper(): name for name in workbook.sheetnames}
        required = {"YL", "YKE", "KEW"}
        if not required.issubset(sheets):
            raise ProductImportWorkbookError(
                "MIXED_SAMPLE_REFERENCE_SHEETS_INVALID",
                "基础资料必须同时包含 YL、YKE、KEW 三个工作表",
            )
        for customer_code in SUPPORTED_CUSTOMERS:
            worksheet = workbook[sheets[customer_code]]
            for row_number, values in enumerate(
                worksheet.iter_rows(max_col=19, values_only=True), start=1
            ):
                if customer_code == "YL":
                    if row_number == 1:
                        continue
                    record = _reference_record(
                        customer_code=customer_code,
                        source_sheet=worksheet.title,
                        source_row=row_number,
                        product_code=values[0],
                        product_name=values[1],
                        drawing_number=None,
                        dimensions_raw=values[3],
                        report_raw=values[4],
                        report_width=values[5],
                        report_length=values[7],
                        material_text=values[10],
                        sale_unit_price=values[15],
                        base_process=values[16] if len(values) > 16 else None,
                    )
                else:
                    record = _reference_record(
                        customer_code=customer_code,
                        source_sheet=worksheet.title,
                        source_row=row_number,
                        product_code=values[3],
                        product_name=values[1],
                        drawing_number=values[2],
                        dimensions_raw=values[8],
                        report_raw=values[9],
                        report_width=values[10],
                        report_length=values[12],
                        material_text=values[15],
                        sale_unit_price=values[17],
                        base_process=values[18],
                    )
                if record is not None:
                    records.append(record)
        if not records:
            errors.append({"sheet": "基础资料", "row_number": 1, "message": "没有读取到可用产品资料"})
        return records, errors
    finally:
        workbook.close()


def _image_extension(content: bytes) -> str:
    with Image.open(BytesIO(content)) as image:
        return ".png" if (image.format or "").upper() == "PNG" else ".jpg"


def _choice(value: object, choices: set[str]) -> str | None:
    text = _text(value)
    return text if text in choices else None


def _row_from_values(
    *,
    row_number: int,
    values: list[object],
    embedded: tuple[EmbeddedProductDrawing, ...],
    simplified: bool,
) -> dict[str, Any] | None:
    if simplified:
        padded = values + [None] * (len(MIXED_HEADERS) - len(values))
        sample_id, product_code, customer_hint = padded[0], padded[1], padded[2]
        flute, forming, printed = padded[3], padded[4], padded[5]
        color, joining, secondary = padded[6], padded[7], padded[8]
        filename_text, remark = padded[9], padded[10]
    else:
        padded = values + [None] * (23 - len(values))
        sample_id, product_code, customer_hint = padded[0], padded[1], None
        flute, forming, printed = padded[3], padded[4], padded[5]
        color, joining, secondary = padded[6], padded[7], padded[8]
        filename_text, remark = padded[9], padded[11]
    sample = _text(sample_id)
    code = normalize_sample_code(product_code)
    if not embedded and not _text(filename_text):
        if not simplified or not _text(product_code):
            return None
    if not sample or not code:
        return {
            "row_number": row_number,
            "sample_id": sample or None,
            "product_code": code or None,
            "error": "有图片的行必须填写样品号和手写型号",
        }
    filenames = [
        item.strip()
        for item in re.split(r"[;；\r\n]+", _text(filename_text))
        if item.strip()
    ]
    if embedded:
        filenames = [
            f"{sample}_{index}{_image_extension(image.content)}"
            for index, image in enumerate(embedded, start=1)
        ]
    unresolved_fields: list[str] = []
    forming_method = _choice(forming, {"模切", "开槽", "无需"})
    joining_method = _choice(joining, {"打钉", "粘合", "无需结合"})
    printed_text = _choice(printed, {"是", "否"})
    secondary_text = _choice(secondary, {"是", "否"})
    flute_text = _choice(flute, {"A", "B", "E", "AB", "BE", "AAA", "ABC", "其他"})
    for value, label in (
        (forming_method, "成型方式"),
        (joining_method, "结合方式"),
        (printed_text, "印刷"),
        (secondary_text, "二次粘合"),
    ):
        if value is None:
            unresolved_fields.append(label)
    return {
        "row_number": row_number,
        "sample_id": sample,
        "product_code": code,
        "customer_hint": normalize_sample_code(customer_hint) or None,
        "flute_type": None if flute_text == "其他" else flute_text,
        "forming_method": forming_method,
        "printed": None if printed_text is None else printed_text == "是",
        "printing_colors": _text(color) or None,
        "joining_method": joining_method,
        "secondary_gluing": None if secondary_text is None else secondary_text == "是",
        "drawing_filenames": tuple(filenames),
        "remark": _text(remark) or None,
        "unresolved_fields": unresolved_fields,
        "_embedded_drawings": embedded,
    }


def read_mixed_sample_workbook(content: bytes) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read the simplified sheet or legacy P1-22 volumes, only for rows with photos."""

    _preflight_product_xlsx_container(content)
    try:
        workbook = load_workbook(BytesIO(content), read_only=False, data_only=False, keep_links=False)
    except Exception as error:
        raise ProductImportWorkbookError(
            "MIXED_SAMPLE_WORKBOOK_INVALID", "样品 Excel 无法安全读取"
        ) from error
    errors: list[dict[str, Any]] = []
    try:
        if MIXED_PRODUCT_SHEET not in workbook.sheetnames:
            raise ProductImportWorkbookError(
                "MIXED_SAMPLE_WORKBOOK_SHEET_INVALID",
                "样品 Excel 缺少“样品录入”工作表",
            )
        worksheet = workbook[MIXED_PRODUCT_SHEET]
        headers = tuple(_text(cell.value) for cell in worksheet[1])
        simplified = headers[: len(MIXED_HEADERS)] == MIXED_HEADERS
        legacy = len(headers) >= 23 and headers[0] in {"样品号", "样品号*"} and headers[1].startswith("手写型号")
        if not simplified and not legacy:
            raise ProductImportWorkbookError(
                "MIXED_SAMPLE_WORKBOOK_HEADERS_INVALID",
                "样品 Excel 表头无法识别，请下载新的混合样品模板",
            )
        embedded_by_row = _extract_embedded_product_drawings(workbook, errors)
        rows: list[dict[str, Any]] = []
        max_col = len(MIXED_HEADERS) if simplified else 23
        for row_number, cells in enumerate(
            worksheet.iter_rows(min_row=2, max_col=max_col), start=2
        ):
            values = [cell.value for cell in cells]
            row = _row_from_values(
                row_number=row_number,
                values=values,
                embedded=tuple(embedded_by_row.get(row_number, ())),
                simplified=simplified,
            )
            if row is None:
                continue
            if row.get("error"):
                errors.append(
                    {
                        "sheet": MIXED_PRODUCT_SHEET,
                        "row_number": row_number,
                        "message": row["error"],
                    }
                )
                continue
            rows.append(row)
        if not rows and not errors:
            errors.append(
                {
                    "sheet": MIXED_PRODUCT_SHEET,
                    "row_number": 2,
                    "message": "没有找到带图片或图片文件名的样品行",
                }
            )
        return rows, errors
    finally:
        workbook.close()


def _candidate_aliases(sample_code: str) -> tuple[str, str | None]:
    code = normalize_sample_code(sample_code)
    if re.fullmatch(r"Z\.\d{6}", code):
        return f"Z.001.{code[2:]}", None
    suffix_match = re.fullmatch(r"(Z\.\d{3}\.\d{6})(.+)", code)
    if suffix_match:
        return suffix_match.group(1), suffix_match.group(2)
    return code, None


def reference_candidates(
    records: list[dict[str, Any]],
    *,
    sample_code: str,
    customer_hint: str | None = None,
) -> list[dict[str, Any]]:
    base_code, suffix = _candidate_aliases(sample_code)
    candidates = [record for record in records if record["product_code"] == base_code]
    hint = normalize_sample_code(customer_hint)
    if hint:
        candidates = [record for record in candidates if record["customer_code"] == hint]
    if suffix:
        named = [
            record for record in candidates
            if suffix.casefold() in record["product_name"].casefold()
        ]
        if named:
            candidates = named
    return candidates


def collapse_reference_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse component rows that share one customer, name and drawing identity."""

    groups: OrderedDict[tuple[str, str, str], list[dict[str, Any]]] = OrderedDict()
    for candidate in candidates:
        identity = (
            candidate["customer_code"],
            candidate["product_name"].casefold(),
            str(candidate.get("drawing_number") or "").casefold(),
        )
        groups.setdefault(identity, []).append(candidate)
    collapsed: list[dict[str, Any]] = []
    for rows in groups.values():
        chosen = dict(rows[0])
        chosen["source_rows"] = [row["source_row"] for row in rows]
        collapsed.append(chosen)
    return collapsed


def candidate_view(candidate: dict[str, Any]) -> dict[str, Any]:
    dimensions = candidate.get("dimensions_raw") or "-"
    report = "×".join(
        str(value) for value in (
            candidate.get("report_width_mm"), candidate.get("report_length_mm")
        ) if value is not None
    ) or "-"
    return {
        "key": candidate["key"],
        "customer_code": candidate["customer_code"],
        "product_name": candidate["product_name"],
        "drawing_number": candidate.get("drawing_number"),
        "dimensions": dimensions,
        "report_size": report,
        "material": candidate.get("legacy_material_text"),
        "source_rows": candidate.get("source_rows") or [candidate["source_row"]],
    }


def select_reference_candidate(
    records: list[dict[str, Any]],
    registration: dict[str, Any],
    *,
    override_key: str | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    candidates = collapse_reference_candidates(
        reference_candidates(
            records,
            sample_code=registration["product_code"],
            customer_hint=registration.get("customer_hint"),
        )
    )
    if override_key:
        selected = next(
            (candidate for candidate in candidates if candidate["key"] == override_key),
            None,
        )
        return selected, candidates
    if len(candidates) == 1:
        return candidates[0], candidates
    return None, candidates


def merge_registration(target: dict[str, Any], incoming: dict[str, Any]) -> str | None:
    for field in (
        "product_code", "customer_hint", "flute_type", "forming_method",
        "printed", "printing_colors", "joining_method", "secondary_gluing",
    ):
        left = target.get(field)
        right = incoming.get(field)
        if left in (None, "") and right not in (None, ""):
            target[field] = right
        elif right not in (None, "") and left not in (None, "") and left != right:
            return f"同一样品在不同 Excel 的{field}不一致"
    names = list(target.get("drawing_filenames") or ())
    images = list(target.get("_embedded_drawings") or ())
    seen_names = {name.casefold() for name in names}
    if not incoming.get("_embedded_drawings"):
        for name in incoming.get("drawing_filenames") or ():
            if name.casefold() not in seen_names:
                names.append(name)
                seen_names.add(name.casefold())
    hashes = {sha256(item.content).hexdigest() for item in images}
    for name, image in zip(
        incoming.get("drawing_filenames") or (),
        incoming.get("_embedded_drawings") or (),
    ):
        digest = sha256(image.content).hexdigest()
        if digest in hashes:
            continue
        if name.casefold() in seen_names:
            return f"同一样品存在同名但内容不同的图片：{name}"
        names.append(name)
        seen_names.add(name.casefold())
        images.append(image)
        hashes.add(digest)
    target["drawing_filenames"] = tuple(names)
    target["_embedded_drawings"] = tuple(images)
    target["unresolved_fields"] = [
        label
        for field, label in (
            ("forming_method", "成型方式"),
            ("joining_method", "结合方式"),
            ("printed", "印刷"),
            ("secondary_gluing", "二次粘合"),
        )
        if target.get(field) is None
    ]
    return None


def process_from_registration(
    registration: dict[str, Any], reference: dict[str, Any]
) -> tuple[str, str | None, str | None, bool, tuple[str, ...], list[str]]:
    base_process = str(reference.get("base_process") or "")
    forming = registration.get("forming_method")
    joining = registration.get("joining_method")
    printed = registration.get("printed")
    secondary = registration.get("secondary_gluing")
    unresolved: list[str] = []
    if forming is None:
        if "轧" in base_process or reference["box_category"] == "die_cut":
            forming = "模切"
        elif reference["box_category"] == "normal":
            forming = "开槽"
        else:
            unresolved.append("成型方式")
    if joining is None:
        if "钉" in base_process:
            joining = "打钉"
        elif "贴" in base_process:
            joining = "粘合"
        else:
            unresolved.append("结合方式")
    if printed is None:
        unresolved.append("印刷")
        printed = False
    if secondary is None:
        unresolved.append("二次粘合")
        secondary = False
    tokens: list[str] = []
    if forming in {"模切", "开槽"}:
        tokens.append(forming)
    if printed:
        tokens.append("印刷")
    if joining in {"打钉", "粘合"}:
        tokens.append(joining)
    if secondary:
        tokens.append("二次粘合")
    box_category = "die_cut" if forming == "模切" else "normal"
    box_style = "模切内盒" if box_category == "die_cut" else "A1"
    return box_category, box_style, joining, printed, tuple(tokens), unresolved
