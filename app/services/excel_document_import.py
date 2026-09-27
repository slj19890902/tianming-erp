from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
import hashlib
import json
import re
from pathlib import PurePath
from typing import Any, Literal
from zipfile import BadZipFile, ZipFile


MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_SHEETS = 30
MAX_ROWS = 20_000
MAX_COLUMNS = 200
MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
OLE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")
ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
PARSER_VERSION = "excel-order-predelivery-v1"


class ExcelImportError(ValueError):
    pass


@dataclass(frozen=True)
class SourceCell:
    row: int
    column: int
    coordinate: str
    raw_value: Any
    display_value: str


@dataclass
class ImportedRow:
    sheet: str
    source_row: int
    source_no: str | None
    customer_po: str | None
    stock_code: str
    product_name: str | None
    requested_quantity: int
    order_quantity: int | None = None
    unit: str | None = None
    unit_price: Decimal | None = None
    amount: Decimal | None = None
    drawing_number: str | None = None
    category: str | None = None
    model: str | None = None
    cells: dict[str, SourceCell] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)

    def normalized_identity(self) -> dict[str, Any]:
        return {
            "sheet": self.sheet,
            "source_row": self.source_row,
            "stock_code": self.stock_code,
            "requested_quantity": self.requested_quantity,
            "order_quantity": self.order_quantity,
            "customer_po": self.customer_po,
        }


@dataclass
class ImportedDocument:
    document_type: Literal["order", "pre_delivery"]
    source_format: Literal["xls", "xlsx"]
    template: str
    filename: str
    source_hash: str
    parser_version: str
    customer_code: str | None
    customer_name_evidence: str | None
    source_date: date | None
    delivery_date: date | None
    customer_po: str | None
    rows: list[ImportedRow]
    skipped_sheets: list[str]
    warnings: list[str]

    @property
    def business_fingerprint(self) -> str:
        payload = {
            "document_type": self.document_type,
            "customer_code": self.customer_code,
            "source_date": self.source_date.isoformat() if self.source_date else None,
            "customer_po": self.customer_po,
            "rows": [row.normalized_identity() for row in self.rows],
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


def _safe_filename(filename: str) -> str:
    value = PurePath(str(filename or "Excel来源").replace("\\", "/")).name
    value = "".join(character for character in value if character >= " " and character != "\x7f")
    return value[:255] or "Excel来源"


def _decimal(value: Any, *, label: str, required: bool = False) -> Decimal | None:
    if value in (None, ""):
        if required:
            raise ExcelImportError(f"{label}为空")
        return None
    text = str(value).strip().replace(",", "")
    try:
        result = Decimal(text)
    except (InvalidOperation, ValueError) as error:
        raise ExcelImportError(f"{label}不是有效数字：{text}") from error
    if not result.is_finite():
        raise ExcelImportError(f"{label}不是有限数字")
    return result


def _positive_integer(value: Any, *, label: str) -> int:
    result = _decimal(value, label=label, required=True)
    assert result is not None
    if result <= 0 or result != result.to_integral_value():
        raise ExcelImportError(f"{label}必须是正整数")
    return int(result)


def _date_value(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for pattern in (r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", r"(20\d{2})(\d{2})(\d{2})"):
        match = re.search(pattern, text)
        if match:
            try:
                return date(*(int(part) for part in match.groups()))
            except ValueError:
                return None
    return None


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).replace("\x00", "").strip()
    return text or None


def _code_text(raw: Any, display: str) -> str:
    text = display.strip()
    if not text:
        return ""
    if isinstance(raw, float) and raw.is_integer() and text.endswith(".0"):
        text = text[:-2]
    return text


class _Sheet:
    def __init__(self, name: str, rows: list[list[SourceCell]], *, hidden: bool = False):
        self.name = name
        self.rows = rows
        self.hidden = hidden

    def cell(self, row: int, column: int) -> SourceCell:
        if row < 1 or column < 1 or row > len(self.rows) or column > len(self.rows[row - 1]):
            return SourceCell(row, column, _coordinate(row, column), None, "")
        return self.rows[row - 1][column - 1]

    def text(self, row: int, column: int) -> str:
        return self.cell(row, column).display_value.strip()

    @property
    def max_row(self) -> int:
        return len(self.rows)


def _coordinate(row: int, column: int) -> str:
    letters = ""
    value = column
    while value:
        value, remainder = divmod(value - 1, 26)
        letters = chr(65 + remainder) + letters
    return f"{letters}{row}"


def _formatted_numeric(value: Any, number_format: str | None) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return _clean_text(value) or ""
    fmt = str(number_format or "").split(";")[0]
    integer = int(value) if float(value).is_integer() else None
    zero_match = re.fullmatch(r"0+", fmt)
    if integer is not None and zero_match:
        return f"{integer:0{len(fmt)}d}"
    return str(integer if integer is not None else value)


def _read_xlsx(content: bytes) -> list[_Sheet]:
    try:
        with ZipFile(BytesIO(content)) as archive:
            names = archive.namelist()
            total = sum(info.file_size for info in archive.infolist())
            compressed = sum(max(info.compress_size, 1) for info in archive.infolist())
            if total > MAX_UNCOMPRESSED_BYTES or total / max(compressed, 1) > MAX_COMPRESSION_RATIO:
                raise ExcelImportError("Excel 解压体积或压缩比超过安全限制")
            lowered = {name.casefold() for name in names}
            if any("vbaproject.bin" in name for name in lowered):
                raise ExcelImportError("不接受含宏的 Excel 文件")
            if any(name.startswith("xl/externallinks/") for name in lowered):
                raise ExcelImportError("不接受含外部链接的 Excel 文件")
    except BadZipFile as error:
        raise ExcelImportError("xlsx 文件结构损坏") from error
    from openpyxl import load_workbook
    try:
        workbook = load_workbook(BytesIO(content), read_only=False, data_only=True, keep_links=False)
    except Exception as error:
        raise ExcelImportError("xlsx 无法读取，可能已损坏或加密") from error
    try:
        if len(workbook.worksheets) > MAX_SHEETS:
            raise ExcelImportError("Excel 工作表数量超过限制")
        result: list[_Sheet] = []
        for worksheet in workbook.worksheets:
            if worksheet.max_row > MAX_ROWS or worksheet.max_column > MAX_COLUMNS:
                raise ExcelImportError(f"工作表“{worksheet.title}”行列数超过限制")
            rows: list[list[SourceCell]] = []
            for row_index, cells in enumerate(worksheet.iter_rows(), start=1):
                rows.append([
                    SourceCell(row_index, cell.column, cell.coordinate, cell.value,
                               _formatted_numeric(cell.value, cell.number_format))
                    for cell in cells
                ])
            result.append(_Sheet(worksheet.title, rows, hidden=worksheet.sheet_state != "visible"))
        return result
    finally:
        workbook.close()


def _read_xls(content: bytes) -> list[_Sheet]:
    try:
        import xlrd
    except ImportError as error:
        raise ExcelImportError("服务器缺少 xls 读取组件，请安装 xlrd 2.0.2") from error
    try:
        workbook = xlrd.open_workbook(file_contents=content, on_demand=True, formatting_info=True)
    except Exception as error:
        raise ExcelImportError("xls 无法读取，可能已损坏、加密或不是有效工作簿") from error
    try:
        if workbook.nsheets > MAX_SHEETS:
            raise ExcelImportError("Excel 工作表数量超过限制")
        result: list[_Sheet] = []
        for index in range(workbook.nsheets):
            worksheet = workbook.sheet_by_index(index)
            if worksheet.nrows > MAX_ROWS or worksheet.ncols > MAX_COLUMNS:
                raise ExcelImportError(f"工作表“{worksheet.name}”行列数超过限制")
            rows: list[list[SourceCell]] = []
            for row_index in range(worksheet.nrows):
                values: list[SourceCell] = []
                for column_index in range(worksheet.ncols):
                    cell = worksheet.cell(row_index, column_index)
                    raw: Any = cell.value
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        try:
                            raw = xlrd.xldate.xldate_as_datetime(cell.value, workbook.datemode)
                        except Exception:
                            raw = cell.value
                    number_format = None
                    if cell.xf_index is not None and cell.xf_index < len(workbook.xf_list):
                        format_key = workbook.xf_list[cell.xf_index].format_key
                        format_info = workbook.format_map.get(format_key)
                        number_format = format_info.format_str if format_info else None
                    values.append(SourceCell(row_index + 1, column_index + 1,
                        _coordinate(row_index + 1, column_index + 1), raw,
                        _formatted_numeric(raw, number_format)))
                rows.append(values)
            visibility = getattr(workbook, "sheet_visibility", [0] * workbook.nsheets)
            result.append(_Sheet(worksheet.name, rows, hidden=bool(visibility[index])))
        return result
    finally:
        workbook.release_resources()


def _read_sheets(content: bytes) -> tuple[Literal["xls", "xlsx"], list[_Sheet]]:
    if content.startswith(OLE_SIGNATURE):
        return "xls", _read_xls(content)
    if content.startswith(ZIP_SIGNATURES):
        return "xlsx", _read_xlsx(content)
    raise ExcelImportError("文件签名不是有效的 xls 或 xlsx，不能只修改扩展名导入")


def _row(sheet: _Sheet, row_number: int, mapping: dict[str, int], *, document_type: str) -> ImportedRow | None:
    cells = {name: sheet.cell(row_number, column) for name, column in mapping.items()}
    stock = _code_text(cells["stock_code"].raw_value, cells["stock_code"].display_value)
    quantity_cell = cells["order_quantity"] if document_type == "order" else cells["requested_quantity"]
    if not stock and not quantity_cell.display_value.strip():
        return None
    if not stock:
        return None
    quantity = _positive_integer(quantity_cell.raw_value, label=f"{sheet.name}!{quantity_cell.coordinate} 数量")
    requested = quantity
    order_quantity = quantity if document_type == "order" else None
    issues: list[str] = []
    if document_type == "order" and "requested_quantity" in cells:
        requested_value = _positive_integer(cells["requested_quantity"].raw_value,
            label=f"{sheet.name}!{cells['requested_quantity'].coordinate} 要求数")
        requested = requested_value
        if requested_value != quantity:
            issues.append(f"要求数 {requested_value} 与发注数 {quantity} 不一致，请确认订单数量")
    return ImportedRow(
        sheet=sheet.name,
        source_row=row_number,
        source_no=_clean_text(cells.get("source_no").display_value if cells.get("source_no") else None),
        customer_po=_clean_text(cells.get("customer_po").display_value if cells.get("customer_po") else None),
        stock_code=stock,
        product_name=_clean_text(cells.get("product_name").display_value if cells.get("product_name") else None),
        requested_quantity=requested,
        order_quantity=order_quantity,
        unit=_clean_text(cells.get("unit").display_value if cells.get("unit") else None),
        unit_price=_decimal(cells.get("unit_price").raw_value, label="单价") if cells.get("unit_price") else None,
        amount=_decimal(cells.get("amount").raw_value, label="金额") if cells.get("amount") else None,
        drawing_number=_clean_text(cells.get("drawing_number").display_value if cells.get("drawing_number") else None),
        category=_clean_text(cells.get("category").display_value if cells.get("category") else None),
        model=_clean_text(cells.get("model").display_value if cells.get("model") else None),
        cells=cells,
        issues=issues,
    )


def _sheet_text(sheet: _Sheet) -> str:
    return " ".join(cell.display_value for row in sheet.rows for cell in row if cell.display_value)


def _parse_yanguang_order(sheets: list[_Sheet]) -> tuple[list[ImportedRow], date | None, str | None, list[str]]:
    rows: list[ImportedRow] = []
    warnings: list[str] = []
    source_date: date | None = None
    customer_po: str | None = None
    mapping = {"source_no": 1, "customer_po": 2, "stock_code": 3, "product_name": 4,
               "drawing_number": 5, "requested_quantity": 6, "unit": 7,
               "order_quantity": 10, "unit_price": 12, "amount": 13}
    for sheet in sheets:
        text = _sheet_text(sheet)
        if "品目" not in text or ("發注" not in text and "发注" not in text):
            continue
        if sheet.hidden:
            warnings.append(f"隐藏工作表“{sheet.name}”未自动导入")
            continue
        source_date = source_date or _date_value(sheet.cell(4, 12).raw_value) or _date_value(sheet.cell(2, 16).raw_value)
        for number in range(11, sheet.max_row + 1):
            try:
                parsed = _row(sheet, number, mapping, document_type="order")
            except ExcelImportError as error:
                if sheet.text(number, 3):
                    raise
                continue
            if parsed:
                rows.append(parsed)
                customer_po = customer_po or parsed.customer_po
    if not rows:
        raise ExcelImportError("未识别到研光购买要求书有效明细")
    return rows, source_date, customer_po, warnings


def _parse_yanguang_predelivery(sheets: list[_Sheet]) -> tuple[list[ImportedRow], date | None, list[str]]:
    mapping = {"source_no": 1, "product_name": 2, "stock_code": 3,
               "drawing_number": 4, "requested_quantity": 5, "unit_price": 6, "amount": 7}
    rows: list[ImportedRow] = []
    warnings: list[str] = []
    delivery_date: date | None = None
    for sheet in sheets:
        text = _sheet_text(sheet)
        if "品目" not in text or "数量" not in text:
            continue
        if sheet.hidden:
            warnings.append(f"隐藏工作表“{sheet.name}”未自动导入")
            continue
        delivery_date = delivery_date or _date_value(sheet.text(2, 1)) or _date_value(text[:300])
        for number in range(4, sheet.max_row + 1):
            parsed = _row(sheet, number, mapping, document_type="pre_delivery")
            if parsed:
                rows.append(parsed)
    if not rows:
        raise ExcelImportError("未识别到研光预送货有效明细")
    return rows, delivery_date, warnings


def _parse_guangyang_predelivery(sheets: list[_Sheet]) -> tuple[list[ImportedRow], date | None, list[str]]:
    mapping = {"source_no": 1, "model": 2, "drawing_number": 3, "stock_code": 4,
               "category": 5, "requested_quantity": 6, "unit_price": 7, "amount": 8}
    rows: list[ImportedRow] = []
    warnings: list[str] = []
    delivery_date: date | None = None
    for sheet in sheets:
        text = _sheet_text(sheet)
        sample_codes = sum(1 for number in range(1, min(sheet.max_row, 40) + 1)
                           if re.fullmatch(r"C?\d{6,}", sheet.text(number, 4), re.IGNORECASE))
        if sample_codes < 3:
            continue
        if sheet.hidden:
            warnings.append(f"隐藏工作表“{sheet.name}”未自动导入")
            continue
        delivery_date = delivery_date or _date_value(text[:500])
        for number in range(1, sheet.max_row + 1):
            parsed = _row(sheet, number, mapping, document_type="pre_delivery")
            if parsed:
                rows.append(parsed)
    if not rows:
        raise ExcelImportError("未识别到光洋预送货有效明细")
    return rows, delivery_date, warnings


def parse_excel_document(
    content: bytes,
    filename: str,
    *,
    document_type: Literal["order", "pre_delivery"],
    expected_customer_code: str | None = None,
) -> ImportedDocument:
    if not content:
        raise ExcelImportError("Excel 文件为空")
    if len(content) > MAX_FILE_BYTES:
        raise ExcelImportError("Excel 文件超过 20MB 限制")
    safe_name = _safe_filename(filename)
    source_format, sheets = _read_sheets(bytes(content))
    nonempty = [sheet for sheet in sheets if any(cell.display_value for row in sheet.rows for cell in row)]
    if not nonempty:
        raise ExcelImportError("Excel 没有可读取内容")
    all_text = " ".join(_sheet_text(sheet) for sheet in nonempty)
    source_hash = hashlib.sha256(content).hexdigest()
    skipped = [sheet.name for sheet in sheets if sheet not in nonempty]
    warnings: list[str] = []
    customer_name_evidence: str | None = None
    customer_po: str | None = None
    source_date: date | None = None
    delivery_date: date | None = None

    if document_type == "order":
        if "研光" in all_text or "YKE" in all_text.upper():
            template = "yanguang_purchase_order_v1"
            customer_code = "YG"
            customer_name_evidence = "工作簿内容包含研光/YKE"
            rows, source_date, customer_po, extra = _parse_yanguang_order(nonempty)
            warnings.extend(extra)
        else:
            raise ExcelImportError("订单 Excel 模板未识别，请先进行列映射")
    elif "光洋" in all_text or any(
        sum(1 for number in range(1, min(sheet.max_row, 40) + 1)
            if re.fullmatch(r"C\d{6,}", sheet.text(number, 4), re.IGNORECASE)) >= 3
        for sheet in nonempty
    ):
        template = "guangyang_pre_delivery_v1"
        customer_code = "GY"
        customer_name_evidence = "工作簿内容或C前缀品目号符合光洋模板"
        rows, delivery_date, extra = _parse_guangyang_predelivery(nonempty)
        warnings.extend(extra)
    elif "研光" in all_text or "捷太格特" in all_text:
        template = "yanguang_pre_delivery_v1"
        customer_code = "YG"
        customer_name_evidence = "工作簿内容包含研光/捷太格特"
        rows, delivery_date, extra = _parse_yanguang_predelivery(nonempty)
        warnings.extend(extra)
    else:
        raise ExcelImportError("预送货 Excel 模板未识别，请选择客户并进行列映射")

    if expected_customer_code and expected_customer_code.strip().upper() != customer_code:
        raise ExcelImportError(
            f"所选客户 {expected_customer_code.strip().upper()} 与文件识别客户 {customer_code} 不一致"
        )
    return ImportedDocument(
        document_type=document_type,
        source_format=source_format,
        template=template,
        filename=safe_name,
        source_hash=source_hash,
        parser_version=PARSER_VERSION,
        customer_code=customer_code,
        customer_name_evidence=customer_name_evidence,
        source_date=source_date,
        delivery_date=delivery_date,
        customer_po=customer_po,
        rows=rows,
        skipped_sheets=skipped,
        warnings=warnings,
    )
