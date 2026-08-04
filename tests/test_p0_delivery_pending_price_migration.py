from __future__ import annotations

from datetime import date
from decimal import Decimal
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

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
        customer = Customer(
            name="迁移匿名客户",
            payment_term_days=0,
            statement_cycle_start_day=20,
            credit_limit=Decimal("0"),
            delivery_method="配送",
            default_tax_rate=Decimal("0.13"),
            status="active",
            is_active=True,
            version=1,
        )
        db.add(customer)
        db.flush()
        product = Product(
            customer_id=customer.id,
            product_code="P0-MIG-NOPRICE",
            customer_material_code="P0-MIG-NOPRICE",
            product_name="迁移待定价纸箱",
            box_category="normal",
            box_style="普通箱",
        )
        db.add(product)
        db.flush()
        delivery = Delivery(
            delivery_number="P0-MIG-DELIVERY",
            customer_id=customer.id,
            delivery_date=date(2026, 8, 4),
            source_mode="unordered_finished",
            status="pending",
            total_quantity=1,
        )
        db.add(delivery)
        db.flush()
        db.add(
            DeliveryItem(
                delivery_id=delivery.id,
                source_type="unordered_finished",
                product_id=product.id,
                product_code_snapshot=product.product_code,
                product_name_snapshot=product.product_name,
                unit_snapshot="只",
                unit_price_snapshot=None,
                price_source="pending",
                delivered_quantity=1,
                ordered_quantity_snapshot=0,
                order_remaining_snapshot=0,
                over_delivery_quantity=0,
            )
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
