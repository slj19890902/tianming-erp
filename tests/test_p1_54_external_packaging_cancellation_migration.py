from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT
    / "alembic/versions/ll20v8x9z09_external_packaging_purchase_cancellations.py"
)
PARENT_REVISION = "kk19v8x9z08"
TARGET_REVISION = "ll20v8x9z09"
TABLE = "external_packaging_purchase_cancellations"


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _database_checks(path: Path) -> tuple[str, list[tuple]]:
    with sqlite3.connect(path) as connection:
        return (
            connection.execute("PRAGMA integrity_check").fetchone()[0],
            connection.execute("PRAGMA foreign_key_check").fetchall(),
        )


def _version(connection: sqlite3.Connection) -> str:
    return connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]


def _table_exists(connection: sqlite3.Connection) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (TABLE,),
        ).fetchone()
        is not None
    )


def _immutable_trigger_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'trigger' AND tbl_name = ?",
            (TABLE,),
        )
    }


def _seed_cancellation_fact(connection: sqlite3.Connection) -> int:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("INSERT INTO customers(name) VALUES ('P1-54 migration customer')")
    customer_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
    connection.execute(
        "INSERT INTO sales_orders(order_number, customer_id, order_date) "
        "VALUES ('P1-54-MIGRATION-ORDER', ?, '2026-08-13')",
        (customer_id,),
    )
    order_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
    connection.execute(
        "INSERT INTO supplier_master_records(standard_name, normalized_name) "
        "VALUES ('P1-54 migration supplier', 'p1-54 migration supplier')"
    )
    supplier_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
    connection.execute(
        "INSERT INTO external_packaging_purchase_batches("
        "sales_order_id, idempotency_key, request_fingerprint"
        ") VALUES (?, 'p1-54-migration-batch', ?)",
        (order_id, "f" * 64),
    )
    batch_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
    connection.execute(
        "INSERT INTO external_packaging_purchase_orders("
        "batch_id, purchase_number, supplier_id, supplier_name_snapshot, "
        "currency, goods_amount, tax_amount, total_amount"
        ") VALUES (?, 'EP-P1-54-MIGRATION', ?, 'P1-54 migration supplier', "
        "'CNY', 1, 0, 1)",
        (batch_id, supplier_id),
    )
    purchase_order_id = int(
        connection.execute("SELECT last_insert_rowid()").fetchone()[0]
    )
    connection.execute(
        f"INSERT INTO {TABLE}(purchase_order_id, source, reason) "
        "VALUES (?, 'order_workflow_rollback', 'migration fail-closed test')",
        (purchase_order_id,),
    )
    connection.commit()
    return purchase_order_id


def test_ll20_migration_source_is_schema_only() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    compile(source, str(MIGRATION), "exec")
    assert 'revision = "ll20v8x9z09"' in source
    assert 'down_revision = "kk19v8x9z08"' in source
    assert "authorized_data_repair" in source
    assert "拒绝破坏性降级" in source
    for forbidden in (
        "INSERT INTO",
        "DELETE FROM",
        "UPDATE sales_orders",
        "UPDATE external_packaging_purchase_orders",
        "UPDATE external_packaging_purchase_batches",
        "UPDATE external_packaging_receipts",
    ):
        assert forbidden not in source


def test_ll20_sqlite_roundtrip_immutability_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "p1-54-cancellation-migration.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, PARENT_REVISION)

    statements: list[str] = []

    def capture_statement(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(statement)

    event.listen(Engine, "before_cursor_execute", capture_statement)
    try:
        command.upgrade(config, TARGET_REVISION)
    finally:
        event.remove(Engine, "before_cursor_execute", capture_statement)

    business_dml: list[str] = []
    for statement in statements:
        normalized = " ".join(statement.strip().split())
        match = re.match(
            r'^(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+["`\[]?([\w]+)',
            normalized,
            flags=re.IGNORECASE,
        )
        if match and match.group(1).lower() != "alembic_version":
            business_dml.append(normalized)
    assert business_dml == []

    expected_triggers = {
        f"trg_{TABLE}_immutable_update",
        f"trg_{TABLE}_immutable_delete",
    }
    with sqlite3.connect(path) as connection:
        assert _version(connection) == TARGET_REVISION
        assert _table_exists(connection)
        assert _immutable_trigger_names(connection) == expected_triggers
        assert connection.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] == 0
    assert _database_checks(path) == ("ok", [])

    command.downgrade(config, PARENT_REVISION)
    with sqlite3.connect(path) as connection:
        assert _version(connection) == PARENT_REVISION
        assert not _table_exists(connection)
    assert _database_checks(path) == ("ok", [])

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        assert _version(connection) == TARGET_REVISION
        assert _table_exists(connection)
        assert _immutable_trigger_names(connection) == expected_triggers
        purchase_order_id = _seed_cancellation_fact(connection)
        with pytest.raises(sqlite3.IntegrityError, match="rows are immutable"):
            connection.execute(
                f"UPDATE {TABLE} SET reason = 'changed' WHERE purchase_order_id = ?",
                (purchase_order_id,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="rows are immutable"):
            connection.execute(
                f"DELETE FROM {TABLE} WHERE purchase_order_id = ?",
                (purchase_order_id,),
            )
        connection.rollback()
        assert connection.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] == 1

    with pytest.raises(RuntimeError, match="拒绝破坏性降级"):
        command.downgrade(config, PARENT_REVISION)
    with sqlite3.connect(path) as connection:
        assert _version(connection) == TARGET_REVISION
        assert _table_exists(connection)
        assert _immutable_trigger_names(connection) == expected_triggers
        assert connection.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] == 1
    assert _database_checks(path) == ("ok", [])
