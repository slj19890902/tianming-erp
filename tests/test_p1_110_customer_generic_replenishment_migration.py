from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "fj45v8x9z34"
TARGET_REVISION = "fk46v8x9z35"
CURRENT_HEAD_REVISION = "go50v8x9z39"


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    return config


def _parent_schema(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        connection.executescript(
            f"""
            PRAGMA foreign_keys=ON;
            CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL);
            INSERT INTO alembic_version VALUES ('{PARENT_REVISION}');
            CREATE TABLE products (id INTEGER PRIMARY KEY);
            CREATE TABLE stock_replenishment_order_items (
                id INTEGER PRIMARY KEY
            );
            CREATE TABLE semi_finished_inventory_details (
                inventory_lot_id INTEGER PRIMARY KEY,
                owner_customer_id INTEGER
            );
            """
        )


def test_customer_generic_replenishment_migration_round_trips_and_is_unique_head(
    current_alembic_head: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    assert current_alembic_head == CURRENT_HEAD_REVISION
    database = tmp_path / "p1-110-roundtrip.sqlite3"
    _parent_schema(database)
    config = _config(monkeypatch, database)

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        replenishment_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(stock_replenishment_order_items)"
            )
        }
        semi_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(semi_finished_inventory_details)"
            )
        }
        assert {"reference_product_id", "internal_name"} <= replenishment_columns
        assert {"customer_generic_eligible", "internal_name"} <= semi_columns
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()

    command.downgrade(config, PARENT_REVISION)
    with sqlite3.connect(database) as connection:
        assert "reference_product_id" not in {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(stock_replenishment_order_items)"
            )
        }
        assert "customer_generic_eligible" not in {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(semi_finished_inventory_details)"
            )
        }


def test_customer_generic_replenishment_migration_refuses_fact_downgrade(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p1-110-fail-closed.sqlite3"
    _parent_schema(database)
    config = _config(monkeypatch, database)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO products (id) VALUES (1)")
        connection.execute(
            "INSERT INTO stock_replenishment_order_items "
            "(id,reference_product_id,internal_name) VALUES (1,1,'customer reserve')"
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="customer-generic replenishment facts"):
        command.downgrade(config, PARENT_REVISION)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET_REVISION
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_customer_generic_replenishment_migration_recovers_fully_applied_unstamped_schema(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p1-110-fully-applied-unstamped.sqlite3"
    _parent_schema(database)
    config = _config(monkeypatch, database)
    command.upgrade(config, TARGET_REVISION)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE alembic_version SET version_num = ?",
            (PARENT_REVISION,),
        )
        connection.commit()

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET_REVISION
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
