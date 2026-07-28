from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from app.models import Base


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "cu77v8x9z66"
TARGET_REVISION = "cv78v8x9z67"
TABLE_NAME = "production_completion_material_usages"


def _config(monkeypatch: pytest.MonkeyPatch, database_path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database_path.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "p1-11c-migration-test")
    return Config(str(PROJECT_ROOT / "alembic.ini"))


def _upgrade(
    monkeypatch: pytest.MonkeyPatch,
    database_path: Path,
    revision: str,
) -> None:
    command.upgrade(_config(monkeypatch, database_path), revision)


def _downgrade(
    monkeypatch: pytest.MonkeyPatch,
    database_path: Path,
    revision: str,
) -> None:
    command.downgrade(_config(monkeypatch, database_path), revision)


def _revision(connection: sqlite3.Connection) -> str:
    row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row is not None
    return str(row[0])


def _triggers(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'trigger' AND tbl_name = ?
            """,
            (TABLE_NAME,),
        )
    }


def _insert_usage(
    connection: sqlite3.Connection,
    *,
    row_id: int = 1,
    expected_version: int = 2,
    result_version: int = 4,
    credited_quantity: int = 6,
    reason_code: str = "intact_return",
) -> None:
    connection.execute(
        f"""
        INSERT INTO {TABLE_NAME} (
            id,
            completion_id,
            task_id,
            order_item_id,
            reservation_id,
            inventory_lot_id,
            expected_lot_version,
            result_lot_version,
            assigned_stock_quantity,
            actual_consumed_stock_quantity,
            returned_intact_stock_quantity,
            damaged_stock_quantity,
            offcut_stock_quantity,
            remaining_reserved_stock_quantity,
            credited_requirement_quantity,
            actual_credited_requirement_quantity,
            yield_factor,
            variance_reason_code,
            return_confirmed,
            return_confirmed_by,
            return_confirmed_at,
            return_status,
            consume_movement_id,
            release_movement_id,
            status,
            operator_id
        ) VALUES (
            ?, 1, 1, 1, ?, 1,
            ?, ?, 6, 5, 1, 0, 0, 0,
            ?, 5, 1, ?, 1, 1, CURRENT_TIMESTAMP,
            'released', ?, ?, 'posted', 1
        )
        """,
        (
            row_id,
            row_id,
            expected_version,
            result_version,
            credited_quantity,
            reason_code,
            row_id * 10 + 1,
            row_id * 10 + 2,
        ),
    )


def test_cv78_metadata_and_sqlite_guards_are_aligned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "cv78-guard.sqlite3"
    _upgrade(monkeypatch, database_path, TARGET_REVISION)

    with sqlite3.connect(database_path) as connection:
        columns = {
            str(row[1]): int(row[3])
            for row in connection.execute(f"PRAGMA table_info({TABLE_NAME})")
        }
        assert columns["result_lot_version"] == 1
        assert "result_lot_version" in Base.metadata.tables[TABLE_NAME].columns
        assert _revision(connection) == TARGET_REVISION
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert _triggers(connection) == {
            "trg_production_completion_material_usages_update_guard",
            "trg_production_completion_material_usages_delete_guard",
        }

        _insert_usage(connection)
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"""
                UPDATE {TABLE_NAME}
                SET actual_consumed_stock_quantity = 4
                WHERE id = 1
                """
            )
        connection.rollback()

        connection.execute(
            f"""
            UPDATE {TABLE_NAME}
            SET
                status = 'reversed',
                return_status = 'reversed',
                reversed_at = CURRENT_TIMESTAMP,
                reversed_by = 1,
                reversal_reason = '迁移测试受控撤销'
            WHERE id = 1
            """
        )
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"""
                UPDATE {TABLE_NAME}
                SET reversal_reason = '禁止第二次修改'
                WHERE id = 1
                """
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(f"DELETE FROM {TABLE_NAME} WHERE id = 1")
        connection.rollback()

    with pytest.raises(RuntimeError, match="禁止破坏性降级"):
        _downgrade(monkeypatch, database_path, PARENT_REVISION)
    with sqlite3.connect(database_path) as connection:
        assert _revision(connection) == TARGET_REVISION
        assert connection.execute(
            f"SELECT status FROM {TABLE_NAME} WHERE id = 1"
        ).fetchone() == ("reversed",)
        assert _triggers(connection)


def test_cv78_constraints_and_empty_round_trip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    invalid_database = tmp_path / "cv78-invalid.sqlite3"
    _upgrade(monkeypatch, invalid_database, TARGET_REVISION)
    with sqlite3.connect(invalid_database) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_usage(
                connection,
                expected_version=2,
                result_version=2,
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            _insert_usage(
                connection,
                row_id=2,
                credited_quantity=7,
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            _insert_usage(
                connection,
                row_id=3,
                reason_code="other",
            )
        connection.rollback()

    empty_database = tmp_path / "cv78-empty-round-trip.sqlite3"
    _upgrade(monkeypatch, empty_database, TARGET_REVISION)
    _downgrade(monkeypatch, empty_database, PARENT_REVISION)
    with sqlite3.connect(empty_database) as connection:
        assert _revision(connection) == PARENT_REVISION
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (TABLE_NAME,),
        ).fetchone() is None
        assert not _triggers(connection)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    _upgrade(monkeypatch, empty_database, TARGET_REVISION)
    with sqlite3.connect(empty_database) as connection:
        assert _revision(connection) == TARGET_REVISION
        assert _triggers(connection)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
