from __future__ import annotations

from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest


ROOT = Path(__file__).resolve().parents[1]
PARENT = "ff14v8x9z03"
TARGET = "gg15v8x9z04"


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    monkeypatch.setenv("ERP_SECRET_KEY", "p1-44b-migration-test")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _columns(connection: sqlite3.Connection) -> set[str]:
    return {row[1] for row in connection.execute("PRAGMA table_info(mold_tools)")}


def _insert_parent_fact(connection: sqlite3.Connection) -> tuple[int, int]:
    user_id = connection.execute(
        "INSERT INTO users (username,password_hash,role,real_name,is_active,auth_version,"
        "must_change_password,customer_access_mode,ui_mode) "
        "VALUES ('p144b-admin','hash','admin','P144B',1,1,0,'all','standard') RETURNING id"
    ).fetchone()[0]
    mold_id = connection.execute(
        "INSERT INTO mold_tools (mold_code,mold_name,rack_location,location_version,"
        "remarks,is_active,created_by,updated_by) "
        "VALUES ('P144B-MIG','迁移模具','1F-M-R01-L2-G01',1,'保留',1,?,?) RETURNING id",
        (user_id, user_id),
    ).fetchone()[0]
    connection.commit()
    return user_id, mold_id


def _assert_health(connection: sqlite3.Connection, revision: str) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
        revision,
    )


def test_mold_archive_migration_round_trip_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "p1-44b-migration.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, PARENT)
    with sqlite3.connect(path) as connection:
        user_id, mold_id = _insert_parent_fact(connection)
        parent_columns = _columns(connection)
        _assert_health(connection, PARENT)

    command.upgrade(config, TARGET)
    with sqlite3.connect(path) as connection:
        assert _columns(connection) == parent_columns | {
            "archive_status",
            "archived_at",
            "archived_by",
            "archive_reason",
            "pre_archive_location",
            "restored_at",
            "restored_by",
        }
        assert connection.execute(
            "SELECT archive_status,archived_at,archived_by,archive_reason,"
            "pre_archive_location,restored_at,restored_by FROM mold_tools WHERE id=?",
            (mold_id,),
        ).fetchone() == ("active", None, None, None, None, None, None)
        _assert_health(connection, TARGET)

    command.downgrade(config, PARENT)
    with sqlite3.connect(path) as connection:
        assert _columns(connection) == parent_columns
        _assert_health(connection, PARENT)

    command.upgrade(config, TARGET)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE mold_tools SET archive_status='archived',is_active=0,"
            "rack_location='3F-M-ARCHIVE-AB2-N',location_version=2,"
            "archived_at=CURRENT_TIMESTAMP,archived_by=?,archive_reason='unbound',"
            "pre_archive_location='1F-M-R01-L2-G01' WHERE id=?",
            (user_id, mold_id),
        )
        connection.commit()
        _assert_health(connection, TARGET)
    with pytest.raises(RuntimeError, match="拒绝破坏性降级"):
        command.downgrade(config, PARENT)
    with sqlite3.connect(path) as connection:
        _assert_health(connection, TARGET)
