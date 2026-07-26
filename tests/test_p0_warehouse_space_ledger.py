from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess

from alembic import command
from alembic.config import Config
import pytest


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "cp72v8x9z61"
TARGET_REVISION = "cq73v8x9z62"
MIGRATION = (
    ROOT
    / "alembic"
    / "versions"
    / "cq73v8x9z62_warehouse_space_ledger.py"
)
WAREHOUSE_HTML = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")


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


def test_space_ledger_migration_is_linear_after_accepted_n041() -> None:
    spec = importlib.util.spec_from_file_location("warehouse_space_ledger", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == TARGET_REVISION
    assert module.down_revision == PARENT_REVISION
    source = MIGRATION.read_text(encoding="utf-8")
    assert "warehouse_floors" in source
    assert "warehouse_areas" in source
    assert "warehouse_locations" not in source
    assert "refusing destructive downgrade" in source


def test_space_ledger_migration_round_trip_and_no_seed_guess(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "warehouse-space-ledger.sqlite3"
    config = _config(monkeypatch, path)

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET_REVISION
        assert connection.execute("SELECT COUNT(*) FROM warehouse_floors").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM warehouse_areas").fetchone()[0] == 0
    assert _checks(path) == ("ok", 0)

    command.downgrade(config, PARENT_REVISION)
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "warehouse_floors" not in tables
        assert "warehouse_areas" not in tables
    assert _checks(path) == ("ok", 0)

    command.upgrade(config, TARGET_REVISION)
    assert _checks(path) == ("ok", 0)


def test_space_ledger_downgrade_fails_closed_after_real_floor_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "warehouse-space-ledger-fact.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO warehouse_floors (
                floor_code, floor_name, floor_number, construction_status
            ) VALUES ('1F', '一楼', 1, 'ledger_building')
            """
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="refusing destructive downgrade"):
        command.downgrade(config, PARENT_REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET_REVISION
    assert _checks(path) == ("ok", 0)


def test_space_ledger_frontend_is_real_data_first_and_pending_by_default(
    tmp_path: Path,
) -> None:
    for marker in (
        "楼层与区域建设进度",
        "规划 / 已录库位",
        "已布局 / 待布局",
        "栈板容量 / 当前占用",
        "没有现场平面图时",
        "不会自动出现在三楼或其他平面图",
        'values["placement_status"] = "unplaced"',
    ):
        if marker.startswith("values"):
            assert marker in (ROOT / "app" / "api" / "warehouse.py").read_text(
                encoding="utf-8"
            )
        else:
            assert marker in WAREHOUSE_HTML
    assert "FLOOR3_AREA_OPTIONS" in WAREHOUSE_HTML
    assert "loadWarehouseSpace" in WAREHOUSE_HTML
    assert "refreshLocationLedgerAreaOptions" in WAREHOUSE_HTML

    node = shutil.which("node")
    if node is None:
        return
    scripts = "\n".join(
        re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>",
            WAREHOUSE_HTML,
            flags=re.DOTALL,
        )
    )
    script = tmp_path / "warehouse-space-ledger.js"
    script.write_text(scripts, encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(script)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
