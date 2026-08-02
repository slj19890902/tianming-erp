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
from app.models.delivery import Delivery


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "dh90v8x9z79"
TARGET_REVISION = "di91v8x9z80"
MIGRATION = PROJECT_ROOT / "alembic/versions/di91v8x9z80_mixed_delivery_source.py"


def _run_alembic(database_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        ERP_DATABASE_PATH=str(database_path),
        ERP_BACKUP_DIR=str(database_path.parent / "backups"),
        ERP_SECRET_KEY="q1-03-mixed-delivery-migration-test",
        ERP_ENVIRONMENT="test",
        PYTHONUTF8="1",
    )
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _must_run(database_path: Path, *arguments: str) -> None:
    result = _run_alembic(database_path, *arguments)
    assert result.returncode == 0, result.stdout + result.stderr


def _state(database_path: Path) -> tuple[str, str, list[tuple[object, ...]]]:
    with sqlite3.connect(database_path) as connection:
        return (
            str(connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]),
            str(connection.execute("PRAGMA integrity_check").fetchone()[0]),
            connection.execute("PRAGMA foreign_key_check").fetchall(),
        )


def test_mixed_delivery_migration_roundtrip_is_linear_and_clean(tmp_path: Path) -> None:
    database_path = tmp_path / "q1-03-mixed-roundtrip.sqlite3"
    _must_run(database_path, "upgrade", PREVIOUS_REVISION)
    assert _state(database_path) == (PREVIOUS_REVISION, "ok", [])

    _must_run(database_path, "upgrade", TARGET_REVISION)
    assert _state(database_path) == (TARGET_REVISION, "ok", [])

    _must_run(database_path, "downgrade", PREVIOUS_REVISION)
    assert _state(database_path) == (PREVIOUS_REVISION, "ok", [])

    _must_run(database_path, "upgrade", TARGET_REVISION)
    assert _state(database_path) == (TARGET_REVISION, "ok", [])


def test_mixed_delivery_fact_blocks_downgrade_before_ddl(tmp_path: Path) -> None:
    database_path = tmp_path / "q1-03-mixed-fail-closed.sqlite3"
    _must_run(database_path, "upgrade", TARGET_REVISION)

    engine = create_sqlite_engine(database_path)
    with Session(engine) as database:
        customer = Customer(
            name="Q1-03混合送货迁移匿名客户",
            payment_term_days=0,
            statement_cycle_start_day=20,
            credit_limit=Decimal("0"),
            delivery_method="配送",
            default_tax_rate=Decimal("0.13"),
            status="active",
            is_active=True,
            version=1,
        )
        database.add(customer)
        database.flush()
        database.add(
            Delivery(
                delivery_number="Q1-03-MIXED-MIGRATION-FACT",
                customer_id=customer.id,
                delivery_date=date(2026, 8, 2),
                source_mode="mixed",
                status="pending",
                total_quantity=0,
            )
        )
        database.commit()
    engine.dispose()

    before = _state(database_path)
    blocked = _run_alembic(database_path, "downgrade", PREVIOUS_REVISION)
    assert blocked.returncode != 0
    assert "存在订单待送与无订单成品库存混合送货事实，禁止降级" in (
        blocked.stdout + blocked.stderr
    )
    assert _state(database_path) == before == (TARGET_REVISION, "ok", [])


def test_mixed_delivery_migration_metadata_and_guard_order() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "di91v8x9z80"' in source
    assert 'down_revision: Union[str, Sequence[str], None] = "dh90v8x9z79"' in source
    assert source.split("def downgrade() -> None:", 1)[1].index("mixed_fact") < source.rindex("batch_alter_table")
