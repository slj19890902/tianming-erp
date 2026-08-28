from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


OLD_HEAD = "gq52v8x9z41"
NEW_HEAD = "gr53v8x9z42"


def _config(database_path: Path) -> Config:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url", f"sqlite+pysqlite:///{database_path.as_posix()}"
    )
    return config


def test_p1_123_migration_roundtrip_and_fact_guard(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "p1-123-migration.sqlite3"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE users (id INTEGER PRIMARY KEY);
            CREATE TABLE warehouse_areas (id INTEGER PRIMARY KEY);
            CREATE TABLE warehouse_locations (
                id INTEGER PRIMARY KEY,
                location_code TEXT NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT 1
            );
            INSERT INTO warehouse_locations (id, location_code, is_active)
            VALUES (1, 'LEGACY-KEEP', 1);
            CREATE TABLE inventory_pallets (
                id INTEGER PRIMARY KEY,
                location_id INTEGER
            );
            CREATE TRIGGER trg_inventory_pallets_require_location
            BEFORE INSERT ON inventory_pallets
            FOR EACH ROW
            WHEN NEW.location_id IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM warehouse_locations
                     WHERE id = NEW.location_id
                 )
            BEGIN
                SELECT RAISE(ABORT, 'location missing');
            END;
            """
        )
    config = _config(database_path)
    command.stamp(config, OLD_HEAD)
    command.upgrade(config, NEW_HEAD)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("pragma integrity_check").fetchone()[0] == "ok"
        assert not connection.execute("pragma foreign_key_check").fetchall()
        assert connection.execute(
            "select version_num from alembic_version"
        ).fetchone()[0] == NEW_HEAD
        columns = {
            row[1]
            for row in connection.execute("pragma table_info(warehouse_locations)")
        }
        assert {"map_rack_id", "rack_display_name"}.issubset(columns)
        assert connection.execute(
            "select location_code, map_rack_id from warehouse_locations where id = 1"
        ).fetchone() == ("LEGACY-KEEP", None)
        assert connection.execute(
            "select count(*) from sqlite_master "
            "where type='trigger' and name='trg_inventory_pallets_require_location'"
        ).fetchone()[0] == 1
        connection.execute(
            "insert into inventory_pallets (id, location_id) values (1, 1)"
        )

    command.downgrade(config, OLD_HEAD)
    command.upgrade(config, NEW_HEAD)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "update warehouse_locations set map_rack_id = 'rack-guard', "
            "rack_display_name = '有历史的货架' where id = 1"
        )
        connection.commit()
    try:
        command.downgrade(config, OLD_HEAD)
    except RuntimeError as error:
        assert "cannot downgrade P1-123" in str(error)
    else:  # pragma: no cover
        raise AssertionError("rack bindings must block destructive downgrade")
