from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text


OLD_HEAD = "jb63v8x9z52"
NEW_HEAD = "jc64v8x9z53"


def _config() -> Config:
    return Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))


def _prepare_old_schema(tmp_path: Path, monkeypatch, name: str):
    from app.core.database import create_sqlite_engine
    from app.models import Base

    database_path = tmp_path / name
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    engine = create_sqlite_engine(database_path)
    Base.metadata.create_all(engine)
    config = _config()
    command.stamp(config, NEW_HEAD)
    command.downgrade(config, OLD_HEAD)
    return engine, config


def test_revision_migration_backfills_current_rows_and_keeps_one_head(
    tmp_path: Path,
    monkeypatch,
) -> None:
    engine, config = _prepare_old_schema(
        tmp_path,
        monkeypatch,
        "p1-137b-migration.sqlite3",
    )
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=OFF"))
        connection.execute(
            text(
                "INSERT INTO sales_deliveries "
                "(id, delivery_number, customer_id, delivery_date, source_mode, "
                "status, total_quantity, version) "
                "VALUES (1, 'P1-137B-MIG', 999999, '2026-09-01', "
                "'order', 'dispatched', 5, 1)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO sales_delivery_items "
                "(id, delivery_id, source_type, order_item_id, delivered_quantity) "
                "VALUES (1, 1, 'order', 888888, 5)"
            )
        )

    command.upgrade(config, NEW_HEAD)
    with engine.connect() as connection:
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("sales_delivery_items")
        }
        assert {"revision_number", "is_current"}.issubset(columns)
        assert connection.execute(
            text(
                "SELECT revision_number, is_current "
                "FROM sales_delivery_items WHERE id = 1"
            )
        ).one() == (1, 1)
        assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == NEW_HEAD

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE sales_delivery_items SET is_current = 0 WHERE id = 1"
            )
        )
        connection.execute(
            text(
                "INSERT INTO sales_delivery_items "
                "(id, delivery_id, source_type, revision_number, is_current, "
                "order_item_id, delivered_quantity) "
                "VALUES (2, 1, 'order', 2, 1, 888888, 4)"
            )
        )
        with pytest.raises(Exception):
            connection.execute(
                text(
                    "INSERT INTO sales_delivery_items "
                    "(id, delivery_id, source_type, revision_number, is_current, "
                    "order_item_id, delivered_quantity) "
                    "VALUES (3, 1, 'order', 3, 1, 888888, 3)"
                )
            )

    with pytest.raises(RuntimeError, match="delivery revision history exists"):
        command.downgrade(config, OLD_HEAD)


def test_revision_migration_clean_roundtrip(
    tmp_path: Path,
    monkeypatch,
) -> None:
    engine, config = _prepare_old_schema(
        tmp_path,
        monkeypatch,
        "p1-137b-roundtrip.sqlite3",
    )
    command.upgrade(config, NEW_HEAD)
    command.downgrade(config, OLD_HEAD)
    command.upgrade(config, NEW_HEAD)
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == NEW_HEAD
