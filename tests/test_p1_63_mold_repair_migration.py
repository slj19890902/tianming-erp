from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest


ROOT = Path(__file__).resolve().parents[1]
PARENT = "qq25v8x9z14"
TARGET = "rr26v8x9z15"


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def test_repair_schema_roundtrip_defaults_and_fail_closed_downgrade(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "p1-63-repair.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, PARENT)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO users(username,password_hash,role,real_name,display_name,must_change_password) "
            "VALUES ('p163','x','admin','P1-63','P1-63',0)"
        )
        connection.execute(
            "INSERT INTO mold_tools(mold_code,mold_name,rack_location) VALUES ('P163-M','P1-63','1F-M-R01-L1-G01')"
        )
        connection.commit()

    command.upgrade(config, TARGET)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT repair_status, repair_version FROM mold_tools"
        ).fetchone() == ("normal", 1)
        assert connection.execute("SELECT COUNT(*) FROM mold_repair_events").fetchone()[0] == 0
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='mold_repair_events'"
            )
        }
        assert triggers == {
            "trg_mold_repair_events_immutable_update",
            "trg_mold_repair_events_immutable_delete",
        }
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    command.downgrade(config, PARENT)
    command.upgrade(config, TARGET)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE mold_tools SET repair_status='needs_repair', repair_version=2")
        connection.commit()
    with pytest.raises(RuntimeError, match="禁止降级"):
        command.downgrade(config, PARENT)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == TARGET
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
