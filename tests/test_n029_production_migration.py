from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from app.models import Base


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "aw50v8x9y0s40"
TARGET_REVISION = "ax51v8x9z41"
PRODUCTION_TABLES = {
    "production_tasks",
    "production_completion_batches",
    "production_completions",
    "production_stock_transfers",
}
PREEXISTING_FACT_TABLES = {
    "sales_order_items",
    "inventory_lots",
    "inventory_movements",
}
DOWNGRADE_BLOCKED_MESSAGE = (
    "N029 production-managed tasks or facts exist; downgrade would lose workflow state"
)


def _alembic_config(monkeypatch: pytest.MonkeyPatch, database_path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database_path.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "n029-production-migration-test")
    return Config(str(PROJECT_ROOT / "alembic.ini"))


def _upgrade(
    monkeypatch: pytest.MonkeyPatch, database_path: Path, revision: str
) -> None:
    command.upgrade(_alembic_config(monkeypatch, database_path), revision)


def _downgrade(
    monkeypatch: pytest.MonkeyPatch, database_path: Path, revision: str
) -> None:
    command.downgrade(_alembic_config(monkeypatch, database_path), revision)


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _table_sql(connection: sqlite3.Connection, table_name: str) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    assert row is not None
    return row[0]


def _assert_database_health(
    connection: sqlite3.Connection, expected_revision: str
) -> None:
    assert connection.execute(
        "SELECT version_num FROM alembic_version"
    ).fetchone() == (expected_revision,)
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _unique_column_sets(table_name: str) -> set[frozenset[str]]:
    return {
        frozenset(column.name for column in constraint.columns)
        for constraint in Base.metadata.tables[table_name].constraints
        if isinstance(constraint, UniqueConstraint)
    }


def _foreign_key_deletes(table_name: str) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for constraint in Base.metadata.tables[table_name].constraints:
        if isinstance(constraint, ForeignKeyConstraint):
            element = next(iter(constraint.elements))
            result[element.parent.name] = element.ondelete
    return result


def _check_sql(table_name: str) -> str:
    return " ".join(
        str(constraint.sqltext)
        for constraint in Base.metadata.tables[table_name].constraints
        if isinstance(constraint, CheckConstraint)
    )


def test_n029_models_register_complete_contract_in_metadata() -> None:
    assert PRODUCTION_TABLES <= set(Base.metadata.tables)

    tasks = Base.metadata.tables["production_tasks"]
    assert set(tasks.columns.keys()) == {
        "id",
        "order_item_id",
        "status",
        "planned_quantity",
        "finished_coverage_snapshot",
        "readiness_basis",
        "ready_at",
        "version",
        "created_at",
        "updated_at",
    }
    assert frozenset({"order_item_id"}) in _unique_column_sets("production_tasks")
    assert _foreign_key_deletes("production_tasks") == {"order_item_id": "CASCADE"}
    task_checks = _check_sql("production_tasks")
    assert "planned_quantity >= 0" in task_checks
    assert "finished_coverage_snapshot >= 0" in task_checks
    assert "version >= 1" in task_checks
    assert "waiting_material" in task_checks and "not_required" in task_checks
    assert "planned_quantity = 0" in task_checks
    assert "planned_quantity > 0" in task_checks

    batches = Base.metadata.tables["production_completion_batches"]
    assert not batches.c.idempotency_key.nullable
    assert not batches.c.request_hash.nullable
    assert batches.c.request_hash.type.length == 64
    assert frozenset({"idempotency_key"}) in _unique_column_sets(
        "production_completion_batches"
    )
    assert _foreign_key_deletes("production_completion_batches") == {
        "completed_by": "SET NULL"
    }
    batch_checks = _check_sql("production_completion_batches")
    assert "length(request_hash) = 64" in batch_checks
    assert "item_count > 0" in batch_checks

    completions = Base.metadata.tables["production_completions"]
    assert frozenset({"order_item_id"}) in _unique_column_sets(
        "production_completions"
    )
    assert frozenset({"inventory_lot_id"}) in _unique_column_sets(
        "production_completions"
    )
    assert _foreign_key_deletes("production_completions") == {
        "batch_id": "RESTRICT",
        "task_id": "RESTRICT",
        "order_item_id": "RESTRICT",
        "warehouse_location_id": "RESTRICT",
        "inventory_lot_id": "RESTRICT",
        "completed_by": "SET NULL",
    }
    completion_checks = _check_sql("production_completions")
    assert "expected_version >= 1" in completion_checks
    assert "quantity > 0" in completion_checks
    assert "initial_disposition = 'direct'" in completion_checks
    assert "warehouse_location_id IS NULL" in completion_checks
    assert "inventory_lot_id IS NULL" in completion_checks
    assert "initial_disposition = 'stock'" in completion_checks
    assert "warehouse_location_id IS NOT NULL" in completion_checks

    transfers = Base.metadata.tables["production_stock_transfers"]
    assert not transfers.c.warehouse_location_id.nullable
    assert not transfers.c.inventory_lot_id.nullable
    assert not transfers.c.idempotency_key.nullable
    assert transfers.c.request_hash.type.length == 64
    transfer_uniques = _unique_column_sets("production_stock_transfers")
    assert frozenset({"completion_id"}) in transfer_uniques
    assert frozenset({"inventory_lot_id"}) in transfer_uniques
    assert frozenset({"idempotency_key"}) in transfer_uniques
    assert _foreign_key_deletes("production_stock_transfers") == {
        "completion_id": "RESTRICT",
        "warehouse_location_id": "RESTRICT",
        "inventory_lot_id": "RESTRICT",
        "transferred_by": "SET NULL",
    }
    assert "length(request_hash) = 64" in _check_sql(
        "production_stock_transfers"
    )


def test_n029_upgrade_from_empty_database_creates_only_new_tables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database_path = tmp_path / "n029-empty-upgrade.sqlite3"
    _upgrade(monkeypatch, database_path, TARGET_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _assert_database_health(connection, TARGET_REVISION)
        assert PRODUCTION_TABLES <= _tables(connection)
        for table_name in PREEXISTING_FACT_TABLES:
            assert table_name in _tables(connection)


def test_n029_upgrade_preserves_current_head_tables_verbatim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database_path = tmp_path / "n029-current-head-copy.sqlite3"
    _upgrade(monkeypatch, database_path, PREVIOUS_REVISION)

    with sqlite3.connect(database_path) as connection:
        before = {
            table_name: _table_sql(connection, table_name)
            for table_name in PREEXISTING_FACT_TABLES
        }
        before_counts = {
            table_name: connection.execute(
                f"SELECT COUNT(*) FROM {table_name}"
            ).fetchone()
            for table_name in PREEXISTING_FACT_TABLES
        }

    _upgrade(monkeypatch, database_path, TARGET_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _assert_database_health(connection, TARGET_REVISION)
        assert PRODUCTION_TABLES <= _tables(connection)
        assert {
            table_name: _table_sql(connection, table_name)
            for table_name in PREEXISTING_FACT_TABLES
        } == before
        assert {
            table_name: connection.execute(
                f"SELECT COUNT(*) FROM {table_name}"
            ).fetchone()
            for table_name in PREEXISTING_FACT_TABLES
        } == before_counts


def test_n029_empty_tables_downgrade_in_dependency_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database_path = tmp_path / "n029-empty-downgrade.sqlite3"
    _upgrade(monkeypatch, database_path, TARGET_REVISION)
    _downgrade(monkeypatch, database_path, PREVIOUS_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _assert_database_health(connection, PREVIOUS_REVISION)
        assert PRODUCTION_TABLES.isdisjoint(_tables(connection))
        assert PREEXISTING_FACT_TABLES <= _tables(connection)


def test_n029_downgrade_refuses_existing_completion_facts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database_path = tmp_path / "n029-fact-downgrade.sqlite3"
    _upgrade(monkeypatch, database_path, TARGET_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO production_completion_batches (
                idempotency_key, request_hash, item_count, completed_at
            ) VALUES (?, ?, 1, ?)
            """,
            ("n029-downgrade-guard", "a" * 64, "2026-07-17 09:00:00"),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match=DOWNGRADE_BLOCKED_MESSAGE):
        _downgrade(monkeypatch, database_path, PREVIOUS_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _assert_database_health(connection, TARGET_REVISION)
        assert PRODUCTION_TABLES <= _tables(connection)
        assert connection.execute(
            "SELECT idempotency_key, item_count FROM production_completion_batches"
        ).fetchone() == ("n029-downgrade-guard", 1)


def test_n029_downgrade_refuses_task_only_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database_path = tmp_path / "n029-task-only-downgrade.sqlite3"
    _upgrade(monkeypatch, database_path, TARGET_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        customer_id = connection.execute(
            "INSERT INTO customers (name) VALUES (?)",
            ("N029迁移测试客户",),
        ).lastrowid
        product_id = connection.execute(
            """
            INSERT INTO products (
                customer_id, product_code, customer_material_code, product_name
            ) VALUES (?, ?, ?, ?)
            """,
            (customer_id, "N029-MIG-P001", "N029-MIG-P001", "N029迁移测试产品"),
        ).lastrowid
        order_id = connection.execute(
            """
            INSERT INTO sales_orders (order_number, customer_id, order_date)
            VALUES (?, ?, ?)
            """,
            ("N029-MIG-ORDER", customer_id, "2026-07-17"),
        ).lastrowid
        item_id = connection.execute(
            """
            INSERT INTO sales_order_items (
                order_id, product_id, quantity, unit_price, subtotal,
                snapshot_product_name
            ) VALUES (?, ?, 1, 1, 1, ?)
            """,
            (order_id, product_id, "N029迁移测试产品"),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO production_tasks (
                order_item_id, status, planned_quantity,
                finished_coverage_snapshot, version
            ) VALUES (?, 'waiting_material', 0, 0, 1)
            """,
            (item_id,),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match=DOWNGRADE_BLOCKED_MESSAGE):
        _downgrade(monkeypatch, database_path, PREVIOUS_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _assert_database_health(connection, TARGET_REVISION)
        assert connection.execute("SELECT COUNT(*) FROM production_tasks").fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM production_completions"
        ).fetchone() == (0,)
