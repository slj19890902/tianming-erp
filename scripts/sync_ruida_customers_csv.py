from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select
from sqlalchemy.orm import Session

from phase1_postgres.database import SessionLocal
from phase1_postgres.models import Customer


LEGACY_SOURCE = "BoxDB20"


def clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def clean_int(value: Any, default: int = 30) -> int:
    text = clean(value)
    if not text:
        return default
    try:
        return int(float(text))
    except ValueError:
        return default


def unique_value(session: Session, model_attr, value: str, current_id: int | None, suffix: str) -> str:
    candidate = value
    counter = 1
    while True:
        existing = session.scalar(select(Customer).where(model_attr == candidate))
        if existing is None or existing.id == current_id:
            return candidate
        counter += 1
        candidate = f"{value}-{suffix}" if counter == 2 else f"{value}-{suffix}-{counter}"


def find_existing_customer(session: Session, row: dict[str, str]) -> Customer | None:
    legacy_id = clean(row.get("legacy_customer_id"))
    if legacy_id:
        existing = session.scalar(
            select(Customer).where(Customer.legacy_source == LEGACY_SOURCE, Customer.legacy_id == legacy_id)
        )
        if existing is not None:
            return existing

    code = clean(row.get("customer_code"))
    if code:
        existing = session.scalar(select(Customer).where(Customer.customer_code == code))
        if existing is not None:
            return existing

    name = clean(row.get("customer_name"))
    if name:
        existing = session.scalar(select(Customer).where(Customer.name == name))
        if existing is not None:
            return existing

    short_name = clean(row.get("short_name"))
    if short_name and "天华" in short_name:
        existing = session.scalar(select(Customer).where(Customer.name.like("%天华%")))
        if existing is not None:
            return existing

    return None


def apply_customer_row(session: Session, row: dict[str, str]) -> str:
    legacy_id = clean(row.get("legacy_customer_id")) or "UNKNOWN"
    raw_code = clean(row.get("customer_code")) or f"RUIDA-{legacy_id}"
    raw_name = clean(row.get("customer_name")) or clean(row.get("short_name")) or f"瑞达客户-{legacy_id}"

    customer = find_existing_customer(session, row)
    action = "updated"
    if customer is None:
        customer = Customer(customer_code=raw_code, name=raw_name, delivery_method="配送", payment_term_days=30)
        session.add(customer)
        session.flush()
        action = "created"

    customer.customer_code = unique_value(session, Customer.customer_code, raw_code, customer.id, legacy_id)
    customer.name = unique_value(session, Customer.name, raw_name, customer.id, legacy_id)
    customer.short_name = clean(row.get("short_name"))
    customer.contact_person = clean(row.get("contact_person"))
    customer.phone = clean(row.get("phone"))
    customer.mobile = clean(row.get("mobile"))
    customer.address = clean(row.get("address"))
    customer.payment_term_days = clean_int(row.get("payment_term_days"), 30)
    customer.delivery_method = "配送"
    customer.invoice_title = clean(row.get("customer_name"))
    customer.tax_number = clean(row.get("tax_id"))
    customer.note = clean(row.get("source_json"))
    customer.customer_number = clean_int(legacy_id, 0) or None
    customer.legacy_source = LEGACY_SOURCE
    customer.legacy_id = legacy_id
    customer.is_active = clean(row.get("is_hidden")) not in {"1", "true", "True", "是"}
    session.flush()
    return action


def sync_customers(csv_path: Path, commit: bool) -> dict[str, int]:
    stats = {"source_rows": 0, "created": 0, "updated": 0}
    with SessionLocal() as session:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                stats["source_rows"] += 1
                action = apply_customer_row(session, row)
                stats[action] += 1
        if commit:
            session.commit()
        else:
            session.rollback()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Ruida Customers CSV into current ERP customers table.")
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--report", type=Path, default=PROJECT_ROOT / "migration-reports" / "ruida_customer_sandbox_sync.json")
    args = parser.parse_args()

    if not args.csv.exists():
        print(f"CSV not found: {args.csv}")
        return 2

    stats = sync_customers(args.csv, commit=args.commit)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8-sig")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    print(f"report: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
