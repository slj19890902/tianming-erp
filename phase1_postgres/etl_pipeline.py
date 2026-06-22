from __future__ import annotations

import argparse
import logging
import math
import os
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base, Customer, Product


SOURCE_DB_URL = os.getenv(
    "SOURCE_DB_URL",
    "mssql+pyodbc://erp_reader:password@127.0.0.1/BoxDB20_REPRO?driver=ODBC+Driver+17+for+SQL+Server",
)
TARGET_DB_URL = os.getenv(
    "TARGET_DB_URL",
    "postgresql+psycopg2://postgres:password@NAS_IP:5432/tm_erp_db",
)

LEGACY_SOURCE = "BoxDB20"
UNKNOWN_CUSTOMER_CODE = "UNKNOWN-HISTORY"
UNKNOWN_CUSTOMER_NAME = "未知客户/历史归档"

LOGGER = logging.getLogger("tm_erp_etl")


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if not text or text.upper() in {"NULL", "NONE", "NAN"}:
        return None
    return " ".join(text.split())


def clean_int(value: Any, default: int | None = None) -> int | None:
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def clean_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if number.is_nan() or number.is_infinite():
        return None
    return number


def first_present(row: pd.Series, candidates: Iterable[str]) -> Any:
    for name in candidates:
        if name in row.index:
            return row[name]
    return None


def make_unique(base: str, used: set[str], fallback_prefix: str, legacy_id: int | str | None) -> str:
    candidate = clean_text(base) or f"{fallback_prefix}-{legacy_id or len(used) + 1}"
    original = candidate
    counter = 2
    while candidate in used:
        if legacy_id is not None and counter == 2:
            candidate = f"{original}-{legacy_id}"
        else:
            candidate = f"{original}-{counter}"
        counter += 1
    used.add(candidate)
    return candidate


def transform_customers(source_df: pd.DataFrame) -> tuple[list[dict[str, Any]], dict[int, int]]:
    rows: list[dict[str, Any]] = [
        {
            "legacy_source": LEGACY_SOURCE,
            "legacy_id": "UNKNOWN",
            "customer_number": 0,
            "customer_code": UNKNOWN_CUSTOMER_CODE,
            "name": UNKNOWN_CUSTOMER_NAME,
            "short_name": "历史归档",
            "contact_person": None,
            "phone": None,
            "mobile": None,
            "address": None,
            "payment_term_days": 30,
            "delivery_method": "配送",
            "note": "ETL orphan fallback customer. Do not delete.",
            "is_active": True,
        }
    ]
    used_codes = {UNKNOWN_CUSTOMER_CODE}
    used_names = {UNKNOWN_CUSTOMER_NAME}
    legacy_map: dict[int, int] = {}

    for _, row in source_df.iterrows():
        legacy_id = clean_int(first_present(row, ["ID", "id"]))
        if legacy_id is None:
            LOGGER.warning("Skipping customer without legacy ID: %s", row.to_dict())
            continue

        raw_code = first_present(row, ["编码", "客户编号", "Code", "CustomerCode", "customer_code"])
        raw_name = first_present(row, ["名称", "客户名称", "全称", "Name", "CustomerName", "name"])
        raw_short = first_present(row, ["简称", "ShortName", "short_name"])

        code = make_unique(clean_text(raw_code) or f"CUST-{legacy_id}", used_codes, "CUST", legacy_id)
        name = make_unique(clean_text(raw_name) or f"未命名客户-{legacy_id}", used_names, "未命名客户", legacy_id)

        rows.append(
            {
                "legacy_source": LEGACY_SOURCE,
                "legacy_id": str(legacy_id),
                "customer_number": legacy_id,
                "customer_code": code,
                "name": name,
                "short_name": clean_text(raw_short),
                "contact_person": clean_text(first_present(row, ["联系人", "Contact", "contact_person"])),
                "phone": clean_text(first_present(row, ["电话", "Phone", "phone"])),
                "mobile": clean_text(first_present(row, ["手机", "Mobile", "mobile"])),
                "address": clean_text(first_present(row, ["地址", "Address", "address"])),
                "payment_term_days": clean_int(first_present(row, ["账期", "PaymentTermDays"]), 30) or 30,
                "delivery_method": "配送",
                "note": clean_text(first_present(row, ["备注", "Note", "note"])),
                "is_active": True,
            }
        )
        legacy_map[legacy_id] = legacy_id

    return rows, legacy_map


def transform_products(
    source_df: pd.DataFrame,
    customer_legacy_id_map: dict[int, int],
    unknown_customer_id: int,
) -> tuple[list[dict[str, Any]], dict[int, int | None]]:
    rows: list[dict[str, Any]] = []
    legacy_map: dict[int, int | None] = {}
    seen_codes: set[tuple[int, str]] = set()
    seen_material_codes: set[tuple[int, str]] = set()

    for _, row in source_df.iterrows():
        legacy_id = clean_int(first_present(row, ["ID", "id"]))
        if legacy_id is None:
            LOGGER.warning("Skipping product without legacy ID: %s", row.to_dict())
            continue

        legacy_customer_id = clean_int(first_present(row, ["客户_FK", "客户FK", "CustomerID", "customer_id", "PID"]))
        customer_id = customer_legacy_id_map.get(legacy_customer_id or -1, unknown_customer_id)

        product_code = clean_text(first_present(row, ["客户料号", "存货编码", "品号", "Code", "product_code"]))
        if not product_code:
            product_code = f"LEGACY-{legacy_id}"
        while (customer_id, product_code) in seen_codes:
            product_code = f"{product_code}-{legacy_id}"
        seen_codes.add((customer_id, product_code))

        customer_material_code = clean_text(
            first_present(row, ["客户物料编码", "客户料号", "存货编码", "品号", "customer_material_code"])
        )
        if not customer_material_code:
            customer_material_code = product_code
        while (customer_id, customer_material_code) in seen_material_codes:
            customer_material_code = f"{customer_material_code}-{legacy_id}"
        seen_material_codes.add((customer_id, customer_material_code))

        length_mm = clean_decimal(first_present(row, ["长", "长度", "length_mm", "L"]))
        width_mm = clean_decimal(first_present(row, ["宽", "宽度", "width_mm", "W"]))
        height_mm = clean_decimal(first_present(row, ["高", "高度", "height_mm", "H"]))
        cardboard_length = clean_decimal(first_present(row, ["纸长", "default_cardboard_length_mm"]))
        cardboard_width = clean_decimal(first_present(row, ["纸宽", "default_cardboard_width_mm"]))
        unit_price = clean_decimal(first_present(row, ["单价", "平方价", "default_unit_price"]))

        rows.append(
            {
                "legacy_source": LEGACY_SOURCE,
                "legacy_id": str(legacy_id),
                "customer_id": customer_id,
                "product_code": product_code,
                "customer_material_code": customer_material_code,
                "product_name": clean_text(first_present(row, ["品名", "产品名称", "Name", "product_name"]))
                or f"未命名常用箱-{legacy_id}",
                "default_material_text": clean_text(first_present(row, ["材质", "材料", "material", "default_material_text"])),
                "length_mm": length_mm,
                "width_mm": width_mm,
                "height_mm": height_mm,
                "box_category": "die_cut"
                if clean_text(first_present(row, ["模切", "box_category"])) in {"1", "true", "True", "模切", "die_cut"}
                else "normal",
                "box_style": clean_text(first_present(row, ["箱型", "box_style"])),
                "production_process": clean_text(first_present(row, ["工艺", "production_process"])),
                "default_score_line": clean_text(first_present(row, ["yaxian", "压线", "default_score_line"])),
                "default_cardboard_length_mm": cardboard_length,
                "default_cardboard_width_mm": cardboard_width,
                "default_pieces_per_sheet": clean_int(first_present(row, ["拼版数量", "pieces_per_sheet"]), 1) or 1,
                "default_unit_price": unit_price,
                "note": clean_text(first_present(row, ["备注", "Note", "note"])),
                "is_active": True,
            }
        )
        legacy_map[legacy_id] = None

    return rows, legacy_map


def build_source_engine(source_db_url: str = SOURCE_DB_URL) -> Engine:
    return create_engine(source_db_url, pool_pre_ping=True)


def build_target_engine(target_db_url: str = TARGET_DB_URL) -> Engine:
    return create_engine(target_db_url, pool_pre_ping=True)


def create_target_schema(target_engine: Engine) -> None:
    Base.metadata.create_all(target_engine)


def read_table_in_chunks(engine: Engine, table_name: str, chunksize: int = 2000):
    query = f"SELECT * FROM {table_name}"
    yield from pd.read_sql_query(query, engine, chunksize=chunksize)


def upsert_customers(session: Session, rows: list[dict[str, Any]]) -> dict[int, int]:
    legacy_to_new: dict[int, int] = {}
    for data in rows:
        existing = session.scalar(
            select(Customer).where(
                Customer.legacy_source == data["legacy_source"],
                Customer.legacy_id == data["legacy_id"],
            )
        )
        if existing is None:
            existing = Customer(**data)
            session.add(existing)
            session.flush()
        else:
            for key, value in data.items():
                setattr(existing, key, value)
            session.flush()

        legacy_id = clean_int(data["legacy_id"])
        if legacy_id is not None:
            legacy_to_new[legacy_id] = existing.id

    unknown = session.scalar(
        select(Customer).where(
            Customer.legacy_source == LEGACY_SOURCE,
            Customer.legacy_id == "UNKNOWN",
        )
    )
    if unknown is None:
        raise RuntimeError("Unknown fallback customer was not created.")
    legacy_to_new.setdefault(0, unknown.id)
    return legacy_to_new


def upsert_products(session: Session, rows: list[dict[str, Any]]) -> dict[int, int]:
    legacy_to_new: dict[int, int] = {}
    for data in rows:
        existing = session.scalar(
            select(Product).where(
                Product.legacy_source == data["legacy_source"],
                Product.legacy_id == data["legacy_id"],
            )
        )
        if existing is None:
            existing = Product(**data)
            session.add(existing)
            session.flush()
        else:
            for key, value in data.items():
                setattr(existing, key, value)
            session.flush()

        legacy_id = clean_int(data["legacy_id"])
        if legacy_id is not None:
            legacy_to_new[legacy_id] = existing.id
    return legacy_to_new


def run_etl(
    source_db_url: str = SOURCE_DB_URL,
    target_db_url: str = TARGET_DB_URL,
    chunksize: int = 2000,
    dry_run: bool = True,
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    source_engine = build_source_engine(source_db_url)
    target_engine = build_target_engine(target_db_url)
    create_target_schema(target_engine)
    SessionLocal = sessionmaker(target_engine, expire_on_commit=False)

    with SessionLocal() as session:
        all_customer_map: dict[int, int] = {}
        for chunk in read_table_in_chunks(source_engine, "Customers", chunksize=chunksize):
            rows, _ = transform_customers(chunk)
            customer_map = upsert_customers(session, rows)
            all_customer_map.update(customer_map)
            LOGGER.info("Prepared %s customer rows.", len(rows))

        unknown_customer = session.scalar(
            select(Customer).where(Customer.customer_code == UNKNOWN_CUSTOMER_CODE)
        )
        if unknown_customer is None:
            raise RuntimeError("Missing unknown fallback customer.")

        for chunk in read_table_in_chunks(source_engine, "OrderXLs_common", chunksize=chunksize):
            rows, _ = transform_products(chunk, all_customer_map, unknown_customer.id)
            upsert_products(session, rows)
            LOGGER.info("Prepared %s product rows.", len(rows))

        if dry_run:
            session.rollback()
            LOGGER.info("Dry run complete. Rolled back all target changes.")
        else:
            session.commit()
            LOGGER.info("ETL commit complete.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 1 BoxDB20 to PostgreSQL ETL pipeline.")
    parser.add_argument("--source-db-url", default=SOURCE_DB_URL)
    parser.add_argument("--target-db-url", default=TARGET_DB_URL)
    parser.add_argument("--chunksize", type=int, default=2000)
    parser.add_argument("--commit", action="store_true", help="Commit changes to PostgreSQL. Defaults to dry-run.")
    args = parser.parse_args()
    run_etl(
        source_db_url=args.source_db_url,
        target_db_url=args.target_db_url,
        chunksize=args.chunksize,
        dry_run=not args.commit,
    )


if __name__ == "__main__":
    main()
