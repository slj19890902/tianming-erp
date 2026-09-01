from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "iw58v8x9z47"
TARGET_REVISION = "ix59v8x9z48"
INDEX_NAME = "uq_warehouse_locations_map_rack_cell"


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    resolved = database.resolve()
    formal_database = (PROJECT_ROOT / "data" / "carton_erp.sqlite3").resolve()
    assert resolved != formal_database
    assert resolved.is_relative_to(database.parent.resolve())
    monkeypatch.setenv("ERP_DATABASE_PATH", str(resolved))
    monkeypatch.setenv("ERP_BACKUP_DIR", str((database.parent / "backups").resolve()))
    monkeypatch.setenv("ERP_ENVIRONMENT", "development")
    monkeypatch.setenv("ERP_SECRET_KEY", "p1-133-rack-cell-migration-test")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{resolved.as_posix()}")
    return config


def _prepare_parent_schema(
    monkeypatch: pytest.MonkeyPatch,
    database: Path,
    rows: list[tuple[str, str, object, object, object]],
) -> Config:
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE warehouse_locations (
                id INTEGER PRIMARY KEY,
                location_code TEXT NOT NULL UNIQUE,
                address_kind TEXT NOT NULL,
                map_rack_id TEXT,
                level_no INTEGER,
                slot_no INTEGER
            );
            CREATE TABLE inventory_lots (
                id INTEGER PRIMARY KEY,
                location_id INTEGER NOT NULL,
                FOREIGN KEY(location_id) REFERENCES warehouse_locations(id)
                    ON DELETE RESTRICT
            );
            CREATE TABLE stocktake_orders (
                id INTEGER PRIMARY KEY,
                order_number TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL
            );
            CREATE TABLE stocktake_items (
                id INTEGER PRIMARY KEY,
                order_id INTEGER NOT NULL,
                FOREIGN KEY(order_id) REFERENCES stocktake_orders(id)
                    ON DELETE RESTRICT
            );
            CREATE TRIGGER trg_stocktake_items_insert_guard
            BEFORE INSERT ON stocktake_items
            WHEN NOT EXISTS (
                SELECT 1 FROM stocktake_orders
                WHERE id = NEW.order_id AND status = 'draft'
            )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'stocktake items may only be inserted into a draft order'
                );
            END;
            """
        )
        connection.executemany(
            "INSERT INTO warehouse_locations "
            "(id, location_code, address_kind, map_rack_id, level_no, slot_no) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (row_id, location_code, address_kind, rack_id, level_no, slot_no)
                for row_id, (
                    location_code,
                    address_kind,
                    rack_id,
                    level_no,
                    slot_no,
                ) in enumerate(rows, start=1)
            ],
        )
        connection.execute(
            "INSERT INTO inventory_lots (id, location_id) VALUES (1, 1)"
        )
        connection.commit()
    config = _config(monkeypatch, database)
    command.stamp(config, PARENT_REVISION)
    return config


def _index_sql(connection: sqlite3.Connection) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        (INDEX_NAME,),
    ).fetchone()
    return None if row is None else str(row[0])


def _assert_health(
    connection: sqlite3.Connection,
    revision: str,
    expected_location_count: int,
) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    assert connection.execute(
        "SELECT version_num FROM alembic_version"
    ).fetchone() == (revision,)
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute(
        "SELECT COUNT(*) FROM warehouse_locations"
    ).fetchone() == (expected_location_count,)
    assert connection.execute("SELECT COUNT(*) FROM inventory_lots").fetchone() == (1,)


def test_rack_cell_identity_model_declares_the_same_partial_unique_index() -> None:
    from app.models.stocktake import StocktakeOrder
    from app.models.warehouse_inventory import WarehouseLocation

    index = next(
        item for item in WarehouseLocation.__table__.indexes if item.name == INDEX_NAME
    )
    assert index.unique is True
    assert [column.name for column in index.columns] == [
        "map_rack_id",
        "level_no",
        "slot_no",
    ]
    assert str(index.dialect_options["sqlite"]["where"]) == "map_rack_id IS NOT NULL"
    assert (
        str(index.dialect_options["postgresql"]["where"])
        == "map_rack_id IS NOT NULL"
    )
    assert "location_address_version" in StocktakeOrder.__table__.columns
    assert "location_position_status" in StocktakeOrder.__table__.columns
    assert "published_map_revision" in StocktakeOrder.__table__.columns


def test_rack_cell_identity_upgrade_downgrade_upgrade_is_zero_backfill(
    current_alembic_head: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p1-133-rack-cell-roundtrip.sqlite3"
    rows = [
        ("LEGACY-UNBOUND", "legacy", None, None, None),
        ("F1-A-01-01", "rack_slot", "rack-f1-a", 1, 1),
    ]
    config = _prepare_parent_schema(monkeypatch, database, rows)
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == [current_alembic_head]
    assert TARGET_REVISION in {
        row.revision
        for row in script.walk_revisions(
            base=TARGET_REVISION,
            head=current_alembic_head,
        )
    }
    assert script.get_revision(TARGET_REVISION).down_revision == PARENT_REVISION

    with sqlite3.connect(database) as connection:
        table_sql_before = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='warehouse_locations'"
        ).fetchone()[0]
        rows_before = connection.execute(
            "SELECT id, location_code, address_kind, map_rack_id, level_no, slot_no "
            "FROM warehouse_locations ORDER BY id"
        ).fetchall()
        _assert_health(connection, PARENT_REVISION, 2)

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        _assert_health(connection, TARGET_REVISION, 2)
        assert connection.execute(
            "SELECT id, location_code, address_kind, map_rack_id, level_no, slot_no "
            "FROM warehouse_locations ORDER BY id"
        ).fetchall() == rows_before
        index_list = {
            row[1]: (row[2], row[4])
            for row in connection.execute("PRAGMA index_list('warehouse_locations')")
        }
        assert index_list[INDEX_NAME] == (1, 1)
        assert [
            row[2]
            for row in connection.execute(f"PRAGMA index_info('{INDEX_NAME}')")
        ] == ["map_rack_id", "level_no", "slot_no"]
        normalized_index_sql = " ".join((_index_sql(connection) or "").split())
        assert "WHERE map_rack_id IS NOT NULL" in normalized_index_sql
        assert {
            row[1] for row in connection.execute("PRAGMA table_info('stocktake_orders')")
        } >= {
            "location_address_version",
            "location_position_status",
            "published_map_revision",
        }
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='trigger' "
            "AND name='trg_stocktake_orders_location_identity_guard'"
        ).fetchone() == (1,)
        connection.execute(
            "INSERT INTO stocktake_orders "
            "(id, order_number, status, location_address_version, "
            "location_position_status, published_map_revision) "
            "VALUES (1, 'ST-IDENTITY', 'submitted', 1, 'published', 'map-v1')"
        )
        connection.commit()
        with pytest.raises(
            sqlite3.IntegrityError,
            match="submitted stocktake location identity is immutable",
        ):
            connection.execute(
                "UPDATE stocktake_orders SET location_address_version = 2 WHERE id = 1"
            )
        connection.rollback()
        connection.execute("DELETE FROM stocktake_orders WHERE id = 1")
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO warehouse_locations "
                "(id, location_code, address_kind, map_rack_id, level_no, slot_no) "
                "VALUES (3, 'DUPLICATE-CELL', 'rack_slot', 'rack-f1-a', 1, 1)"
            )
        connection.rollback()

    command.downgrade(config, PARENT_REVISION)
    with sqlite3.connect(database) as connection:
        _assert_health(connection, PARENT_REVISION, 2)
        assert _index_sql(connection) is None
        assert {
            row[1] for row in connection.execute("PRAGMA table_info('stocktake_orders')")
        } == {"id", "order_number", "status"}
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='trigger' AND name='trg_stocktake_items_insert_guard'"
        ).fetchone() == (1,)
        with pytest.raises(
            sqlite3.IntegrityError,
            match="stocktake items may only be inserted into a draft order",
        ):
            connection.execute(
                "INSERT INTO stocktake_items (id, order_id) VALUES (1, 999)"
            )
        connection.rollback()
        assert connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='warehouse_locations'"
        ).fetchone()[0] == table_sql_before
        assert connection.execute(
            "SELECT id, location_code, address_kind, map_rack_id, level_no, slot_no "
            "FROM warehouse_locations ORDER BY id"
        ).fetchall() == rows_before

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        _assert_health(connection, TARGET_REVISION, 2)
        assert _index_sql(connection) is not None
        assert {
            row[1] for row in connection.execute("PRAGMA table_info('stocktake_orders')")
        } >= {
            "location_address_version",
            "location_position_status",
            "published_map_revision",
        }
        assert connection.execute(
            "SELECT id, location_code, address_kind, map_rack_id, level_no, slot_no "
            "FROM warehouse_locations ORDER BY id"
        ).fetchall() == rows_before


def test_rack_cell_identity_upgrade_blocks_an_inflight_mobile_stocktake(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p1-133-pending-stocktake.sqlite3"
    config = _prepare_parent_schema(
        monkeypatch,
        database,
        [("LEGACY-UNBOUND", "legacy", None, None, None)],
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO stocktake_orders (id, order_number, status) "
            "VALUES (1, 'ST-PENDING', 'submitted')"
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="submitted stocktake must be reviewed first"):
        command.upgrade(config, TARGET_REVISION)

    with sqlite3.connect(database) as connection:
        _assert_health(connection, PARENT_REVISION, 1)
        assert _index_sql(connection) is None
        assert {
            row[1] for row in connection.execute("PRAGMA table_info('stocktake_orders')")
        } == {"id", "order_number", "status"}


INVALID_CASES = [
    pytest.param(
        [("EMPTY-RACK", "rack_slot", "", 1, 1)],
        "invalid existing rack binding",
        id="empty-rack-id",
    ),
    pytest.param(
        [("BLANK-RACK", "rack_slot", "   ", 1, 1)],
        "invalid existing rack binding",
        id="blank-rack-id",
    ),
    pytest.param(
        [("LEADING-SPACE-RACK", "rack_slot", " rack-a", 1, 1)],
        "invalid existing rack binding",
        id="leading-space-rack-id",
    ),
    pytest.param(
        [("TRAILING-SPACE-RACK", "rack_slot", "rack-a ", 1, 1)],
        "invalid existing rack binding",
        id="trailing-space-rack-id",
    ),
    pytest.param(
        [("NULL-LEVEL", "rack_slot", "rack-a", None, 1)],
        "invalid existing rack binding",
        id="null-level",
    ),
    pytest.param(
        [("BLANK-LEVEL", "rack_slot", "rack-a", "", 1)],
        "invalid existing rack binding",
        id="blank-level",
    ),
    pytest.param(
        [("LOW-LEVEL", "rack_slot", "rack-a", 0, 1)],
        "invalid existing rack binding",
        id="level-below-range",
    ),
    pytest.param(
        [("HIGH-LEVEL", "rack_slot", "rack-a", 100, 1)],
        "invalid existing rack binding",
        id="level-above-range",
    ),
    pytest.param(
        [("NULL-SLOT", "rack_slot", "rack-a", 1, None)],
        "invalid existing rack binding",
        id="null-slot",
    ),
    pytest.param(
        [("BLANK-SLOT", "rack_slot", "rack-a", 1, "")],
        "invalid existing rack binding",
        id="blank-slot",
    ),
    pytest.param(
        [("LOW-SLOT", "rack_slot", "rack-a", 1, 0)],
        "invalid existing rack binding",
        id="slot-below-range",
    ),
    pytest.param(
        [("HIGH-SLOT", "rack_slot", "rack-a", 1, 100)],
        "invalid existing rack binding",
        id="slot-above-range",
    ),
    pytest.param(
        [("WRONG-KIND", "legacy", "rack-a", 1, 1)],
        "invalid existing rack binding",
        id="non-rack-slot",
    ),
    pytest.param(
        [
            ("DUPLICATE-A", "rack_slot", "rack-a", 1, 1),
            ("DUPLICATE-B", "rack_slot", "rack-a", 1, 1),
        ],
        "duplicate existing rack-cell key",
        id="duplicate-rack-cell",
    ),
]


@pytest.mark.parametrize(("rows", "message"), INVALID_CASES)
def test_rack_cell_identity_upgrade_blocks_invalid_existing_data_without_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    rows: list[tuple[str, str, object, object, object]],
    message: str,
) -> None:
    database = tmp_path / "p1-133-rack-cell-invalid.sqlite3"
    config = _prepare_parent_schema(monkeypatch, database, rows)

    with pytest.raises(RuntimeError, match=message):
        command.upgrade(config, TARGET_REVISION)

    with sqlite3.connect(database) as connection:
        _assert_health(connection, PARENT_REVISION, len(rows))
        assert _index_sql(connection) is None
        assert connection.execute(
            "SELECT location_code, address_kind, map_rack_id, level_no, slot_no "
            "FROM warehouse_locations ORDER BY id"
        ).fetchall() == rows
