from __future__ import annotations

from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "dk93v8x9z82"
TARGET_REVISION = "dl94v8x9z83"


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def test_material_code_uniqueness_round_trips_by_supplier(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "material-code-per-supplier.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, PARENT_REVISION)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO materials (code, supplier_name, layer_count) "
            "VALUES ('EXISTING', '昆山鸣朋', 3)"
        )
        connection.commit()

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM materials").fetchone()[0] == 1
        connection.execute(
            "INSERT INTO materials (code, supplier_name, layer_count) "
            "VALUES ('G9G', '昆山鸣朋', 3)"
        )
        connection.execute(
            "INSERT INTO materials (code, supplier_name, layer_count) "
            "VALUES ('G9G', '胜源', 3)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO materials (code, supplier_name, layer_count) "
                "VALUES ('G9G', '胜源', 3)"
            )
        connection.execute("DELETE FROM materials WHERE code = 'G9G'")
        connection.commit()

    command.downgrade(config, PARENT_REVISION)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        indexes = {
            row[1]: bool(row[2])
            for row in connection.execute("PRAGMA index_list('materials')")
        }
        assert indexes["ix_materials_code"] is False
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='materials'"
        ).fetchone()[0]
        assert "UNIQUE (supplier_name, code)" in table_sql
        assert "ck_materials_version" in table_sql


def test_material_code_downgrade_refuses_cross_supplier_duplicates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "material-code-downgrade-guard.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        connection.executemany(
            "INSERT INTO materials (code, supplier_name, layer_count) VALUES (?, ?, ?)",
            [("G9G", "昆山鸣朋", 3), ("G9G", "胜源", 3)],
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="已有跨供应商同码材质"):
        command.downgrade(config, PARENT_REVISION)
