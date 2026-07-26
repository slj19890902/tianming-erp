from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import sqlite3

from alembic import command
from alembic.config import Config
import pytest


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "cq73v8x9z62"
TARGET_REVISION = "cr74v8x9z63"
MIGRATION = (
    ROOT
    / "alembic"
    / "versions"
    / "cr74v8x9z63_floor3_space_ledger_compat.py"
)
FACT_TABLES = (
    "warehouse_locations",
    "floor3_location_layouts",
    "inventory_lots",
    "inventory_pallets",
    "inventory_reservations",
    "inventory_movements",
    "inventory_location_movements",
)


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _run_alembic(path: Path, operation: str, revision: str) -> None:
    previous = os.environ.get("ERP_DATABASE_PATH")
    os.environ["ERP_DATABASE_PATH"] = str(path)
    try:
        getattr(command, operation)(_config(path), revision)
    finally:
        if previous is None:
            os.environ.pop("ERP_DATABASE_PATH", None)
        else:
            os.environ["ERP_DATABASE_PATH"] = previous


def _table_digest(connection: sqlite3.Connection, table: str) -> str:
    digest = hashlib.sha256()
    for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid"):
        digest.update(repr(row).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _fact_digests(path: Path) -> dict[str, str]:
    with sqlite3.connect(path) as connection:
        return {table: _table_digest(connection, table) for table in FACT_TABLES}


def _checks(path: Path) -> tuple[str, int]:
    with sqlite3.connect(path) as connection:
        return (
            connection.execute("PRAGMA integrity_check").fetchone()[0],
            len(connection.execute("PRAGMA foreign_key_check").fetchall()),
        )


@pytest.fixture(scope="module")
def cq_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("floor3-space-ledger") / "cq73.sqlite3"
    _run_alembic(path, "upgrade", PARENT_REVISION)
    return path


def _copy_database(source: Path, target: Path) -> Path:
    shutil.copy2(source, target)
    return target


def test_floor3_compat_migration_is_linear_and_read_only_for_existing_facts() -> None:
    spec = importlib.util.spec_from_file_location("floor3_space_ledger_compat", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    source = MIGRATION.read_text(encoding="utf-8")
    assert module.revision == TARGET_REVISION
    assert module.down_revision == PARENT_REVISION
    assert "SEED_EXPECTED_COUNT = 396" in source
    assert "planned_pallet_capacity" in source
    assert "UPDATE warehouse_locations" not in source
    assert "DELETE FROM warehouse_locations" not in source
    assert "INSERT INTO warehouse_locations" not in source


def test_floor3_compat_round_trip_preserves_every_existing_fact(
    cq_database: Path,
    tmp_path: Path,
) -> None:
    path = _copy_database(cq_database, tmp_path / "floor3-compat-round-trip.sqlite3")
    before = _fact_digests(path)
    with sqlite3.connect(path) as connection:
        expected_locations, expected_capacity = connection.execute(
            """
            SELECT COUNT(*),
                   SUM(CASE WHEN storage_type IN ('ground','temporary_aisle')
                            THEN 1 ELSE 0 END)
            FROM warehouse_locations
            WHERE source_version = 'V11'
            """
        ).fetchone()
        expected_c1 = connection.execute(
            """
            SELECT COUNT(*)
            FROM warehouse_locations
            WHERE source_version = 'V11' AND area_code = 'C1'
            """
        ).fetchone()[0]

    _run_alembic(path, "upgrade", TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        floor = connection.execute(
            """
            SELECT floor_code, floor_name, floor_number, construction_status
            FROM warehouse_floors
            """
        ).fetchone()
        assert floor == ("3F", "三楼", 3, "enabled")
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_areas"
        ).fetchone()[0] == 22
        assert connection.execute(
            "SELECT SUM(planned_location_count) FROM warehouse_areas"
        ).fetchone()[0] == expected_locations
        assert connection.execute(
            "SELECT SUM(planned_pallet_capacity) FROM warehouse_areas"
        ).fetchone()[0] == expected_capacity
        assert connection.execute(
            """
            SELECT planned_location_count, planned_pallet_capacity
            FROM warehouse_areas
            WHERE area_code = 'C1'
            """
        ).fetchone() == (expected_c1, expected_c1)
        assert connection.execute(
            """
            SELECT planned_location_count, planned_pallet_capacity, remarks
            FROM warehouse_areas
            WHERE area_code = 'F12'
            """
        ).fetchone() == (
            8,
            8,
            "V11 过道临放位；不是普通长期货位，现有布局与业务门禁保持不变。",
        )
    assert _fact_digests(path) == before
    assert _checks(path) == ("ok", 0)

    _run_alembic(path, "downgrade", PARENT_REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_floors"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_areas"
        ).fetchone()[0] == 0
    assert _fact_digests(path) == before
    assert _checks(path) == ("ok", 0)

    _run_alembic(path, "upgrade", TARGET_REVISION)
    assert _fact_digests(path) == before
    assert _checks(path) == ("ok", 0)


def test_floor3_compat_includes_existing_c1_extensions_without_rewriting_them(
    cq_database: Path,
    tmp_path: Path,
) -> None:
    path = _copy_database(cq_database, tmp_path / "floor3-compat-extensions.sqlite3")
    with sqlite3.connect(path) as connection:
        for offset, code in enumerate(("C1-R12", "C1-R13"), start=1):
            cursor = connection.execute(
                """
                INSERT INTO warehouse_locations (
                    location_code, location_name, warehouse_type, is_active,
                    warehouse_floor, area_code, storage_type, side_code,
                    sort_order, is_temporary, source_version, placement_status
                ) VALUES (?, ?, 'finished', 1, 3, 'C1', 'ground', 'R',
                          ?, 0, 'V11', 'placed')
                """,
                (code, code, 396 + offset),
            )
            connection.execute(
                """
                INSERT INTO floor3_location_layouts (
                    location_id, left_pct, top_pct, width_pct, height_pct,
                    z_index, version, source_type
                ) VALUES (?, 50, ?, 50, 7, 0, 1, 'seeded')
                """,
                (cursor.lastrowid, 77 + offset * 7),
            )
        connection.commit()
    before = _fact_digests(path)

    _run_alembic(path, "upgrade", TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT SUM(planned_location_count) FROM warehouse_areas"
        ).fetchone()[0] == 398
        assert connection.execute(
            "SELECT SUM(planned_pallet_capacity) FROM warehouse_areas"
        ).fetchone()[0] == 284
        assert connection.execute(
            """
            SELECT planned_location_count, planned_pallet_capacity
            FROM warehouse_areas
            WHERE area_code = 'C1'
            """
        ).fetchone() == (24, 24)
    assert _fact_digests(path) == before
    assert _checks(path) == ("ok", 0)


def test_floor3_compat_upgrade_fails_before_write_on_floor_collision(
    cq_database: Path,
    tmp_path: Path,
) -> None:
    path = _copy_database(cq_database, tmp_path / "floor3-compat-collision.sqlite3")
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO warehouse_floors (
                floor_code, floor_name, floor_number, construction_status
            ) VALUES ('3F', '三楼人工台账', 3, 'ledger_building')
            """
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="三楼楼层台账已存在"):
        _run_alembic(path, "upgrade", TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == PARENT_REVISION
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_areas"
        ).fetchone()[0] == 0
    assert _checks(path) == ("ok", 0)


def test_floor3_compat_downgrade_fails_closed_after_area_edit(
    cq_database: Path,
    tmp_path: Path,
) -> None:
    path = _copy_database(cq_database, tmp_path / "floor3-compat-edited.sqlite3")
    _run_alembic(path, "upgrade", TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE warehouse_areas
            SET planned_pallet_capacity = planned_pallet_capacity + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE area_code = 'C1'
            """
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="禁止破坏性降级"):
        _run_alembic(path, "downgrade", PARENT_REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET_REVISION
    assert _checks(path) == ("ok", 0)
