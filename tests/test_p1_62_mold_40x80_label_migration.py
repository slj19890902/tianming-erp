from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest


ROOT = Path(__file__).resolve().parents[1]
PARENT = "rr26v8x9z15"
TARGET = "ss27v8x9z16"
JOBS = "mold_label_print_jobs"


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def test_ss27_backfills_legacy_template_and_refuses_losing_80x40_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "p1-62-template-version.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, PARENT)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO users(username,password_hash,role,real_name,display_name,must_change_password) "
            "VALUES ('p162','x','admin','P1-62','P1-62',0)"
        )
        user_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute(
            f"INSERT INTO {JOBS}(idempotency_key,source,item_count,printed_by,printed_by_username) "
            "VALUES ('p1-62-old-40x30','single',1,?,'p162')",
            (user_id,),
        )
        connection.commit()

    command.upgrade(config, TARGET)
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({JOBS})")}
        assert "template_version" in columns
        assert connection.execute(
            f"SELECT template_version FROM {JOBS} WHERE idempotency_key='p1-62-old-40x30'"
        ).fetchone()[0] == "mold_40x30_v1"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    command.downgrade(config, PARENT)
    command.upgrade(config, TARGET)
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"INSERT INTO {JOBS}(idempotency_key,source,item_count,template_version,printed_by,printed_by_username) "
            "VALUES ('p1-62-new-80x40','single',1,'mold_80x40_v1',?,'p162')",
            (user_id,),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="40×80"):
        command.downgrade(config, PARENT)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == TARGET
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
