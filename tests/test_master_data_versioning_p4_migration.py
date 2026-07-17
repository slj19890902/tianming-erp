from __future__ import annotations

import hashlib
from pathlib import Path
import re
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from app.models import Base


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "ax51v8x9z41"
TARGET_REVISION = "ay52v8x9z42"
VERSION_TABLE = "master_data_object_versions"
IMMUTABLE_UPDATE_TRIGGER = "trg_master_data_object_versions_immutable_update"
IMMUTABLE_DELETE_TRIGGER = "trg_master_data_object_versions_immutable_delete"
IMMUTABLE_UPDATE_MESSAGE = "master_data_object_versions is append-only; UPDATE is forbidden"
IMMUTABLE_DELETE_MESSAGE = "master_data_object_versions is append-only; DELETE is forbidden"
DOWNGRADE_BLOCKED_MESSAGE = (
    "主数据版本账本已有事实记录或当前对象版本不为 1，"
    "拒绝降级以避免历史丢失"
)


def _alembic_config(monkeypatch: pytest.MonkeyPatch, database_path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database_path.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "p4-master-data-migration-test")
    return Config(str(PROJECT_ROOT / "alembic.ini"))


def _upgrade(
    monkeypatch: pytest.MonkeyPatch,
    database_path: Path,
    revision: str,
) -> None:
    command.upgrade(_alembic_config(monkeypatch, database_path), revision)


def _downgrade(
    monkeypatch: pytest.MonkeyPatch,
    database_path: Path,
    revision: str,
) -> None:
    command.downgrade(_alembic_config(monkeypatch, database_path), revision)


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table_name}")')
    }


def _triggers(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
            (table_name,),
        )
    }


def _insert_ledger_fact(connection: sqlite3.Connection) -> int:
    snapshot_json = '{"name":"P4 immutable ledger fact"}'
    snapshot_hash = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
    return int(
        connection.execute(
            f"""
            INSERT INTO {VERSION_TABLE} (
                object_type, object_id, version, action,
                snapshot_schema_version, snapshot_json, snapshot_sha256,
                changed_fields_json, change_set_id, reason, source
            ) VALUES ('customer', 1, 1, 'baseline', 1, ?, ?, '{{}}', ?, ?, ?)
            """,
            (
                snapshot_json,
                snapshot_hash,
                "00000000-0000-0000-0000-000000000001",
                "immutable-ledger-test",
                "test.p4",
            ),
        ).lastrowid
    )


def _assert_database_health(
    connection: sqlite3.Connection,
    expected_revision: str,
) -> None:
    assert connection.execute(
        "SELECT version_num FROM alembic_version"
    ).fetchone() == (expected_revision,)
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _check_sql(table_name: str) -> str:
    return " ".join(
        str(constraint.sqltext)
        for constraint in Base.metadata.tables[table_name].constraints
        if isinstance(constraint, CheckConstraint)
    )


def test_p4_model_metadata_registers_versions_and_ledger_contract() -> None:
    for table_name in ("customers", "products", "materials"):
        table = Base.metadata.tables[table_name]
        assert "version" in table.c
        assert not table.c.version.nullable
        assert str(table.c.version.server_default.arg) == "1"
        assert "version >= 1" in _check_sql(table_name)

    ledger = Base.metadata.tables[VERSION_TABLE]
    assert set(ledger.c.keys()) == {
        "id",
        "object_type",
        "object_id",
        "version",
        "action",
        "snapshot_schema_version",
        "snapshot_json",
        "snapshot_sha256",
        "changed_fields_json",
        "restored_from_version",
        "change_set_id",
        "operation_log_id",
        "actor_user_id",
        "actor_username_snapshot",
        "reason",
        "source",
        "created_at",
    }
    unique_sets = {
        frozenset(column.name for column in constraint.columns)
        for constraint in ledger.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert frozenset({"object_type", "object_id", "version"}) in unique_sets
    foreign_key_deletes = {
        next(iter(constraint.elements)).parent.name: next(
            iter(constraint.elements)
        ).ondelete
        for constraint in ledger.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert foreign_key_deletes == {
        "operation_log_id": "SET NULL",
        "actor_user_id": "SET NULL",
    }
    assert {
        "ix_master_data_object_versions_object_created",
        "ix_master_data_object_versions_change_set_id",
        "ix_master_data_object_versions_operation_log_id",
        "ix_master_data_object_versions_actor_user_id",
    } <= {index.name for index in ledger.indexes}


def test_upgrade_initializes_existing_rows_without_fabricating_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "p4-existing.sqlite3"
    _upgrade(monkeypatch, database_path, PREVIOUS_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        customer_id = connection.execute(
            "INSERT INTO customers (name) VALUES (?)",
            ("P4 迁移客户",),
        ).lastrowid
        material_id = connection.execute(
            "INSERT INTO materials (code) VALUES (?)",
            ("P4-MIG-MAT",),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO products (
                customer_id, product_code, customer_material_code,
                product_name, material_id
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                customer_id,
                "P4-MIG-P001",
                "P4-MIG-C001",
                "P4 迁移产品",
                material_id,
            ),
        )
        connection.commit()

    _upgrade(monkeypatch, database_path, TARGET_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _assert_database_health(connection, TARGET_REVISION)
        assert VERSION_TABLE in _tables(connection)
        for table_name in ("customers", "products", "materials"):
            assert "version" in _columns(connection, table_name)
            assert connection.execute(
                f"SELECT DISTINCT version FROM {table_name}"
            ).fetchall() == [(1,)]
        assert connection.execute(
            f"SELECT COUNT(*) FROM {VERSION_TABLE}"
        ).fetchone() == (0,)


def test_sqlite_ledger_triggers_allow_insert_and_reject_history_rewrites(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "p4-immutable-ledger.sqlite3"
    _upgrade(monkeypatch, database_path, TARGET_REVISION)

    with sqlite3.connect(database_path) as connection:
        assert {IMMUTABLE_UPDATE_TRIGGER, IMMUTABLE_DELETE_TRIGGER} <= _triggers(
            connection, VERSION_TABLE
        )
        ledger_id = _insert_ledger_fact(connection)
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError, match=re.escape(IMMUTABLE_UPDATE_MESSAGE)):
            connection.execute(
                f"UPDATE {VERSION_TABLE} SET reason = ? WHERE id = ?",
                ("rewritten", ledger_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match=re.escape(IMMUTABLE_DELETE_MESSAGE)):
            connection.execute(
                f"DELETE FROM {VERSION_TABLE} WHERE id = ?",
                (ledger_id,),
            )

        assert connection.execute(
            f"SELECT reason FROM {VERSION_TABLE} WHERE id = ?", (ledger_id,)
        ).fetchone() == ("immutable-ledger-test",)


def test_empty_ledger_with_only_v1_entities_can_round_trip_downgrade(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "p4-empty-ledger-roundtrip.sqlite3"
    _upgrade(monkeypatch, database_path, TARGET_REVISION)
    with sqlite3.connect(database_path) as connection:
        customer_id = connection.execute(
            "INSERT INTO customers (name, version) VALUES (?, 1)",
            ("P4 可降级客户",),
        ).lastrowid
        material_id = connection.execute(
            "INSERT INTO materials (code, version) VALUES (?, 1)",
            ("P4-DOWN-MAT",),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO products (
                customer_id, product_code, customer_material_code,
                product_name, material_id, version
            ) VALUES (?, ?, ?, ?, ?, 1)
            """,
            (
                customer_id,
                "P4-DOWN-P001",
                "P4-DOWN-C001",
                "P4 可降级产品",
                material_id,
            ),
        )
        connection.commit()

    _downgrade(monkeypatch, database_path, PREVIOUS_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _assert_database_health(connection, PREVIOUS_REVISION)
        assert VERSION_TABLE not in _tables(connection)
        assert not {
            IMMUTABLE_UPDATE_TRIGGER,
            IMMUTABLE_DELETE_TRIGGER,
        } & _triggers(connection, VERSION_TABLE)
        for table_name in ("customers", "products", "materials"):
            assert "version" not in _columns(connection, table_name)
        assert connection.execute(
            "SELECT name FROM customers WHERE name = ?",
            ("P4 可降级客户",),
        ).fetchone() == ("P4 可降级客户",)

    _upgrade(monkeypatch, database_path, TARGET_REVISION)

    with sqlite3.connect(database_path) as connection:
        _assert_database_health(connection, TARGET_REVISION)
        assert {IMMUTABLE_UPDATE_TRIGGER, IMMUTABLE_DELETE_TRIGGER} <= _triggers(
            connection, VERSION_TABLE
        )


def test_downgrade_refuses_ledger_fact_before_any_schema_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "p4-ledger-fact.sqlite3"
    _upgrade(monkeypatch, database_path, TARGET_REVISION)
    snapshot_json = '{"name":"P4事实客户"}'
    snapshot_hash = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
    with sqlite3.connect(database_path) as connection:
        customer_id = connection.execute(
            "INSERT INTO customers (name, version) VALUES (?, 1)",
            ("P4 事实客户",),
        ).lastrowid
        connection.execute(
            f"""
            INSERT INTO {VERSION_TABLE} (
                object_type, object_id, version, action,
                snapshot_schema_version, snapshot_json, snapshot_sha256,
                changed_fields_json, change_set_id, reason, source
            ) VALUES ('customer', ?, 1, 'baseline', 1, ?, ?, '{{}}', ?, ?, ?)
            """,
            (
                customer_id,
                snapshot_json,
                snapshot_hash,
                "00000000-0000-0000-0000-000000000001",
                "迁移守卫事实",
                "test.p4",
            ),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match=re.escape(DOWNGRADE_BLOCKED_MESSAGE)):
        _downgrade(monkeypatch, database_path, PREVIOUS_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _assert_database_health(connection, TARGET_REVISION)
        assert VERSION_TABLE in _tables(connection)
        assert "version" in _columns(connection, "customers")
        assert connection.execute(
            f"SELECT COUNT(*) FROM {VERSION_TABLE}"
        ).fetchone() == (1,)


def test_downgrade_refuses_bumped_entity_even_if_ledger_is_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "p4-bumped-entity.sqlite3"
    _upgrade(monkeypatch, database_path, TARGET_REVISION)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO materials (code, version) VALUES (?, 2)",
            ("P4-BUMPED-MAT",),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match=re.escape(DOWNGRADE_BLOCKED_MESSAGE)):
        _downgrade(monkeypatch, database_path, PREVIOUS_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _assert_database_health(connection, TARGET_REVISION)
        assert connection.execute(
            "SELECT version FROM materials WHERE code = ?",
            ("P4-BUMPED-MAT",),
        ).fetchone() == (2,)
