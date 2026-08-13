from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "ll20v8x9z09"
TARGET_REVISION = "mm21v8x9z10"
JOBS = "mold_label_print_jobs"
ITEMS = "mold_label_print_job_items"


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _triggers(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
            (table,),
        )
    }


def test_mm21_schema_only_roundtrip_immutable_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "p1-58-mold-label-print.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, PARENT_REVISION)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO users(username,password_hash,role,real_name,display_name,must_change_password) "
            "VALUES ('p158','x','admin','P1-58','P1-58',0)"
        )
        user_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute(
            "INSERT INTO mold_tools(mold_code,mold_name,rack_location) VALUES ('P1-58-M','P1-58 M','1F-M-R01-L1-G01')"
        )
        mold_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.commit()
    command.upgrade(config, TARGET_REVISION)
    expected = {JOBS, ITEMS}
    with sqlite3.connect(path) as connection:
        assert expected <= _tables(connection)
        assert connection.execute(f"SELECT COUNT(*) FROM {JOBS}").fetchone()[0] == 0
        assert connection.execute(f"SELECT COUNT(*) FROM {ITEMS}").fetchone()[0] == 0
        for table in expected:
            assert _triggers(connection, table) == {
                f"trg_{table}_immutable_update",
                f"trg_{table}_immutable_delete",
            }
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    command.downgrade(config, PARENT_REVISION)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            f"INSERT INTO {JOBS}(idempotency_key,source,item_count,printed_by,printed_by_username) VALUES ('p1-58-migration-job','single',1,?,'p158')",
            (user_id,),
        )
        job_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute(
            f"INSERT INTO {ITEMS}(print_job_id,mold_tool_id,item_order,mold_code_snapshot,rack_location_snapshot) VALUES (?,?,1,'P1-58-M','1F-M-R01-L1-G01')",
            (job_id, mold_id),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="rows are immutable"):
            connection.execute(f"UPDATE {JOBS} SET source='batch' WHERE id=?", (job_id,))
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="rows are immutable"):
            connection.execute(f"DELETE FROM {ITEMS} WHERE print_job_id=?", (job_id,))
        connection.rollback()

    with pytest.raises(RuntimeError, match="拒绝破坏性降级"):
        command.downgrade(config, PARENT_REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == TARGET_REVISION
        assert connection.execute(f"SELECT COUNT(*) FROM {JOBS}").fetchone()[0] == 1
        assert connection.execute(f"SELECT COUNT(*) FROM {ITEMS}").fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
