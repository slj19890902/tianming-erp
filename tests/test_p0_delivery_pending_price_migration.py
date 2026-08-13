from __future__ import annotations

from datetime import date
from decimal import Decimal
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import create_sqlite_engine
from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryItem
from app.models.product import Product


ROOT = Path(__file__).resolve().parents[1]
PREVIOUS = "di91v8x9z80"
TARGET = "dj92v8x9z81"


def _run(database: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        ERP_DATABASE_PATH=str(database),
        ERP_BACKUP_DIR=str(database.parent / "backups"),
        ERP_SECRET_KEY="p0-delivery-pending-price-migration",
        ERP_ENVIRONMENT="test",
        PYTHONUTF8="1",
    )
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _must(database: Path, *args: str) -> None:
    result = _run(database, *args)
    assert result.returncode == 0, result.stdout + result.stderr


def _state(database: Path) -> tuple[str, str, list[tuple], int]:
    with sqlite3.connect(database) as connection:
        return (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0],
            connection.execute("PRAGMA integrity_check").fetchone()[0],
            connection.execute("PRAGMA foreign_key_check").fetchall(),
            next(
                int(row[3])
                for row in connection.execute(
                    'PRAGMA table_info("delivery_pick_task_items")'
                )
                if row[1] == "order_item_id"
            ),
        )


def test_pending_price_constraint_migration_roundtrip_is_linear(tmp_path: Path) -> None:
    database = tmp_path / "pending-price-roundtrip.sqlite3"
    _must(database, "upgrade", PREVIOUS)
    assert _state(database) == (PREVIOUS, "ok", [], 1)
    _must(database, "upgrade", TARGET)
    assert _state(database) == (TARGET, "ok", [], 0)
    _must(database, "downgrade", PREVIOUS)
    assert _state(database) == (PREVIOUS, "ok", [], 1)
    _must(database, "upgrade", TARGET)
    assert _state(database) == (TARGET, "ok", [], 0)


def test_pending_price_fact_blocks_downgrade_before_ddl(tmp_path: Path) -> None:
    database = tmp_path / "pending-price-fail-closed.sqlite3"
    _must(database, "upgrade", TARGET)
    engine = create_sqlite_engine(database)
    with Session(engine) as db:
        customer_id = db.execute(
            text("INSERT INTO customers(name) VALUES ('迁移匿名客户') RETURNING id")
        ).scalar_one()
        product_id = db.execute(
            text(
                """INSERT INTO products(
                customer_id,product_code,customer_material_code,product_name,box_category
                ) VALUES (:customer_id,'P0-MIG-NOPRICE','P0-MIG-NOPRICE',
                '迁移待定价纸箱','normal') RETURNING id"""
            ),
            {"customer_id": customer_id},
        ).scalar_one()
        delivery_id = db.execute(
            text(
                """INSERT INTO sales_deliveries(
                delivery_number,customer_id,delivery_date,source_mode,status,total_quantity
                ) VALUES ('P0-MIG-DELIVERY',:customer_id,'2026-08-04',
                'unordered_finished','pending',1) RETURNING id"""
            ),
            {"customer_id": customer_id},
        ).scalar_one()
        db.execute(
            text(
                """INSERT INTO sales_delivery_items(
                delivery_id,source_type,product_id,product_code_snapshot,
                product_name_snapshot,unit_snapshot,unit_price_snapshot,price_source,
                delivered_quantity,ordered_quantity_snapshot,order_remaining_snapshot,
                over_delivery_quantity
                ) VALUES (:delivery_id,'unordered_finished',:product_id,
                'P0-MIG-NOPRICE','迁移待定价纸箱','只',NULL,'pending',1,0,0,0)"""
            ),
            {"delivery_id": delivery_id, "product_id": product_id},
        )
        db.commit()
    engine.dispose()

    before = _state(database)
    blocked = _run(database, "downgrade", PREVIOUS)
    assert blocked.returncode != 0
    assert "存在单价待补的无订单成品送货事实，禁止降级" in (
        blocked.stdout + blocked.stderr
    )
    assert _state(database) == before == (TARGET, "ok", [], 0)


def test_pending_price_migration_metadata_is_single_head() -> None:
    migration = (
        ROOT
        / "alembic/versions/dj92v8x9z81_unordered_delivery_price_pending.py"
    ).read_text(encoding="utf-8")
    assert 'revision: str = "dj92v8x9z81"' in migration
    assert 'down_revision: Union[str, Sequence[str], None] = "di91v8x9z80"' in migration
    downgrade = migration.split("def downgrade() -> None:", 1)[1]
    assert downgrade.index("pending_price_fact") < downgrade.index(
        "_replace_delivery_constraints"
    )
