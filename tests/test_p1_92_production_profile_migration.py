from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest


ROOT = Path(__file__).resolve().parents[1]
PARENT = "df40v8x9z29"
TARGET = "dg41v8x9z30"
MIGRATION = (
    ROOT
    / "alembic"
    / "versions"
    / "dg41v8x9z30_production_task_profile_refresh.py"
)


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def test_p1_92_migration_is_linear_from_live_parent() -> None:
    spec = importlib.util.spec_from_file_location("p1_92_profile", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == TARGET
    assert module.down_revision == PARENT


def test_p1_92_migration_round_trip_and_destructive_downgrade_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "p1-92.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, PARENT)
    command.upgrade(config, TARGET)
    command.downgrade(config, PARENT)
    command.upgrade(config, TARGET)

    with sqlite3.connect(path) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(production_tasks)")
        }
        assert "production_profile_schema_version" in columns
        assert "production_needs_die_cut_snapshot" in columns
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "INSERT INTO production_tasks "
            "(order_item_id, status, planned_quantity, "
            "production_profile_schema_version) VALUES (999999, 'waiting_material', 0, 1)"
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="Refusing destructive downgrade"):
        command.downgrade(config, PARENT)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM production_tasks WHERE order_item_id = 999999"
        )
        connection.commit()
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
