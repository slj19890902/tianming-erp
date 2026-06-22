from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker


def test_phase5_migration_script_can_run_directly() -> None:
    project_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "migrate_phase5_orders.py"),
            "--help",
        ],
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--dry-run" in result.stdout


def test_phase5_schema_migration_preserves_legacy_orders(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                order_no TEXT NOT NULL,
                customer_id INTEGER NOT NULL,
                product_archive_id INTEGER,
                order_quantity INTEGER,
                sale_unit_price REAL
            );
            INSERT INTO orders VALUES (4, 'SO20260529132650852', 3, 3, 200, 1.5);
            """
        )
        connection.commit()

    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "phase5-migration-test")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        legacy_row = connection.execute(
            "SELECT id, order_no, order_quantity FROM orders"
        ).fetchone()

    assert {"sales_orders", "sales_order_items", "order_daily_sequences"} <= tables
    assert legacy_row == (4, "SO20260529132650852", 200)
    migration_source = next(
        (Path(__file__).resolve().parents[1] / "alembic" / "versions").glob(
            "*phase5*"
        )
    ).read_text(encoding="utf-8")
    assert "drop_table('orders')" not in migration_source
    assert 'drop_table("orders")' not in migration_source


def test_old_order_migration_is_dry_run_safe_and_idempotent(
    tmp_path: Path,
) -> None:
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.customer import Customer
    from app.models.migration import MigrationEntityMap
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from scripts.migrate_phase5_orders import migrate_phase5_orders

    database_path = tmp_path / "phase5.sqlite3"
    engine = create_sqlite_engine(database_path)
    Base.metadata.create_all(engine)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                order_no TEXT NOT NULL,
                customer_id INTEGER NOT NULL,
                product_archive_id INTEGER,
                customer_po TEXT,
                style_no TEXT,
                product_name TEXT,
                material TEXT,
                length_mm REAL,
                width_mm REAL,
                height_mm REAL,
                order_quantity INTEGER,
                sale_unit_price REAL,
                status TEXT,
                delivery_due_date TEXT,
                remark TEXT,
                created_at TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO orders VALUES (
                4, 'SO20260529132650852', 1, 3, 'KH-001', 'SME-001',
                '五层加强纸箱', 'K=A-BC', 520, 350, 300, 200, 3.6,
                'pending_material', '2026-06-20', '旧订单', '2026-05-29 13:26:50'
            )
            """
        )
        connection.commit()

    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        customer = Customer(
            id=1,
            customer_number=1,
            customer_code="SME",
            name="苏州思迈尔包装有限公司",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        product = Product(
            id=10,
            customer_id=1,
            product_code="SME-001",
            customer_material_code="KH-001",
            product_name="五层加强纸箱",
            legacy_material_text="K=A-BC",
            length_mm=Decimal("520"),
            width_mm=Decimal("350"),
            height_mm=Decimal("300"),
            box_category="normal",
        )
        session.add_all([customer, product])
        session.flush()
        session.add(
            MigrationEntityMap(
                source_system="legacy_product_archives",
                entity_type="product",
                source_id="3",
                target_table="products",
                target_id=product.id,
            )
        )
        session.commit()

    report_path = tmp_path / "migration_report_phase5.log"
    dry_run = migrate_phase5_orders(
        database_path=database_path,
        report_path=report_path,
        dry_run=True,
    )
    assert dry_run.orders_created == 1
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 0

    first = migrate_phase5_orders(
        database_path=database_path,
        report_path=report_path,
        dry_run=False,
    )
    second = migrate_phase5_orders(
        database_path=database_path,
        report_path=report_path,
        dry_run=False,
    )

    assert first.orders_created == 1
    assert second.orders_created == 0
    assert second.orders_skipped == 1
    with session_factory() as session:
        order = session.scalar(select(Order))
        items = session.scalars(select(OrderItem)).all()
        mapping = session.scalar(
            select(MigrationEntityMap).where(
                MigrationEntityMap.source_system == "legacy_orders",
                MigrationEntityMap.source_id == "4",
            )
        )

    assert order is not None
    assert order.order_number == "SO20260529132650852"
    assert order.total_amount == Decimal("720.00")
    assert len(items) == 1
    assert items[0].product_id == 10
    assert mapping is not None
    assert report_path.exists()
