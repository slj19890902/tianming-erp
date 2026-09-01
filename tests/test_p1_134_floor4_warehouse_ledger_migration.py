from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest


ROOT = Path(__file__).resolve().parents[1]
PARENT = "jc64v8x9z53"
TARGET = "jd65v8x9z54"
MIGRATION = (
    ROOT
    / "alembic"
    / "versions"
    / "jd65v8x9z54_p1_134_floor4_warehouse_ledger.py"
)


def _config(monkeypatch: pytest.MonkeyPatch, database_path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database_path.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "p1-134-floor4-ledger-test")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option(
        "sqlalchemy.url", f"sqlite+pysqlite:///{database_path.as_posix()}"
    )
    return config


@pytest.fixture(scope="module")
def parent_database(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch_module: pytest.MonkeyPatch,
) -> Path:
    database_path = tmp_path_factory.mktemp("p1-134-floor4-ledger") / "parent.sqlite3"
    command.upgrade(_config(monkeypatch_module, database_path), PARENT)
    return database_path


@pytest.fixture(scope="module")
def monkeypatch_module() -> pytest.MonkeyPatch:
    patcher = pytest.MonkeyPatch()
    yield patcher
    patcher.undo()


def _copy(source: Path, target: Path) -> Path:
    target.write_bytes(source.read_bytes())
    return target


def _checks(database_path: Path) -> tuple[str, list[tuple]]:
    with sqlite3.connect(database_path) as connection:
        return (
            connection.execute("PRAGMA integrity_check").fetchone()[0],
            connection.execute("PRAGMA foreign_key_check").fetchall(),
        )


def test_floor4_ledger_migration_is_the_unique_linear_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(monkeypatch, ROOT / "data" / "unused-floor4-test.sqlite3")
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == [TARGET]
    assert script.get_revision(TARGET).down_revision == PARENT

    spec = importlib.util.spec_from_file_location("p1_134_floor4_ledger", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = MIGRATION.read_text(encoding="utf-8")
    assert module.revision == TARGET
    assert module.down_revision == PARENT
    assert "INSERT INTO warehouse_areas" not in source
    assert "INSERT INTO warehouse_locations" not in source
    assert "INSERT INTO customer_finished_storage_area_preferences" not in source


def test_floor4_ledger_round_trip_creates_only_the_floor_shell(
    parent_database: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = _copy(parent_database, tmp_path / "round-trip.sqlite3")
    config = _config(monkeypatch, database_path)
    with sqlite3.connect(database_path) as connection:
        before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "customers",
                "warehouse_areas",
                "warehouse_locations",
                "customer_finished_storage_area_preferences",
                "inventory_lots",
            )
        }

    command.upgrade(config, TARGET)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT floor_code, floor_name, floor_number, construction_status, "
            "planning_reference_pallet_capacity FROM warehouse_floors "
            "WHERE floor_number=4"
        ).fetchone() == ("4F", "四楼", 4, "enabled", 0)
        assert {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        } == before
    assert _checks(database_path) == ("ok", [])

    command.downgrade(config, PARENT)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_floors WHERE floor_number=4"
        ).fetchone()[0] == 0
    command.upgrade(config, TARGET)
    assert _checks(database_path) == ("ok", [])


def test_floor4_ledger_preserves_a_compatible_preexisting_floor(
    parent_database: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = _copy(parent_database, tmp_path / "preexisting.sqlite3")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO warehouse_floors "
            "(floor_code,floor_name,floor_number,construction_status,remarks) "
            "VALUES ('4F','四楼',4,'enabled','现场已建立')"
        )
        connection.commit()
    config = _config(monkeypatch, database_path)
    command.upgrade(config, TARGET)
    command.downgrade(config, PARENT)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT floor_code, floor_name, remarks FROM warehouse_floors "
            "WHERE floor_number=4"
        ).fetchone() == ("4F", "四楼", "现场已建立")
    assert _checks(database_path) == ("ok", [])


@pytest.mark.parametrize(
    "floor_code,floor_name,floor_number,status",
    [
        ("4F", "错误四楼", 4, "enabled"),
        ("FOUR", "四楼", 4, "enabled"),
        ("4F", "四楼", 4, "not_started"),
    ],
)
def test_floor4_ledger_rejects_conflicting_existing_identity(
    parent_database: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    floor_code: str,
    floor_name: str,
    floor_number: int,
    status: str,
) -> None:
    database_path = _copy(
        parent_database,
        tmp_path / f"collision-{floor_code}-{status}.sqlite3",
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO warehouse_floors "
            "(floor_code,floor_name,floor_number,construction_status) "
            "VALUES (?,?,?,?)",
            (floor_code, floor_name, floor_number, status),
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="现有四楼台账"):
        command.upgrade(_config(monkeypatch, database_path), TARGET)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == PARENT
    assert _checks(database_path) == ("ok", [])


def test_floor4_ledger_downgrade_fails_closed_after_real_area_exists(
    parent_database: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = _copy(parent_database, tmp_path / "used-floor.sqlite3")
    config = _config(monkeypatch, database_path)
    command.upgrade(config, TARGET)
    with sqlite3.connect(database_path) as connection:
        floor_id = connection.execute(
            "SELECT id FROM warehouse_floors WHERE floor_number=4"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO warehouse_areas "
            "(floor_id,area_code,area_name,construction_status) "
            "VALUES (?,'XZ-4F-01','4F 新振成品区','enabled')",
            (floor_id,),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="四楼已建立区域、货位或容量计划"):
        command.downgrade(config, PARENT)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_areas WHERE floor_id=?",
            (floor_id,),
        ).fetchone()[0] == 1
    assert _checks(database_path) == ("ok", [])
