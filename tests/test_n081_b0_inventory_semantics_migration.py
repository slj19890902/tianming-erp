from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from types import ModuleType

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import sqlalchemy as sa


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "cp72v8x9z61"
TARGET_REVISION = "cj66v8x9z55"
MIGRATION_PATH = (
    PROJECT_ROOT
    / "alembic"
    / "versions"
    / "cj66v8x9z55_n081_b0_inventory_semantics.py"
)


def _load_migration_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "cj66v8x9z55_n081_b0_inventory_semantics_under_test",
        MIGRATION_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "n081-b0-migration-test")
    return Config(str(PROJECT_ROOT / "alembic.ini"))


def _health(connection: sqlite3.Connection, revision: str) -> None:
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute(
        "SELECT version_num FROM alembic_version"
    ).fetchone() == (revision,)


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _inventory_lot_related_triggers(
    connection: sqlite3.Connection,
) -> dict[str, str]:
    return {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'trigger' AND sql IS NOT NULL "
            "AND INSTR(LOWER(sql), 'inventory_lots') > 0 "
            "ORDER BY name"
        )
    }


def test_sqlite_rebuild_failure_restores_all_related_triggers(
    tmp_path: Path,
) -> None:
    database = tmp_path / "n081-b0-trigger-restore.sqlite3"
    engine = sa.create_engine(f"sqlite:///{database}")
    migration = _load_migration_module()

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE inventory_lots ("
            "id INTEGER PRIMARY KEY, "
            "stock_date_accuracy TEXT, "
            "stock_date_original_text TEXT)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE warehouse_locations (id INTEGER PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "CREATE TRIGGER "
            "trg_inventory_lots_unknown_date_source_insert "
            "BEFORE INSERT ON inventory_lots "
            "WHEN NEW.stock_date_accuracy = 'unknown' "
            "BEGIN SELECT RAISE(ABORT, 'missing source'); END"
        )
        connection.exec_driver_sql(
            "CREATE TRIGGER "
            "trg_inventory_lots_require_placed_location_insert "
            "BEFORE INSERT ON inventory_lots "
            "WHEN NEW.id < 0 "
            "BEGIN SELECT RAISE(ABORT, 'invalid lot'); END"
        )
        connection.exec_driver_sql(
            "CREATE TRIGGER "
            "trg_warehouse_locations_unplace_reference_guard "
            "BEFORE UPDATE ON warehouse_locations "
            "WHEN EXISTS (SELECT 1 FROM inventory_lots) "
            "BEGIN SELECT RAISE(ABORT, 'location in use'); END"
        )
        trigger_query = (
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'trigger' AND sql IS NOT NULL "
            "AND INSTR(LOWER(sql), 'inventory_lots') > 0 "
            "ORDER BY name"
        )
        before = dict(connection.exec_driver_sql(trigger_query).all())
        assert len(before) == 3
        forced_error = RuntimeError("forced batch rebuild failure")

        def fail_rebuild() -> None:
            assert connection.exec_driver_sql(trigger_query).all() == []
            raise forced_error

        with pytest.raises(RuntimeError) as caught:
            migration._run_sqlite_inventory_lot_rebuild(
                connection,
                fail_rebuild,
            )
        assert caught.value is forced_error

        after = dict(connection.exec_driver_sql(trigger_query).all())
        assert after == before

    engine.dispose()


def test_cj66_linearly_descends_from_cp72_on_the_unique_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = ScriptDirectory.from_config(
        _config(monkeypatch, tmp_path / "lineage.sqlite3")
    )

    heads = script.get_heads()
    assert len(heads) == 1
    assert TARGET_REVISION in {
        revision.revision
        for revision in script.iterate_revisions(heads[0], "base")
    }
    assert script.get_revision(TARGET_REVISION).down_revision == PREVIOUS_REVISION


def test_cp72_to_cj66_marks_legacy_dates_unknown_and_round_trips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "n081-b0.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, PREVIOUS_REVISION)

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        location_id = int(
            connection.execute(
            """
            INSERT INTO warehouse_locations (
                location_code, location_name, warehouse_type, warehouse_floor,
                area_code, storage_type, placement_status
            ) VALUES (
                'N081-B0-01', 'N081 B0 migration test',
                'semi_finished', 1, 'B0', 'ground', 'placed'
            )
            """
            ).lastrowid
        )
        connection.execute(
            """
            INSERT INTO inventory_lots (
                id, lot_number, inventory_type, warehouse_location_id,
                quantity_available, quantity_reserved, quantity_consumed,
                quantity_damaged, quantity_scrapped, unit, status, source_type,
                stock_date, last_movement_at, version, created_at
            ) VALUES (
                10001, 'N081-B0-LEGACY', 'semi_finished', ?,
                20, 0, 0, 0, 0, 'sheets', 'active', 'manual',
                '2026-07-01', '2026-07-01 08:00:00', 1,
                '2026-07-01 08:00:00'
            )
            """,
            (location_id,),
        )
        connection.commit()
        _health(connection, PREVIOUS_REVISION)
        pre_b0_triggers = _inventory_lot_related_triggers(connection)
        assert {
            "trg_inventory_lots_require_placed_location_insert",
            "trg_inventory_lots_require_placed_location_update",
            "trg_warehouse_locations_unplace_reference_guard",
        } <= pre_b0_triggers.keys()

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        _health(connection, TARGET_REVISION)
        assert {
            "stock_date_accuracy",
            "stock_date_original_text",
        } <= _columns(connection, "inventory_lots")
        assert connection.execute(
            """
            SELECT stock_date_accuracy, stock_date_original_text
            FROM inventory_lots WHERE id = 10001
            """
        ).fetchone() == ("unknown", None)
        with pytest.raises(
            sqlite3.IntegrityError,
            match="new unknown stock date requires original text",
        ):
            connection.execute(
                """
                INSERT INTO inventory_lots (
                    id, lot_number, inventory_type, warehouse_location_id,
                    quantity_available, quantity_reserved, quantity_consumed,
                    quantity_damaged, quantity_scrapped, unit, status,
                    source_type, stock_date, stock_date_accuracy,
                    stock_date_original_text, last_movement_at, version,
                    created_at
                ) VALUES (
                    10003, 'N081-B0-INVALID-UNKNOWN', 'semi_finished', ?,
                    1, 0, 0, 0, 0, 'sheets', 'active', 'stocktake',
                    '2026-07-23', 'unknown', NULL,
                    '2026-07-23 08:00:00', 1, '2026-07-23 08:00:00'
                )
                """,
                (location_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE inventory_lots
                SET stock_date_accuracy = 'guessed'
                WHERE id = 10001
                """
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="new unknown stock date requires original text",
        ):
            connection.execute(
                """
                UPDATE inventory_lots
                SET stock_date = '2026-07-02'
                WHERE id = 10001
                """
            )

    command.downgrade(config, PREVIOUS_REVISION)
    with sqlite3.connect(database) as connection:
        _health(connection, PREVIOUS_REVISION)
        assert "stock_date_accuracy" not in _columns(connection, "inventory_lots")
        assert connection.execute(
            "SELECT lot_number, quantity_available FROM inventory_lots WHERE id = 10001"
        ).fetchone() == ("N081-B0-LEGACY", 20)
        assert _inventory_lot_related_triggers(connection) == pre_b0_triggers

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        _health(connection, TARGET_REVISION)


def test_head_deep_downgrade_through_cj66_round_trips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "n081-b0-head-deep-round-trip.sqlite3"
    config = _config(monkeypatch, database)

    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        head_revision = str(
            connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
        )
        _health(connection, head_revision)
        assert {
            "stock_date_accuracy",
            "stock_date_original_text",
        } <= _columns(connection, "inventory_lots")

    command.downgrade(config, PREVIOUS_REVISION)
    with sqlite3.connect(database) as connection:
        _health(connection, PREVIOUS_REVISION)
        assert "stock_date_accuracy" not in _columns(connection, "inventory_lots")
        assert "stock_date_original_text" not in _columns(
            connection,
            "inventory_lots",
        )
        assert {
            "trg_inventory_lots_require_placed_location_insert",
            "trg_inventory_lots_require_placed_location_update",
            "trg_warehouse_locations_unplace_reference_guard",
        } <= _inventory_lot_related_triggers(connection).keys()
        assert not {
            "trg_inventory_lots_unknown_date_source_insert",
            "trg_inventory_lots_unknown_date_source_update",
        } & _inventory_lot_related_triggers(connection).keys()

    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        _health(connection, head_revision)
        assert {
            "stock_date_accuracy",
            "stock_date_original_text",
        } <= _columns(connection, "inventory_lots")


def test_cj66_downgrade_fails_closed_after_date_fact_is_recorded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "n081-b0-downgrade-guard.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, TARGET_REVISION)

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        location_id = int(
            connection.execute(
            """
            INSERT INTO warehouse_locations (
                location_code, location_name, warehouse_type, warehouse_floor,
                area_code, storage_type, placement_status
            ) VALUES (
                'N081-B0-02', 'N081 B0 downgrade test',
                'semi_finished', 1, 'B0', 'ground', 'placed'
            )
            """
            ).lastrowid
        )
        connection.execute(
            """
            INSERT INTO inventory_lots (
                id, lot_number, inventory_type, warehouse_location_id,
                quantity_available, quantity_reserved, quantity_consumed,
                quantity_damaged, quantity_scrapped, unit, status, source_type,
                stock_date, stock_date_accuracy, stock_date_original_text,
                last_movement_at, version, created_at
            ) VALUES (
                10002, 'N081-B0-FACT', 'semi_finished', ?,
                20, 0, 0, 0, 0, 'sheets', 'active', 'stocktake',
                '2026-07-01', 'unknown', '未提供',
                '2026-07-23 08:00:00', 1, '2026-07-23 08:00:00'
            )
            """,
            (location_id,),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="stock-date facts exist"):
        command.downgrade(config, PREVIOUS_REVISION)

    with sqlite3.connect(database) as connection:
        _health(connection, TARGET_REVISION)
        assert connection.execute(
            "SELECT stock_date_accuracy FROM inventory_lots WHERE id = 10002"
        ).fetchone() == ("unknown",)
