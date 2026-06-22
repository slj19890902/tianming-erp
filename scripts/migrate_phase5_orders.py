from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload, sessionmaker

from app.core.config import load_settings, normalize_path
from app.core.database import create_sqlite_engine
from app.models.customer import Customer
from app.models.migration import MigrationEntityMap
from app.models.order import Order, OrderItem
from app.models.product import Product


DEFAULT_REPORT_PATH = PROJECT_ROOT / "migration_report_phase5.log"
SOURCE_SYSTEM = "legacy_orders"
MONEY_QUANTUM = Decimal("0.00")


@dataclass(slots=True)
class MigrationStats:
    dry_run: bool
    legacy_rows: int = 0
    orders_created: int = 0
    orders_skipped: int = 0
    conflicts: int = 0
    mappings_created: int = 0


def _value(row: sqlite3.Row, name: str, default: Any = None) -> Any:
    return row[name] if name in row.keys() else default


def _decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default


def _date(value: Any, default: date | None = None) -> date | None:
    if value is None or value == "":
        return default
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return default


def _datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    text_value = str(value).replace("T", " ")
    try:
        return datetime.fromisoformat(text_value)
    except ValueError:
        return None


def _status(value: Any) -> str:
    source = str(value or "").strip().lower()
    if source in {"cancelled", "void", "invalid", "已作废"}:
        return "cancelled"
    if source in {"delivered", "completed", "已送货", "已完成"}:
        return "delivered"
    if source in {"partial_delivery", "partially_delivered", "部分送货"}:
        return "partially_delivered"
    if source in {"pending_delivery", "ready_delivery", "待送货"}:
        return "pending_delivery"
    if source in {"production", "in_production", "材料已到", "生产中"}:
        return "production"
    return "pending_production"


def _spec(row: sqlite3.Row, product: Product) -> str | None:
    dimensions = [
        _value(row, "length_mm", product.length_mm),
        _value(row, "width_mm", product.width_mm),
        _value(row, "height_mm", product.height_mm),
    ]
    if any(value is None or value == "" for value in dimensions):
        return None
    values = []
    for value in dimensions:
        number = _decimal(value)
        values.append(format(number, "f").rstrip("0").rstrip(".") or "0")
    return "×".join(values) + "mm"


def _legacy_rows(database_path: Path) -> list[sqlite3.Row]:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='orders'"
        ).fetchone()
        if table is None:
            return []
        return connection.execute("SELECT * FROM orders ORDER BY id").fetchall()


def _mapped_product(session: Session, row: sqlite3.Row) -> Product | None:
    archive_id = _value(row, "product_archive_id")
    if archive_id is not None:
        mapping = session.scalar(
            select(MigrationEntityMap).where(
                MigrationEntityMap.source_system == "legacy_product_archives",
                MigrationEntityMap.entity_type == "product",
                MigrationEntityMap.source_id == str(archive_id),
            )
        )
        if mapping is not None:
            product = session.scalar(
                select(Product)
                .options(joinedload(Product.material))
                .where(Product.id == mapping.target_id)
            )
            if product is not None:
                return product

    customer_id = _value(row, "customer_id")
    candidates = [
        str(_value(row, "style_no") or "").strip(),
        str(_value(row, "customer_material_code") or "").strip(),
    ]
    candidates = [value for value in candidates if value]
    if customer_id is None or not candidates:
        return None
    return session.scalar(
        select(Product)
        .options(joinedload(Product.material))
        .where(
            Product.customer_id == int(customer_id),
            or_(
                Product.product_code.in_(candidates),
                Product.customer_material_code.in_(candidates),
            ),
        )
        .order_by(Product.id)
    )


def _write_report(
    report_path: Path,
    *,
    stats: MigrationStats,
    messages: list[str],
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        "\n".join(
            [
                f"phase5 migration {datetime.now().isoformat(timespec='seconds')}",
                json.dumps(asdict(stats), ensure_ascii=False, sort_keys=True),
                *messages,
                "",
            ]
        ),
        encoding="utf-8",
    )


def migrate_phase5_orders(
    *,
    database_path: Path,
    report_path: Path = DEFAULT_REPORT_PATH,
    dry_run: bool = True,
) -> MigrationStats:
    database_path = normalize_path(database_path)
    report_path = normalize_path(report_path)
    rows = _legacy_rows(database_path)
    stats = MigrationStats(dry_run=dry_run, legacy_rows=len(rows))
    messages: list[str] = []
    engine = create_sqlite_engine(database_path)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory() as session:
        # SQLite may otherwise treat the first SAVEPOINT as the top-level
        # transaction, making a dry-run rollback ineffective.
        session.connection().exec_driver_sql("BEGIN")
        for row in rows:
            source_id = str(_value(row, "id"))
            try:
                with session.begin_nested():
                    existing_mapping = session.scalar(
                        select(MigrationEntityMap).where(
                            MigrationEntityMap.source_system == SOURCE_SYSTEM,
                            MigrationEntityMap.entity_type == "order",
                            MigrationEntityMap.source_id == source_id,
                        )
                    )
                    if existing_mapping is not None:
                        stats.orders_skipped += 1
                        continue

                    order_number = str(
                        _value(row, "order_no")
                        or _value(row, "order_number")
                        or f"LEGACY-{source_id}"
                    ).strip()
                    existing_order = session.scalar(
                        select(Order).where(Order.order_number == order_number)
                    )
                    if existing_order is not None:
                        session.add(
                            MigrationEntityMap(
                                source_system=SOURCE_SYSTEM,
                                entity_type="order",
                                source_id=source_id,
                                target_table="sales_orders",
                                target_id=existing_order.id,
                            )
                        )
                        stats.orders_skipped += 1
                        stats.mappings_created += 1
                        continue

                    customer_id = int(_value(row, "customer_id") or 0)
                    if session.get(Customer, customer_id) is None:
                        raise ValueError(f"客户不存在 customer_id={customer_id}")
                    product = _mapped_product(session, row)
                    if product is None:
                        raise ValueError(
                            "无法映射产品 "
                            f"product_archive_id={_value(row, 'product_archive_id')!r}"
                        )
                    if product.customer_id != customer_id:
                        raise ValueError(
                            f"产品客户不匹配 product_id={product.id} "
                            f"customer_id={customer_id}"
                        )

                    quantity = int(
                        _value(row, "order_quantity")
                        or _value(row, "quantity")
                        or 0
                    )
                    unit_price = _decimal(
                        _value(row, "sale_unit_price"),
                        _decimal(_value(row, "unit_price")),
                    )
                    if quantity <= 0:
                        raise ValueError(f"订单数量无效 quantity={quantity}")
                    if unit_price < 0:
                        raise ValueError(f"订单单价无效 unit_price={unit_price}")
                    subtotal = (
                        Decimal(quantity) * unit_price
                    ).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
                    created_at = _datetime(_value(row, "created_at"))
                    order_date = _date(
                        _value(row, "order_date"),
                        created_at.date() if created_at else date.today(),
                    )
                    received_at = _datetime(
                        _value(row, "material_received_at")
                        or _value(row, "material_arrived_at")
                    )

                    order = Order(
                        order_number=order_number,
                        customer_id=customer_id,
                        customer_po=(
                            str(_value(row, "customer_po") or "").strip() or None
                        ),
                        order_date=order_date or date.today(),
                        delivery_date=_date(
                            _value(row, "delivery_due_date")
                            or _value(row, "delivery_date")
                        ),
                        status=_status(_value(row, "status")),
                        payment_status=(
                            "paid"
                            if str(_value(row, "payment_status") or "").lower()
                            in {"paid", "received", "已结清", "已收款"}
                            else "unpaid"
                        ),
                        total_amount=subtotal,
                        remark=str(_value(row, "remark") or "").strip() or None,
                        created_at=created_at or datetime.now(),
                    )
                    session.add(order)
                    session.flush()
                    material = (
                        str(_value(row, "material") or "").strip()
                        or (
                            product.material.code
                            if product.material is not None
                            else product.legacy_material_text
                        )
                    )
                    session.add(
                        OrderItem(
                            order_id=order.id,
                            product_id=product.id,
                            quantity=quantity,
                            unit_price=unit_price,
                            subtotal=subtotal,
                            material_status=(
                                "received" if received_at is not None else "pending"
                            ),
                            material_received_at=received_at,
                            snapshot_product_name=(
                                str(_value(row, "product_name") or "").strip()
                                or product.product_name
                            ),
                            snapshot_spec=_spec(row, product),
                            snapshot_material=material or None,
                        )
                    )
                    session.add(
                        MigrationEntityMap(
                            source_system=SOURCE_SYSTEM,
                            entity_type="order",
                            source_id=source_id,
                            target_table="sales_orders",
                            target_id=order.id,
                        )
                    )
                    stats.orders_created += 1
                    stats.mappings_created += 1
            except Exception as error:
                stats.conflicts += 1
                messages.append(
                    f"ORDER_CONFLICT source_id={source_id} error={error}"
                )

        if dry_run:
            session.rollback()
        else:
            session.commit()

    _write_report(report_path, stats=stats, messages=messages)
    return stats


def main() -> int:
    current = load_settings()
    parser = argparse.ArgumentParser(
        description="Safely migrate legacy single-line orders into Phase 5 tables.",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=current.database_path,
        help="Preview SQLite database containing both legacy and Phase 5 tables.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT_PATH,
        help="Migration report path.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Roll back all inserts after validating every legacy row.",
    )
    args = parser.parse_args()
    stats = migrate_phase5_orders(
        database_path=args.database,
        report_path=args.report,
        dry_run=args.dry_run,
    )
    print(json.dumps(asdict(stats), ensure_ascii=False, indent=2))
    return 0 if stats.conflicts == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
