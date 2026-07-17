from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "ba54v8x9z44"
TARGET_REVISION = "bb55v8x9z45"
MIGRATION_PATH = (
    PROJECT_ROOT
    / "alembic"
    / "versions"
    / "bb55v8x9z45_n022_mold_location_movement.py"
)
NEW_MOLD_COLUMNS = {
    "location_version",
    "last_location_confirmed_at",
    "last_location_confirmed_by",
}
IMMUTABLE_TRIGGERS = {
    "trg_mold_location_movements_immutable_update",
    "trg_mold_location_movements_immutable_delete",
}


def _config(monkeypatch: pytest.MonkeyPatch, database_path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database_path.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "n022-c2-migration-test-secret")
    return Config(str(PROJECT_ROOT / "alembic.ini"))


def _columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        row[1]
        for row in connection.execute(f"PRAGMA table_info({table_name})")
    }


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _triggers(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        )
    }


def _assert_health(connection: sqlite3.Connection, revision: str) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute(
        "SELECT version_num FROM alembic_version"
    ).fetchone() == (revision,)
    assert not any(name.startswith("_alembic_tmp") for name in _tables(connection))


def _insert_parent_rows(connection: sqlite3.Connection) -> tuple[int, int]:
    connection.execute("PRAGMA foreign_keys = ON")
    user_id = connection.execute(
        """
        INSERT INTO users (
            username, password_hash, role, real_name, display_name,
            is_active, auth_version, must_change_password, customer_access_mode,
            ui_mode
        ) VALUES ('n022-admin', 'hash', 'admin', 'N022', 'N022', 1, 1, 0, 'all', 'standard')
        RETURNING id
        """
    ).fetchone()[0]
    mold_id = connection.execute(
        """
        INSERT INTO mold_tools (
            mold_code, mold_name, rack_location, remarks, is_active,
            created_by, updated_by
        ) VALUES (
            'MIG-N022-001', '迁移保留模具', '3F-M-R01-L1-D01-P01',
            'roundtrip', 1, ?, ?
        ) RETURNING id
        """,
        (user_id, user_id),
    ).fetchone()[0]
    connection.commit()
    return user_id, mold_id


def test_n022_c2_revision_is_linear_and_utf8() -> None:
    source = MIGRATION_PATH.read_bytes().decode("utf-8", errors="strict")
    assert "\ufffd" not in source
    assert 'revision = "bb55v8x9z45"' in source
    assert 'down_revision = "ba54v8x9z44"' in source
    assert "mold_location_movements rows are immutable" in source
    assert "禁止破坏性降级" in source


def test_existing_molds_round_trip_upgrade_downgrade_upgrade(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "n022-c2-roundtrip-copy.sqlite3"
    config = _config(monkeypatch, database_path)
    command.upgrade(config, PARENT_REVISION)
    with sqlite3.connect(database_path) as connection:
        _user_id, mold_id = _insert_parent_rows(connection)
        original_columns = _columns(connection, "mold_tools")
        _assert_health(connection, PARENT_REVISION)

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database_path) as connection:
        assert _columns(connection, "mold_tools") == original_columns | NEW_MOLD_COLUMNS
        assert connection.execute(
            """
            SELECT mold_code, rack_location, location_version,
                   last_location_confirmed_at, last_location_confirmed_by
            FROM mold_tools WHERE id = ?
            """,
            (mold_id,),
        ).fetchone() == (
            "MIG-N022-001",
            "3F-M-R01-L1-D01-P01",
            1,
            None,
            None,
        )
        assert "mold_location_movements" in _tables(connection)
        assert IMMUTABLE_TRIGGERS <= _triggers(connection)
        movement_fks = {
            (row[3], row[2], row[4], row[6])
            for row in connection.execute(
                "PRAGMA foreign_key_list(mold_location_movements)"
            )
        }
        assert movement_fks == {
            ("actor_id", "users", "id", "SET NULL"),
            ("mold_tool_id", "mold_tools", "id", "RESTRICT"),
        }
        _assert_health(connection, TARGET_REVISION)

    command.downgrade(config, PARENT_REVISION)
    with sqlite3.connect(database_path) as connection:
        assert _columns(connection, "mold_tools") == original_columns
        assert "mold_location_movements" not in _tables(connection)
        assert connection.execute(
            "SELECT mold_code, rack_location, remarks FROM mold_tools WHERE id = ?",
            (mold_id,),
        ).fetchone() == (
            "MIG-N022-001",
            "3F-M-R01-L1-D01-P01",
            "roundtrip",
        )
        _assert_health(connection, PARENT_REVISION)

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT location_version FROM mold_tools WHERE id = ?", (mold_id,)
        ).fetchone() == (1,)
        assert "mold_location_movements" in _tables(connection)
        assert IMMUTABLE_TRIGGERS <= _triggers(connection)
        _assert_health(connection, TARGET_REVISION)


def test_movement_ledger_enforces_unique_key_fks_immutability_and_downgrade_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "n022-c2-ledger-copy.sqlite3"
    config = _config(monkeypatch, database_path)
    command.upgrade(config, PARENT_REVISION)
    with sqlite3.connect(database_path) as connection:
        user_id, mold_id = _insert_parent_rows(connection)
    command.upgrade(config, TARGET_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            UPDATE mold_tools
            SET rack_location = '3F-M-R02-L1-D01-P01',
                location_version = 2,
                last_location_confirmed_at = CURRENT_TIMESTAMP,
                last_location_confirmed_by = ?
            WHERE id = ? AND location_version = 1
            """,
            (user_id, mold_id),
        )
        movement_id = connection.execute(
            """
            INSERT INTO mold_location_movements (
                mold_tool_id, mold_code_snapshot, from_location, to_location,
                actor_id, idempotency_key, expected_version, resulting_version,
                source, note
            ) VALUES (?, 'MIG-N022-001', '3F-M-R01-L1-D01-P01',
                      '3F-M-R02-L1-D01-P01', ?, 'migration-idem-001',
                      1, 2, 'api', 'migration test')
            RETURNING id
            """,
            (mold_id, user_id),
        ).fetchone()[0]
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE mold_location_movements SET note = 'changed' WHERE id = ?",
                (movement_id,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "DELETE FROM mold_location_movements WHERE id = ?", (movement_id,)
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO mold_location_movements (
                    mold_tool_id, mold_code_snapshot, from_location, to_location,
                    actor_id, idempotency_key, expected_version, resulting_version,
                    source
                ) VALUES (?, 'MIG-N022-001', '3F-M-R01-L1-D01-P01',
                          '3F-M-R03-L1-D01-P01', ?, 'migration-idem-001',
                          2, 3, 'api')
                """,
                (mold_id, user_id),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO mold_location_movements (
                    mold_tool_id, mold_code_snapshot, from_location, to_location,
                    actor_id, idempotency_key, expected_version, resulting_version,
                    source
                ) VALUES (999999, 'MISSING', '3F-M-R01-L1-D01-P01',
                          '3F-M-R03-L1-D01-P01', ?, 'migration-idem-002',
                          1, 2, 'api')
                """,
                (user_id,),
            )
        connection.rollback()
        _assert_health(connection, TARGET_REVISION)

    with pytest.raises(RuntimeError, match="禁止破坏性降级"):
        command.downgrade(config, PARENT_REVISION)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM mold_location_movements"
        ).fetchone() == (1,)
        _assert_health(connection, TARGET_REVISION)
