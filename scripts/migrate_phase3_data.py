from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import load_settings, normalize_path
from app.core.database import create_sqlite_engine
from app.models.customer import Customer
from app.models.material import Material
from app.models.migration import MigrationEntityMap
from app.models.product import Product


DEFAULT_BOXERP_DATABASE = Path(r"Z:\sata1-18015598002\BoxERP\erp.db")
DEFAULT_REPORT_PATH = Path(__file__).resolve().parents[1] / "migration_report_phase3.log"
SOURCE_SYSTEM = "legacy_product_archives"


@dataclass(slots=True)
class MigrationStats:
    dry_run: bool
    customers_updated: int = 0
    customer_conflicts: int = 0
    materials_created: int = 0
    materials_skipped: int = 0
    products_created: int = 0
    products_skipped: int = 0
    product_conflicts: int = 0
    mappings_created: int = 0


def normalize_key(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def decimal_value(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def positive_decimal_value(value: Any) -> Decimal | None:
    decimal = decimal_value(value)
    if decimal is None or decimal <= 0:
        return None
    return decimal


def date_value(value: Any) -> date | None:
    if not value:
        return None
    return date.fromisoformat(str(value)[:10])


def process_fields(process_note: str | None) -> dict[str, str]:
    values: dict[str, str] = {}
    for segment in (process_note or "").split(";"):
        if "=" not in segment:
            continue
        key, value = segment.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def detect_box_category(
    *,
    process_note: str | None,
    die_cut_path: str | None,
    product_name: str | None,
    style_no: str | None,
) -> str:
    process = process_fields(process_note)
    explicit = process.get("die_cut_required", "").casefold()
    if explicit == "true":
        return "die_cut"
    if explicit == "false":
        return "normal"
    if die_cut_path:
        return "die_cut"
    description = f"{product_name or ''} {style_no or ''}"
    return "die_cut" if re.search(r"模切|刀卡|异型|啤盒", description) else "normal"


def _source_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro",
        uri=True,
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    return connection


def _write_report(
    report_path: Path,
    *,
    stats: MigrationStats,
    messages: list[str],
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"phase3 migration {datetime.now().isoformat(timespec='seconds')}",
        json.dumps(asdict(stats), ensure_ascii=False, sort_keys=True),
        *messages,
        "",
    ]
    report_path.write_text("\n".join(lines), encoding="utf-8")


def _update_customers(
    session: Session,
    source: sqlite3.Connection,
    stats: MigrationStats,
    messages: list[str],
) -> None:
    source_rows = source.execute(
        """
        SELECT customer_code, customer_name, default_payment_term,
               credit_limit, customer_number
        FROM customers
        """
    ).fetchall()
    source_by_name = {
        normalize_key(row["customer_name"]): row for row in source_rows
    }
    customers = session.scalars(select(Customer).order_by(Customer.id)).all()
    used_numbers = {
        customer.customer_number
        for customer in customers
        if customer.customer_number is not None
    }
    used_codes = {
        normalize_key(customer.customer_code)
        for customer in customers
        if customer.customer_code
    }
    next_number = max(
        [int(row["customer_number"] or 0) for row in source_rows]
        + [int(number) for number in used_numbers]
        + [0]
    ) + 1

    for customer in customers:
        source_row = source_by_name.get(normalize_key(customer.name))
        number = (
            int(source_row["customer_number"])
            if source_row and source_row["customer_number"] is not None
            else customer.customer_number
        )
        code = (
            str(source_row["customer_code"]).strip()
            if source_row and source_row["customer_code"]
            else customer.customer_code
        )
        if number is None:
            while next_number in used_numbers:
                next_number += 1
            number = next_number
            next_number += 1
        if not code:
            code = f"LEGACY-{customer.id}"

        normalized_code = normalize_key(code)
        number_owner = next(
            (
                item
                for item in customers
                if item.id != customer.id
                and item.customer_number == number
            ),
            None,
        )
        code_owner = next(
            (
                item
                for item in customers
                if item.id != customer.id
                and normalize_key(item.customer_code) == normalized_code
            ),
            None,
        )
        if number_owner or code_owner:
            stats.customer_conflicts += 1
            messages.append(
                f"CUSTOMER_CONFLICT id={customer.id} name={customer.name!r} "
                f"number={number!r} code={code!r}"
            )
            continue

        changed = False
        for attribute, value in (
            ("customer_number", number),
            ("customer_code", code),
            (
                "payment_term_days",
                int(source_row["default_payment_term"] or 0)
                if source_row
                else customer.payment_term_days,
            ),
            (
                "credit_limit",
                decimal_value(source_row["credit_limit"]) or Decimal("0")
                if source_row
                else customer.credit_limit,
            ),
        ):
            if getattr(customer, attribute) != value:
                setattr(customer, attribute, value)
                changed = True
        if changed:
            stats.customers_updated += 1
        used_numbers.add(number)
        used_codes.add(normalized_code)


def _import_materials(
    session: Session,
    source: sqlite3.Connection,
    stats: MigrationStats,
) -> dict[str, Material]:
    materials_by_code = {
        normalize_key(material.code): material
        for material in session.scalars(select(Material)).all()
    }
    rows = source.execute(
        """
        SELECT code, paper_composition, layer_count, flute_type,
               basis_weight_description, quote_price, price_unit,
               supplier_name, quote_date, remarks
        FROM materials
        ORDER BY material_id
        """
    ).fetchall()
    for row in rows:
        key = normalize_key(row["code"])
        if key in materials_by_code:
            stats.materials_skipped += 1
            continue
        material = Material(
            code=str(row["code"]).strip(),
            paper_composition=row["paper_composition"],
            layer_count=row["layer_count"],
            flute_type=row["flute_type"],
            basis_weight_description=row["basis_weight_description"],
            quote_price=decimal_value(row["quote_price"]),
            price_unit=row["price_unit"],
            supplier_name=row["supplier_name"],
            quote_date=date_value(row["quote_date"]),
            remarks=row["remarks"],
        )
        session.add(material)
        session.flush()
        materials_by_code[key] = material
        stats.materials_created += 1
    return materials_by_code


def _archive_rows(session: Session) -> list[dict[str, Any]]:
    available_columns = {
        row[1]
        for row in session.execute(
            text("PRAGMA table_info(product_archives)")
        ).all()
    }
    requested_columns = (
        "id",
        "customer_id",
        "style_no",
        "customer_po",
        "product_name",
        "unit",
        "length_mm",
        "width_mm",
        "height_mm",
        "material",
        "flute_type",
        "layer_count",
        "process_note",
        "last_sale_unit_price",
        "sale_unit_price_no_tax",
        "last_cost_unit_price",
        "die_cut_path",
        "remark",
        "print_color",
        "craft_requirements",
    )
    select_columns = [
        column if column in available_columns else f"NULL AS {column}"
        for column in requested_columns
    ]
    result = session.execute(
        text(
            f"SELECT {', '.join(select_columns)} "
            "FROM product_archives ORDER BY id"
        )
    ).mappings()
    return [dict(row) for row in result]


def _import_products(
    session: Session,
    materials_by_code: dict[str, Material],
    stats: MigrationStats,
    messages: list[str],
) -> None:
    mapped_source_ids = set(
        session.scalars(
            select(MigrationEntityMap.source_id).where(
                MigrationEntityMap.source_system == SOURCE_SYSTEM,
                MigrationEntityMap.entity_type == "product",
            )
        ).all()
    )
    customer_ids = set(session.scalars(select(Customer.id)).all())
    for row in _archive_rows(session):
        source_id = str(row["id"])
        if source_id in mapped_source_ids:
            stats.products_skipped += 1
            continue
        if row["customer_id"] not in customer_ids:
            stats.product_conflicts += 1
            messages.append(
                f"PRODUCT_CONFLICT archive_id={source_id} "
                f"missing_customer_id={row['customer_id']}"
            )
            continue

        style_no = str(row["style_no"] or "").strip()
        if not style_no:
            stats.product_conflicts += 1
            messages.append(f"PRODUCT_CONFLICT archive_id={source_id} empty_style_no")
            continue
        process = process_fields(row.get("process_note"))
        material_text = str(row.get("material") or "").strip() or None
        material = materials_by_code.get(normalize_key(material_text))
        product = Product(
            customer_id=row["customer_id"],
            product_code=style_no,
            customer_material_code=style_no,
            product_name=str(row.get("product_name") or style_no).strip(),
            material_id=material.id if material else None,
            legacy_material_text=material_text,
            length_mm=positive_decimal_value(row.get("length_mm")),
            width_mm=positive_decimal_value(row.get("width_mm")),
            height_mm=positive_decimal_value(row.get("height_mm")),
            box_category=detect_box_category(
                process_note=row.get("process_note"),
                die_cut_path=row.get("die_cut_path"),
                product_name=row.get("product_name"),
                style_no=style_no,
            ),
            box_style=process.get("box_type"),
            print_content=process.get("print_layout"),
            printing_colors=row.get("print_color") or process.get("color"),
            production_process=row.get("craft_requirements")
            or row.get("process_note"),
            unit=str(row.get("unit") or "只"),
            sale_unit_price=decimal_value(row.get("last_sale_unit_price")),
            sale_unit_price_no_tax=decimal_value(
                row.get("sale_unit_price_no_tax")
            ),
            cost_unit_price=decimal_value(row.get("last_cost_unit_price")),
            die_cut_path=row.get("die_cut_path"),
            remark=row.get("remark"),
        )
        try:
            with session.begin_nested():
                session.add(product)
                session.flush()
                session.add(
                    MigrationEntityMap(
                        source_system=SOURCE_SYSTEM,
                        entity_type="product",
                        source_id=source_id,
                        target_table="products",
                        target_id=product.id,
                    )
                )
                session.flush()
            stats.products_created += 1
            stats.mappings_created += 1
            mapped_source_ids.add(source_id)
        except IntegrityError as error:
            stats.product_conflicts += 1
            messages.append(
                f"PRODUCT_CONFLICT archive_id={source_id} "
                f"customer_id={row['customer_id']} style_no={style_no!r} "
                f"error={error.orig}"
            )


def migrate_phase3_data(
    *,
    target_database: Path,
    boxerp_database: Path,
    report_path: Path,
    dry_run: bool,
) -> MigrationStats:
    target = normalize_path(target_database)
    source_path = normalize_path(boxerp_database)
    report = normalize_path(report_path)
    if not target.is_file():
        raise FileNotFoundError(f"目标数据库不存在: {target}")
    if not source_path.is_file():
        raise FileNotFoundError(f"BoxERP 数据库不存在: {source_path}")

    engine = create_sqlite_engine(target)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    stats = MigrationStats(dry_run=dry_run)
    messages: list[str] = []
    with closing(_source_connection(source_path)) as source, session_factory() as session:
        try:
            _update_customers(session, source, stats, messages)
            materials_by_code = _import_materials(session, source, stats)
            _import_products(session, materials_by_code, stats, messages)
            if dry_run:
                session.rollback()
            else:
                session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            _write_report(report, stats=stats, messages=messages)
    engine.dispose()
    return stats


def parse_args() -> argparse.Namespace:
    settings = load_settings()
    parser = argparse.ArgumentParser(
        description="Phase 3 customer/material/product migration",
    )
    parser.add_argument(
        "--target-db",
        type=Path,
        default=settings.database_path,
        help="Migrated ERP SQLite database",
    )
    parser.add_argument(
        "--boxerp-db",
        type=Path,
        default=DEFAULT_BOXERP_DATABASE,
        help="Read-only BoxERP SQLite database containing 46 materials",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT_PATH,
        help="Conflict and summary log path",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run all parsing and inserts, then roll back the target transaction",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    stats = migrate_phase3_data(
        target_database=args.target_db,
        boxerp_database=args.boxerp_db,
        report_path=args.report,
        dry_run=args.dry_run,
    )
    print(json.dumps(asdict(stats), ensure_ascii=False, indent=2))
    print(f"迁移报告: {normalize_path(args.report)}")
    if args.dry_run:
        print("DRY RUN：目标数据库未提交任何变更。")


if __name__ == "__main__":
    main()
