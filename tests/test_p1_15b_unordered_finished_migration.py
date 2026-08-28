from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "cz82v8x9z71"
TARGET_REVISION = "da83v8x9z72"
MIGRATION = (
    PROJECT_ROOT
    / "alembic/versions/da83v8x9z72_unordered_finished_delivery.py"
)
NEW_TABLES = {
    "unordered_finished_delivery_allocations",
    "unordered_finished_delivery_reversals",
}
DELIVERY_COLUMNS = {"source_mode"}
DELIVERY_ITEM_COLUMNS = {
    "source_type",
    "product_id",
    "product_code_snapshot",
    "product_name_snapshot",
    "specification_snapshot",
    "unit_snapshot",
    "unit_price_snapshot",
    "price_source",
}


def _run_alembic(
    database_path: Path,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["ERP_DATABASE_PATH"] = str(database_path)
    environment["ERP_BACKUP_DIR"] = str(database_path.parent / "backups")
    environment["ERP_SECRET_KEY"] = "q1-03-unordered-migration-test"
    environment["ERP_ENVIRONMENT"] = "test"
    environment["PYTHONUTF8"] = "1"
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


def _state(database_path: Path) -> dict[str, object]:
    with sqlite3.connect(database_path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        return {
            "revision": connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0],
            "integrity": connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0],
            "foreign_key_errors": connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall(),
            "tables": tables,
            "delivery_columns": {
                str(row[1])
                for row in connection.execute(
                    'PRAGMA table_info("sales_deliveries")'
                )
            },
            "delivery_item_columns": {
                str(row[1])
                for row in connection.execute(
                    'PRAGMA table_info("sales_delivery_items")'
                )
            },
        }


def test_unordered_finished_migration_roundtrip_is_linear_and_clean(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "q1-03-unordered-roundtrip.sqlite3"
    _must_run(database_path, "upgrade", PREVIOUS_REVISION)
    before = _state(database_path)
    assert before["revision"] == PREVIOUS_REVISION
    assert before["integrity"] == "ok"
    assert before["foreign_key_errors"] == []
    assert NEW_TABLES.isdisjoint(before["tables"])
    assert DELIVERY_COLUMNS.isdisjoint(before["delivery_columns"])
    assert DELIVERY_ITEM_COLUMNS.isdisjoint(before["delivery_item_columns"])

    _must_run(database_path, "upgrade", TARGET_REVISION)
    upgraded = _state(database_path)
    assert upgraded["revision"] == TARGET_REVISION
    assert upgraded["integrity"] == "ok"
    assert upgraded["foreign_key_errors"] == []
    assert NEW_TABLES <= upgraded["tables"]
    assert DELIVERY_COLUMNS <= upgraded["delivery_columns"]
    assert DELIVERY_ITEM_COLUMNS <= upgraded["delivery_item_columns"]

    _must_run(database_path, "downgrade", PREVIOUS_REVISION)
    downgraded = _state(database_path)
    assert downgraded["revision"] == PREVIOUS_REVISION
    assert downgraded["integrity"] == "ok"
    assert downgraded["foreign_key_errors"] == []
    assert NEW_TABLES.isdisjoint(downgraded["tables"])
    assert DELIVERY_COLUMNS.isdisjoint(downgraded["delivery_columns"])
    assert DELIVERY_ITEM_COLUMNS.isdisjoint(downgraded["delivery_item_columns"])

    _must_run(database_path, "upgrade", TARGET_REVISION)
    upgraded_again = _state(database_path)
    assert upgraded_again["revision"] == TARGET_REVISION
    assert upgraded_again["integrity"] == "ok"
    assert upgraded_again["foreign_key_errors"] == []
    assert NEW_TABLES <= upgraded_again["tables"]
    assert DELIVERY_COLUMNS <= upgraded_again["delivery_columns"]
    assert DELIVERY_ITEM_COLUMNS <= upgraded_again["delivery_item_columns"]


def test_unordered_finished_fact_blocks_downgrade_before_ddl(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "q1-03-unordered-fail-closed.sqlite3"
    _must_run(database_path, "upgrade", TARGET_REVISION)

    # Seed against the historical da83 schema itself.  Using today's ORM here
    # would add columns introduced by later revisions and turn schema drift
    # into a false failure of this downgrade guard.
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        customer_id = connection.execute(
            "INSERT INTO customers (name) VALUES (?)",
            ("Q1-03迁移匿名客户",),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO sales_deliveries (
                delivery_number, customer_id, delivery_date, source_mode,
                status, total_quantity
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "Q1-03-MIGRATION-FACT",
                customer_id,
                "2026-07-30",
                "unordered_finished",
                "pending",
                0,
            ),
        )
        connection.commit()

    before = _state(database_path)
    blocked = _run_alembic(database_path, "downgrade", PREVIOUS_REVISION)
    assert blocked.returncode != 0
    assert "存在无订单成品送货事实，禁止破坏性降级" in (
        blocked.stdout + blocked.stderr
    )

    after = _state(database_path)
    assert after == before
    assert after["revision"] == TARGET_REVISION
    assert after["integrity"] == "ok"
    assert after["foreign_key_errors"] == []
    assert NEW_TABLES <= after["tables"]
    assert DELIVERY_COLUMNS <= after["delivery_columns"]
    assert DELIVERY_ITEM_COLUMNS <= after["delivery_item_columns"]


def test_unordered_finished_migration_metadata_and_guard_order() -> None:
    migration = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "da83v8x9z72"' in migration
    assert (
        'down_revision: Union[str, Sequence[str], None] = "cz82v8x9z71"'
        in migration
    )
    downgrade = migration.split("def downgrade() -> None:", 1)[1]
    assert downgrade.index("unordered_facts") < downgrade.index("op.drop_index")
    assert downgrade.index("unordered_facts") < downgrade.index("op.drop_table")
