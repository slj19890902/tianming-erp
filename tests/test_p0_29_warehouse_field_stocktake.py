from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text


ROOT = Path(__file__).resolve().parents[1]
PARENT = "fl47v8x9z36"
TARGET = "gm48v8x9z37"
TABLE = "warehouse_unmatched_inventory_observations"


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def _seed_parent_schema(database: Path) -> None:
    """Build the exact dependency surface for this leaf migration.

    Earlier formal-data migrations intentionally refuse to run against an empty
    database without their audited source facts, so this isolated migration test
    starts from the declared parent revision and the three referenced tables.
    """

    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO alembic_version(version_num) VALUES (?)", (PARENT,)
        )
        connection.exec_driver_sql(
            "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE warehouse_locations (id INTEGER PRIMARY KEY AUTOINCREMENT)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE inventory_lots (id INTEGER PRIMARY KEY AUTOINCREMENT)"
        )
    engine.dispose()


def test_unmatched_inventory_observation_migration_round_trip_when_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p0-29-empty.sqlite3"
    config = _config(monkeypatch, database)
    _seed_parent_schema(database)

    command.upgrade(config, TARGET)
    engine = create_engine(f"sqlite:///{database}")
    inspector = inspect(engine)
    assert TABLE in inspector.get_table_names()
    assert {
        "observed_location_id",
        "observed_location_layout_version",
        "customer_keyword",
        "inventory_keyword",
        "reported_quantity",
        "reason",
        "status",
        "version",
        "idempotency_key",
        "resolved_inventory_lot_id",
    }.issubset({column["name"] for column in inspector.get_columns(TABLE)})
    assert {
        "ix_warehouse_unmatched_observations_location_status",
        "ix_warehouse_unmatched_observations_status_reported",
    }.issubset({index["name"] for index in inspector.get_indexes(TABLE)})
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
    engine.dispose()

    command.downgrade(config, PARENT)
    engine = create_engine(f"sqlite:///{database}")
    assert TABLE not in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
    engine.dispose()

    command.upgrade(config, TARGET)
    engine = create_engine(f"sqlite:///{database}")
    assert TABLE in inspect(engine).get_table_names()
    engine.dispose()


def test_unmatched_inventory_observation_facts_block_destructive_downgrade(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p0-29-fact.sqlite3"
    config = _config(monkeypatch, database)
    _seed_parent_schema(database)
    command.upgrade(config, TARGET)
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(
            text(
                f"INSERT INTO {TABLE} "
                "(observed_location_id, observed_location_layout_version, "
                "inventory_keyword, reason, status, version, idempotency_key) "
                "VALUES (1, 1, 'CPN-P0-29', '现场实物未匹配', 'open', 1, "
                "'p0-29-migration-fact')"
            )
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="contains review facts"):
        command.downgrade(config, PARENT)

    engine = create_engine(f"sqlite:///{database}")
    assert TABLE in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.scalar(text(f"SELECT COUNT(*) FROM {TABLE}")) == 1
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
    engine.dispose()
