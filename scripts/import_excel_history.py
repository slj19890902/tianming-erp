from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from phase1_postgres.database import SessionLocal
from phase1_postgres.models import Customer, Product


DEFAULT_CSV_NAME = "2025年采购单.xlsx - 2020.1-2025.csv"
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
PAIR_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[*xX×]\s*(\d+(?:\.\d+)?)\s*$")


@dataclass(frozen=True)
class ExcelHistoryRecord:
    search_keyword: str
    style_no: str | None
    paper_length_mm: Decimal | None
    paper_width_mm: Decimal | None
    score_line: str | None
    material_code: str | None
    quantity: int | None
    remark: str | None
    source_row: int


def text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value).strip().strip("'").strip('"')


def normalize_key(value: str) -> str:
    return re.sub(r"\s+", "", value.upper().replace("×", "*").replace("X", "*"))


def decimal_or_none(value: Any) -> Decimal | None:
    raw = text(value)
    if not raw:
        return None
    match = NUMBER_RE.search(raw)
    if not match:
        return None
    return Decimal(match.group(0)).quantize(Decimal("0.01"))


def int_or_none(value: Any) -> int | None:
    raw = text(value)
    if not raw:
        return None
    match = NUMBER_RE.search(raw)
    if not match:
        return None
    numeric = Decimal(match.group(0))
    if numeric < 0:
        return None
    return int(numeric)


def clean_score_line(value: Any) -> str | None:
    raw = text(value).replace("×", "*").replace("X", "*").replace("x", "*")
    return raw or None


def parse_paper_size(a_value: Any, b_value: Any) -> tuple[Decimal | None, Decimal | None]:
    a_text = text(a_value)
    match = PAIR_RE.fullmatch(a_text)
    if match:
        return Decimal(match.group(1)).quantize(Decimal("0.01")), Decimal(match.group(2)).quantize(Decimal("0.01"))
    return decimal_or_none(a_value), decimal_or_none(b_value)


def detect_encoding(csv_path: Path) -> str:
    sample = csv_path.read_bytes()[:4096]
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            sample.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    for encoding in ("gb18030", "gbk"):
        try:
            sample.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    return "utf-8-sig"


def read_csv(csv_path: Path) -> pd.DataFrame:
    encoding = detect_encoding(csv_path)
    return pd.read_csv(csv_path, header=None, dtype=object, encoding=encoding, keep_default_na=False)


def parse_csv(csv_path: Path) -> list[ExcelHistoryRecord]:
    df = read_csv(csv_path)
    dedup: dict[tuple[str, str], ExcelHistoryRecord] = {}
    for index, row in df.iterrows():
        values = list(row)
        # A/B/C/D/E/F/G/I/K columns by old Excel habit.
        a_value = values[0] if len(values) > 0 else ""
        b_value = values[1] if len(values) > 1 else ""
        c_value = values[2] if len(values) > 2 else ""
        d_value = values[3] if len(values) > 3 else ""
        e_value = values[4] if len(values) > 4 else ""
        f_value = values[5] if len(values) > 5 else ""
        g_value = values[6] if len(values) > 6 else ""
        i_value = values[8] if len(values) > 8 else ""
        k_value = values[10] if len(values) > 10 else ""

        keyword = text(k_value)
        material = text(e_value).upper()
        if not keyword or keyword.lower() in {"k列", "款号/规格", "综合搜索"}:
            continue
        if not material or material in {"材质", "MATERIAL"}:
            continue
        paper_length, paper_width = parse_paper_size(a_value, b_value)
        if paper_length is None and paper_width is None and not clean_score_line(c_value):
            continue

        record = ExcelHistoryRecord(
            search_keyword=keyword,
            style_no=text(g_value) or None,
            paper_length_mm=paper_length,
            paper_width_mm=paper_width,
            score_line=clean_score_line(c_value),
            material_code=material,
            quantity=int_or_none(d_value),
            remark=" / ".join(part for part in [text(f_value), text(i_value)] if part) or None,
            source_row=int(index) + 1,
        )
        dedup[(normalize_key(keyword), material)] = record
    return list(dedup.values())


def ensure_customer(session: Session, keyword: str) -> Customer:
    if re.match(r"^(213|232)", keyword.strip()):
        customer = session.scalar(select(Customer).where(Customer.name.like("%天华%")))
        if customer is not None:
            return customer
        customer = Customer(
            customer_code="THCJ",
            customer_number=None,
            name="天华超净",
            short_name="天华",
            delivery_method="配送",
            payment_term_days=30,
        )
        session.add(customer)
        session.flush()
        return customer

    customer = session.scalar(select(Customer).where(Customer.name == "历史归档"))
    if customer is not None:
        return customer
    customer = Customer(
        customer_code="HISTORY",
        customer_number=None,
        name="历史归档",
        short_name="历史",
        delivery_method="配送",
        payment_term_days=30,
    )
    session.add(customer)
    session.flush()
    return customer


def product_note(record: ExcelHistoryRecord) -> str:
    return json.dumps(
        {
            "source": "excel_history_2020_2025",
            "search_keyword": record.search_keyword,
            "style_no": record.style_no,
            "quantity": record.quantity,
            "remark": record.remark,
            "source_row": record.source_row,
        },
        ensure_ascii=False,
    )


def upsert_record(session: Session, record: ExcelHistoryRecord) -> str:
    customer = ensure_customer(session, record.search_keyword)
    product = None
    if record.style_no:
        product = session.scalar(
            select(Product).where(Product.customer_id == customer.id, Product.product_code == record.style_no)
        )
    if product is None:
        product = session.scalar(
            select(Product).where(
                Product.customer_id == customer.id,
                Product.historical_search_key == record.search_keyword,
                Product.historical_material_code == record.material_code,
            )
        )

    action = "updated"
    if product is None:
        base_code = record.style_no or re.sub(r"\W+", "", record.search_keyword)[:24] or f"HIST{record.source_row}"
        product_code = base_code
        suffix = 1
        while session.scalar(select(Product.id).where(Product.customer_id == customer.id, Product.product_code == product_code)):
            suffix += 1
            product_code = f"{base_code}-{suffix}"
        product = Product(
            customer_id=customer.id,
            product_code=product_code,
            customer_material_code=product_code,
            product_name=record.search_keyword,
            box_category="normal",
            default_pieces_per_sheet=1,
            is_active=True,
        )
        session.add(product)
        action = "created"

    product.product_name = record.search_keyword
    product.default_material_text = record.material_code
    product.default_cardboard_length_mm = record.paper_length_mm
    product.default_cardboard_width_mm = record.paper_width_mm
    product.default_score_line = record.score_line
    product.historical_search_key = record.search_keyword
    product.historical_style_no = record.style_no
    product.historical_material_code = record.material_code
    product.note = product_note(record)
    return action


def import_csv_to_products(csv_path: Path | str, session: Session, *, commit: bool = False) -> dict[str, Any]:
    path = Path(csv_path)
    records = parse_csv(path)
    summary: dict[str, Any] = {"total_records": len(records), "created": 0, "updated": 0, "preview": []}
    for record in records:
        action = upsert_record(session, record)
        session.flush()
        summary[action] += 1
        if len(summary["preview"]) < 20:
            summary["preview"].append(asdict(record) | {"action": action})
    if commit:
        session.commit()
    else:
        session.rollback()
    return summary


def write_report(path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8-sig")


def default_csv_path() -> Path:
    candidates = [
        PROJECT_ROOT / DEFAULT_CSV_NAME,
        PROJECT_ROOT / "data" / DEFAULT_CSV_NAME,
        PROJECT_ROOT / "migration-workfiles" / DEFAULT_CSV_NAME,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return PROJECT_ROOT / DEFAULT_CSV_NAME


def main() -> int:
    parser = argparse.ArgumentParser(description="Import 2020-2025 Excel purchase history into products.")
    parser.add_argument("--csv", type=Path, default=default_csv_path())
    parser.add_argument("--commit", action="store_true", help="Write changes. Omit for dry-run preview.")
    parser.add_argument("--report", type=Path, default=PROJECT_ROOT / "migration-reports" / "excel_history_import_report.json")
    args = parser.parse_args()

    if not args.csv.exists():
        print(f"CSV not found: {args.csv}")
        return 2
    with SessionLocal() as session:
        summary = import_csv_to_products(args.csv, session, commit=args.commit)
    write_report(args.report, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    print(f"report: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
