from __future__ import annotations

import argparse
import json
import math
import re
import sys
import tempfile
import zipfile
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

SCRIPT_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_PROJECT_ROOT))

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import load_settings, normalize_path
from app.core.database import create_engine_from_settings
from app.models.historical_requisition import HistoricalRequisitionMap
from app.models.product import Product
from scripts.master_data_write_guard import reject_legacy_master_data_write_if_versioned


NUMBER = r"\d+(?:\.\d+)?"
PAIR_RE = re.compile(rf"^\s*({NUMBER})\s*[*xX×]\s*({NUMBER})\s*$")
MULTI_SIZE_RE = re.compile(rf"^\s*{NUMBER}(?:\s*[*xX×]\s*{NUMBER}){{2,}}\s*$")
MATERIAL_RE = re.compile(r"^[A-Z0-9][A-Z0-9./_-]{2,24}$", re.IGNORECASE)
HEADER_WORDS = (
    "苏州工业园区",
    "采购单",
    "送货单",
    "to:",
    "合计",
    "材质",
    "规格",
)


@dataclass(frozen=True, slots=True)
class HistoricalRecord:
    search_key: str
    cardboard_length: float
    cardboard_width: float
    score_lines: str | None
    quantity: int | None
    material_code: str
    record_date: date | None
    sheet_name: str
    row_number: int
    raw_data: str | None = None


@dataclass(frozen=True, slots=True)
class ProductCandidate:
    id: int
    product_code: str
    product_name: str


def write_windows_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8-sig")


def write_json_report(path: Path, payload: dict[str, Any]) -> None:
    write_windows_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2),
    )


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value).strip()


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        numeric = float(value)
        return None if math.isnan(numeric) else numeric
    text = _text(value)
    if re.fullmatch(NUMBER, text):
        return float(text)
    return None


def _date_value(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value)
    for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def normalize_search_key(value: str) -> str:
    normalized = value.upper().replace("×", "*").replace("X", "*")
    return re.sub(r"\s+", "", normalized)


def _looks_like_material(text: str) -> bool:
    if not MATERIAL_RE.fullmatch(text):
        return False
    if PAIR_RE.fullmatch(text) or MULTI_SIZE_RE.fullmatch(text):
        return False
    upper = text.upper()
    if upper in {"TO", "NO", "PCS"}:
        return False
    return bool(re.search(r"[A-Z]", upper)) and (
        bool(re.search(r"\d", upper)) or "/" in upper
    )


def _search_key(values: list[Any], material_index: int) -> str | None:
    start = 8 if len(values) > 8 else material_index + 1
    candidates: list[str] = []
    for value in values[start:]:
        text = _text(value)
        lower = text.lower()
        if (
            not text
            or _date_value(value)
            or any(word.lower() in lower for word in HEADER_WORDS)
            or _looks_like_material(text)
            or PAIR_RE.fullmatch(text)
            or MULTI_SIZE_RE.fullmatch(text)
        ):
            continue
        has_identity = bool(re.search(r"[\u4e00-\u9fffA-Za-z]", text))
        has_product_pattern = bool(
            re.search(r"\d{5,}", text)
            or re.search(rf"{NUMBER}\s*[*xX×]\s*{NUMBER}", text)
        )
        if has_identity or has_product_pattern:
            candidates.append(text)
    return max(candidates, key=len) if candidates else None


def parse_row(
    values: Iterable[Any],
    *,
    sheet_name: str,
    row_number: int,
    inherited_date: date | None = None,
) -> HistoricalRecord | None:
    row = list(values)
    joined = " ".join(_text(value) for value in row if _text(value))
    if not joined or any(word.lower() in joined.lower() for word in HEADER_WORDS):
        return None

    cardboard_length: float | None = None
    cardboard_width: float | None = None
    for value in row[:4]:
        match = PAIR_RE.fullmatch(_text(value))
        if match:
            cardboard_length = float(match.group(1))
            cardboard_width = float(match.group(2))
            break
    if cardboard_length is None:
        first = _number(row[0]) if row else None
        second = _number(row[1]) if len(row) > 1 else None
        if first is not None and second is not None:
            cardboard_length, cardboard_width = first, second
    if cardboard_length is None or cardboard_width is None:
        return None

    score_lines = next(
        (
            _text(value).replace("×", "*").replace("X", "*")
            for value in row
            if MULTI_SIZE_RE.fullmatch(_text(value))
        ),
        None,
    )
    material_index = -1
    material_code: str | None = None
    for index, value in enumerate(row[:10]):
        text = _text(value)
        if _looks_like_material(text):
            material_index = index
            material_code = text.upper()
            break
    if material_code is None:
        return None

    search_key = _search_key(row, material_index)
    if not search_key:
        return None

    quantity: int | None = None
    for value in reversed(row[2:material_index]):
        numeric = _number(value)
        if numeric is not None and numeric >= 1 and float(numeric).is_integer():
            quantity = int(numeric)
            break
    row_date = next(
        (parsed for value in row if (parsed := _date_value(value)) is not None),
        inherited_date,
    )
    return HistoricalRecord(
        search_key=search_key,
        cardboard_length=cardboard_length,
        cardboard_width=cardboard_width,
        score_lines=score_lines,
        quantity=quantity,
        material_code=material_code,
        record_date=row_date,
        sheet_name=sheet_name,
        row_number=row_number,
        raw_data=json.dumps(
            [_text(value) or None for value in row],
            ensure_ascii=False,
        ),
    )


def deduplicate_latest(
    records: Iterable[HistoricalRecord],
) -> list[HistoricalRecord]:
    selected: dict[str, HistoricalRecord] = {}
    for record in records:
        key = normalize_search_key(record.search_key)
        current = selected.get(key)
        record_sort = (
            record.record_date or date.min,
            record.sheet_name,
            record.row_number,
        )
        current_sort = (
            current.record_date or date.min,
            current.sheet_name,
            current.row_number,
        ) if current else None
        if current is None or record_sort > current_sort:
            selected[key] = record
    return sorted(selected.values(), key=lambda item: normalize_search_key(item.search_key))


def repair_workbook_copy(source: Path, temp_dir: Path) -> Path:
    source = normalize_path(source)
    repaired = temp_dir / f"{source.stem}_repaired.xlsx"
    with zipfile.ZipFile(source, "r") as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    styles_name = "xl/styles.xml"
    if styles_name in members:
        styles = members[styles_name].decode("utf-8")

        def clamp_family(match: re.Match[str]) -> str:
            value = min(int(match.group(2)), 14)
            return f"{match.group(1)}{value}{match.group(3)}"

        styles = re.sub(
            r'(<family\b[^>]*\bval=")(\d+)("[^>]*/>)',
            clamp_family,
            styles,
        )
        members[styles_name] = styles.encode("utf-8")
    with zipfile.ZipFile(repaired, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return repaired


def load_records(workbook_path: Path, *, all_sheets: bool = True) -> list[HistoricalRecord]:
    try:
        import pandas as pd
    except ImportError as error:
        raise RuntimeError("缺少 pandas，请先执行 pip install -r requirements.txt") from error

    with tempfile.TemporaryDirectory(prefix="boxerp-xlsx-") as temp_name:
        repaired = repair_workbook_copy(workbook_path, Path(temp_name))
        records: list[HistoricalRecord] = []
        with pd.ExcelFile(repaired, engine="openpyxl") as workbook:
            preferred = [
                name for name in workbook.sheet_names if "2020.1-2025" in name
            ]
            sheet_names = workbook.sheet_names if all_sheets else preferred
            if not sheet_names:
                sheet_names = workbook.sheet_names[:1]

            for sheet_name in sheet_names:
                frame = pd.read_excel(
                    workbook,
                    sheet_name=sheet_name,
                    header=None,
                    dtype=object,
                )
                inherited_date: date | None = None
                for row_index, row in frame.iterrows():
                    values = row.tolist()
                    dates = [
                        parsed
                        for value in values
                        if (parsed := _date_value(value)) is not None
                    ]
                    if dates:
                        inherited_date = max(dates)
                    record = parse_row(
                        values,
                        sheet_name=sheet_name,
                        row_number=int(row_index) + 1,
                        inherited_date=inherited_date,
                    )
                    if record:
                        records.append(record)
    return deduplicate_latest(records)


def _product_match(
    record: HistoricalRecord,
    products: list[ProductCandidate],
) -> ProductCandidate | None:
    haystack = normalize_search_key(record.search_key)
    exact_codes = [
        product
        for product in products
        if product.product_code
        and len(normalize_search_key(product.product_code)) >= 4
        and normalize_search_key(product.product_code) in haystack
    ]
    if exact_codes:
        return max(exact_codes, key=lambda product: len(product.product_code))
    names = [
        product
        for product in products
        if product.product_name
        and len(normalize_search_key(product.product_name)) >= 4
        and normalize_search_key(product.product_name) in haystack
    ]
    return max(names, key=lambda product: len(product.product_name)) if names else None


def preview_matches(
    records: list[HistoricalRecord],
    database_path: Path,
) -> tuple[list[tuple[HistoricalRecord, int | None]], int]:
    settings = replace(
        load_settings(),
        database_path=normalize_path(database_path),
    )
    engine = create_engine_from_settings(settings)
    with Session(engine) as session:
        products = [
            ProductCandidate(
                id=row.id,
                product_code=row.product_code or "",
                product_name=row.product_name or "",
            )
            for row in session.execute(
                select(Product.id, Product.product_code, Product.product_name)
            )
        ]
    engine.dispose()
    matches = [
        (record, matched.id if (matched := _product_match(record, products)) else None)
        for record in records
    ]
    return matches, sum(product_id is not None for _, product_id in matches)


def commit_matches(
    matches: list[tuple[HistoricalRecord, int | None]],
    *,
    workbook_path: Path,
    database_path: Path,
) -> tuple[int, int]:
    settings = replace(
        load_settings(),
        database_path=normalize_path(database_path),
    )
    engine = create_engine_from_settings(settings)
    updated = 0
    fallback = 0
    try:
        with Session(engine) as session:
            reject_legacy_master_data_write_if_versioned(
                session,
                script_name="scripts/import_historical_requisitions.py",
            )
            with session.begin():
                for record, product_id in matches:
                    if product_id is not None:
                        product = session.get(Product, product_id)
                        if product is None:
                            raise RuntimeError(f"产品不存在: {product_id}")
                        product.default_cardboard_length = Decimal(str(record.cardboard_length))
                        product.default_cardboard_width = Decimal(str(record.cardboard_width))
                        product.default_score_lines = record.score_lines
                        product.default_material_code = record.material_code
                        updated += 1
                        continue
                    normalized = normalize_search_key(record.search_key)
                    existing = session.scalar(
                        select(HistoricalRequisitionMap).where(
                            HistoricalRequisitionMap.normalized_search_key == normalized
                        )
                    )
                    values = {
                        "search_key": record.search_key,
                        "normalized_search_key": normalized,
                        "cardboard_length": Decimal(str(record.cardboard_length)),
                        "cardboard_width": Decimal(str(record.cardboard_width)),
                        "score_lines": record.score_lines,
                        "material_code": record.material_code,
                        "quantity": record.quantity,
                        "record_date": record.record_date,
                        "source_workbook": workbook_path.name,
                        "source_sheet": record.sheet_name,
                        "source_row": record.row_number,
                        "raw_data": record.raw_data,
                    }
                    if existing:
                        for key, value in values.items():
                            setattr(existing, key, value)
                    else:
                        session.add(HistoricalRequisitionMap(**values))
                    fallback += 1
    finally:
        engine.dispose()
    return updated, fallback


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="导入纸板历史报料模板。")
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--preview-limit", type=int, default=20)
    parser.add_argument(
        "--preview-output",
        type=Path,
        help="可选的 Windows 预览文本输出路径，使用 UTF-8 BOM 编码。",
    )
    parser.add_argument("--primary-sheet-only", action="store_true")
    parser.add_argument("--commit", action="store_true")
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("import_requisition_report.log"),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    workbook_path = normalize_path(args.workbook)
    database_path = normalize_path(
        args.database or load_settings().database_path
    )
    records = load_records(
        workbook_path,
        all_sheets=not args.primary_sheet_only,
    )
    matches, matched_count = preview_matches(records, database_path)

    preview_lines = [
        "序号 | 综合标识 | 纸板长 | 纸板宽 | 压线 | 材质 | 数量 | 日期 | 产品ID"
    ]
    for index, (record, product_id) in enumerate(
        matches[: max(args.preview_limit, 0)],
        1,
    ):
        preview_lines.append(
            f"{index} | {record.search_key} | {record.cardboard_length:g} | "
            f"{record.cardboard_width:g} | {record.score_lines or ''} | "
            f"{record.material_code} | {record.quantity or ''} | "
            f"{record.record_date or ''} | {product_id or ''}"
        )
    preview_text = "\n".join(preview_lines)
    print(preview_text)
    if args.preview_output:
        write_windows_text(args.preview_output, preview_text + "\n")

    updated = fallback = 0
    if args.commit:
        updated, fallback = commit_matches(
            matches,
            workbook_path=workbook_path,
            database_path=database_path,
        )
    report = {
        "workbook": str(workbook_path),
        "database": str(database_path),
        "unique_records": len(records),
        "matched_products": matched_count,
        "unmatched_records": len(records) - matched_count,
        "committed": args.commit,
        "products_updated": updated,
        "fallback_rows_written": fallback,
    }
    write_json_report(args.report, report)
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
