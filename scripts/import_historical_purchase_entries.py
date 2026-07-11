from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

SCRIPT_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_PROJECT_ROOT))

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.core.config import load_settings, normalize_path
from app.core.database import create_engine_from_settings
from app.models.historical_purchase import HistoricalPurchaseEntry
from app.models.product import Product
from app.services.historical_purchase_lookup import (
    DEFAULT_SHEET_NAME,
    HistoricalPurchaseRecord,
    load_historical_purchase_records,
    normalize_lookup_text,
)


@dataclass(frozen=True, slots=True)
class ProductCandidate:
    id: int
    customer_id: int
    product_code: str
    product_name: str


@dataclass(frozen=True, slots=True)
class ProductMatchIndex:
    by_code: dict[str, tuple[ProductCandidate, ...]]
    code_lengths: tuple[int, ...]
    non_ascii_codes: tuple[tuple[str, ProductCandidate], ...]


@dataclass(frozen=True, slots=True)
class ImportSummary:
    workbook: str
    database: str
    sheet: str
    parsed_rows: int
    inserted_rows: int
    updated_rows: int
    unchanged_rows: int
    stale_database_rows: int
    matched_products: int
    ambiguous_product_matches: int
    unmatched_products: int
    committed: bool


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record_fingerprint(record: HistoricalPurchaseRecord) -> str:
    payload = asdict(record)
    payload["record_date"] = (
        record.record_date.isoformat() if record.record_date else None
    )
    payload["board_length_cm"] = str(record.board_length_cm)
    payload["board_width_cm"] = str(record.board_width_cm)
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_product_match_index(
    products: list[ProductCandidate],
) -> ProductMatchIndex:
    grouped: dict[str, list[ProductCandidate]] = {}
    non_ascii: list[tuple[str, ProductCandidate]] = []
    for product in products:
        code = normalize_lookup_text(product.product_code)
        if len(code) < 3:
            continue
        grouped.setdefault(code, []).append(product)
        if not code.isascii():
            non_ascii.append((code, product))
    return ProductMatchIndex(
        by_code={key: tuple(value) for key, value in grouped.items()},
        code_lengths=tuple(sorted({len(key) for key in grouped}, reverse=True)),
        non_ascii_codes=tuple(non_ascii),
    )


def match_product(
    record: HistoricalPurchaseRecord,
    index: ProductMatchIndex,
) -> tuple[ProductCandidate | None, bool]:
    haystack = normalize_lookup_text(record.search_text)
    normalized_tokens = {
        normalize_lookup_text(token)
        for token in re.findall(r"[A-Za-z0-9._/-]+", record.search_text)
        if token
    }
    matched_codes: set[str] = set()
    for token in normalized_tokens:
        for length in index.code_lengths:
            if length > len(token):
                continue
            for start in range(0, len(token) - length + 1):
                candidate = token[start : start + length]
                if candidate in index.by_code:
                    matched_codes.add(candidate)
    for code, _product in index.non_ascii_codes:
        if code in haystack:
            matched_codes.add(code)
    code_matches = [
        product
        for code in matched_codes
        for product in index.by_code[code]
    ]
    if not code_matches:
        return None, False
    longest = max(
        len(normalize_lookup_text(product.product_code))
        for product in code_matches
    )
    strongest = [
        product
        for product in code_matches
        if len(normalize_lookup_text(product.product_code)) == longest
    ]
    unique_ids = {product.id for product in strongest}
    if len(unique_ids) == 1:
        return strongest[0], False

    named = [
        product
        for product in strongest
        if len(normalize_lookup_text(product.product_name)) >= 3
        and normalize_lookup_text(product.product_name) in haystack
    ]
    if len({product.id for product in named}) == 1:
        return named[0], False
    return None, True


def entry_values(
    record: HistoricalPurchaseRecord,
    *,
    workbook_name: str,
    file_hash: str,
    matched_product: ProductCandidate | None,
) -> dict[str, Any]:
    searchable = " | ".join(
        value
        for value in (
            record.search_text,
            record.product_reference,
            record.material_code,
            record.supplier_name or "",
        )
        if value
    )
    return {
        "source_workbook": workbook_name,
        "source_sheet": DEFAULT_SHEET_NAME,
        "source_row": record.source_row,
        "source_file_sha256": file_hash,
        "source_fingerprint": record_fingerprint(record),
        "supplier_name": record.supplier_name,
        "record_date": record.record_date,
        "product_reference": record.product_reference,
        "search_text": record.search_text,
        "normalized_search_text": normalize_lookup_text(searchable),
        "material_code": record.material_code,
        "historical_quantity": record.historical_quantity,
        "report_length_mm": record.report_length_mm,
        "report_width_mm": record.report_width_mm,
        "crease_text": record.crease_text,
        "crease_type": record.crease_type,
        "crease_left_mm": record.crease_left_mm,
        "crease_middle_mm": record.crease_middle_mm,
        "crease_right_mm": record.crease_right_mm,
        "notes": record.notes,
        "product_id": matched_product.id if matched_product else None,
        "customer_id": matched_product.customer_id if matched_product else None,
    }


def _changed(existing: HistoricalPurchaseEntry, values: dict[str, Any]) -> bool:
    if existing.source_fingerprint != values["source_fingerprint"]:
        return True
    return (
        existing.product_id != values["product_id"]
        or existing.customer_id != values["customer_id"]
    )


def import_historical_purchase_entries(
    workbook_path: Path,
    database_path: Path,
    *,
    commit: bool,
) -> ImportSummary:
    workbook_path = normalize_path(workbook_path)
    database_path = normalize_path(database_path)
    records = list(load_historical_purchase_records(workbook_path))
    source_hash = file_sha256(workbook_path)

    settings = replace(load_settings(), database_path=database_path)
    engine = create_engine_from_settings(settings)
    if not inspect(engine).has_table(HistoricalPurchaseEntry.__tablename__):
        engine.dispose()
        raise RuntimeError(
            "数据库缺少 historical_purchase_entries，请先运行对应 migration"
        )

    inserted = updated = unchanged = matched = ambiguous = 0
    with Session(engine) as session:
        products = [
            ProductCandidate(
                id=row.id,
                customer_id=row.customer_id,
                product_code=row.product_code or "",
                product_name=row.product_name or "",
            )
            for row in session.scalars(
                select(Product).where(Product.deleted_at.is_(None))
            )
        ]
        product_index = build_product_match_index(products)
        existing_rows = {
            row.source_row: row
            for row in session.scalars(
                select(HistoricalPurchaseEntry).where(
                    HistoricalPurchaseEntry.source_workbook == workbook_path.name,
                    HistoricalPurchaseEntry.source_sheet == DEFAULT_SHEET_NAME,
                )
            )
        }
        source_rows: set[int] = set()
        for record in records:
            source_rows.add(record.source_row)
            product, is_ambiguous = match_product(record, product_index)
            matched += int(product is not None)
            ambiguous += int(is_ambiguous)
            values = entry_values(
                record,
                workbook_name=workbook_path.name,
                file_hash=source_hash,
                matched_product=product,
            )
            existing = existing_rows.get(record.source_row)
            if existing is None:
                inserted += 1
                if commit:
                    session.add(HistoricalPurchaseEntry(**values))
                continue
            if not _changed(existing, values):
                unchanged += 1
                continue
            updated += 1
            if commit:
                for key, value in values.items():
                    setattr(existing, key, value)

        stale = len(set(existing_rows) - source_rows)
        if commit:
            session.commit()
    engine.dispose()
    return ImportSummary(
        workbook=str(workbook_path),
        database=str(database_path),
        sheet=DEFAULT_SHEET_NAME,
        parsed_rows=len(records),
        inserted_rows=inserted,
        updated_rows=updated,
        unchanged_rows=unchanged,
        stale_database_rows=stale,
        matched_products=matched,
        ambiguous_product_matches=ambiguous,
        unmatched_products=len(records) - matched,
        committed=commit,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="将人工维护的历史采购工作表导入 ERP 搜索表。"
    )
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--report", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    database_path = args.database or load_settings().database_path
    summary = import_historical_purchase_entries(
        args.workbook,
        database_path,
        commit=args.commit,
    )
    payload = asdict(summary)
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    print(content)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(content + "\n", encoding="utf-8-sig")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
