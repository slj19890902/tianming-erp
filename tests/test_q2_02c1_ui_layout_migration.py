from __future__ import annotations

from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect

from app.core.database import create_sqlite_engine


ROOT = Path(__file__).resolve().parents[1]
PARENT = "dw05v8x9z94"
TARGET = "dx06v8x9z95"
TABLE = "ui_layout_revisions"


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _checks(path: Path) -> tuple[str, int]:
    with sqlite3.connect(path) as connection:
        return (
            connection.execute("PRAGMA integrity_check").fetchone()[0],
            len(connection.execute("PRAGMA foreign_key_check").fetchall()),
        )


def test_ui_layout_migration_is_linear_and_round_trips(monkeypatch, tmp_path) -> None:
    source = (ROOT / "alembic/versions/dx06v8x9z95_ui_layout_revisions.py").read_text(encoding="utf-8")
    assert 'revision = "dx06v8x9z95"' in source
    assert 'down_revision = "dw05v8x9z94"' in source
    from app.models import Base

    assert TABLE in Base.metadata.tables
    path = tmp_path / "ui-layout-roundtrip.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET)
    inspector = inspect(create_sqlite_engine(path))
    assert TABLE in set(inspector.get_table_names())
    columns = {column["name"]: column for column in inspector.get_columns(TABLE)}
    assert columns["operation_key"]["type"].length == 64
    assert _checks(path) == ("ok", 0)
    command.downgrade(config, PARENT)
    assert TABLE not in set(inspect(create_sqlite_engine(path)).get_table_names())
    command.upgrade(config, TARGET)
    assert _checks(path) == ("ok", 0)


def test_ui_layout_rows_are_immutable_and_downgrade_fails_closed(monkeypatch, tmp_path) -> None:
    path = tmp_path / "ui-layout-immutable.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET)
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"""INSERT INTO {TABLE}(
            role_code,display_mode,stream,version,catalog_version,payload_json,payload_hash,
            base_release_version,operation_kind
            ) VALUES ('admin','standard','release',1,'q2-02c1-v1','{{}}',?,0,'publish')""",
            ("a" * 64,),
        )
        connection.commit()
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute(f"UPDATE {TABLE} SET version=2")
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute(f"DELETE FROM {TABLE}")
    with pytest.raises(RuntimeError, match="禁止破坏性降级"):
        command.downgrade(config, PARENT)
    assert _checks(path) == ("ok", 0)
