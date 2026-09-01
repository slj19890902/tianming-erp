from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest


ROOT = Path(__file__).resolve().parents[1]
PARENT = "iz61v8x9z50"
TARGET = "ja62v8x9z51"
MIGRATION = (
    ROOT
    / "alembic"
    / "versions"
    / "ja62v8x9z51_p1_134_customer_finished_storage_areas.py"
)


def _config(monkeypatch: pytest.MonkeyPatch, database_path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database_path.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "p1-134-migration-test")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option(
        "sqlalchemy.url", f"sqlite+pysqlite:///{database_path.as_posix()}"
    )
    return config


def test_p1_134_migration_is_linear() -> None:
    spec = importlib.util.spec_from_file_location("p1_134_migration", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == TARGET
    assert module.down_revision == PARENT


def test_p1_134_migration_round_trips_and_fails_closed_after_customer_fact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "p1-134-migration.sqlite3"
    config = _config(monkeypatch, database_path)
    command.upgrade(config, PARENT)
    with sqlite3.connect(database_path) as connection:
        core_counts_before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("customers", "warehouse_areas", "users")
        }
    command.upgrade(config, TARGET)

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(customer_finished_storage_area_preferences)"
            )
        }
        assert {
            "id",
            "customer_id",
            "warehouse_area_id",
            "priority",
            "created_by",
            "created_at",
        } == columns
        foreign_keys = {
            row[3]: (row[2], row[4], row[6])
            for row in connection.execute(
                "PRAGMA foreign_key_list(customer_finished_storage_area_preferences)"
            )
        }
        assert foreign_keys == {
            "customer_id": ("customers", "id", "CASCADE"),
            "warehouse_area_id": ("warehouse_areas", "id", "RESTRICT"),
            "created_by": ("users", "id", "SET NULL"),
        }
        assert {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("customers", "warehouse_areas", "users")
        } == core_counts_before
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    command.downgrade(config, PARENT)
    command.upgrade(config, TARGET)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        customer_id = connection.execute(
            "INSERT INTO customers (customer_number, customer_code, name) "
            "VALUES (9134, 'P1-134-MIG', 'P1-134 迁移客户') RETURNING id"
        ).fetchone()[0]
        area_ids = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM warehouse_areas ORDER BY id LIMIT 2"
            ).fetchall()
        ]
        assert len(area_ids) == 2
        area_id, second_area_id = area_ids
        connection.execute(
            "INSERT INTO customer_finished_storage_area_preferences "
            "(customer_id, warehouse_area_id, priority) VALUES (?, ?, 1)",
            (customer_id, area_id),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO customer_finished_storage_area_preferences "
                "(customer_id, warehouse_area_id, priority) VALUES (?, ?, 2)",
                (customer_id, area_id),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO customer_finished_storage_area_preferences "
                "(customer_id, warehouse_area_id, priority) VALUES (?, ?, 1)",
                (customer_id, second_area_id),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO customer_finished_storage_area_preferences "
                "(customer_id, warehouse_area_id, priority) VALUES (?, ?, 0)",
                (customer_id, second_area_id),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM warehouse_areas WHERE id = ?", (area_id,))
        connection.rollback()

        connection.execute("DELETE FROM customers WHERE id = ?", (customer_id,))
        assert connection.execute(
            "SELECT COUNT(*) FROM customer_finished_storage_area_preferences "
            "WHERE customer_id = ?",
            (customer_id,),
        ).fetchone()[0] == 0

        surviving_customer_id = connection.execute(
            "INSERT INTO customers (customer_number, customer_code, name) "
            "VALUES (9135, 'P1-134-MIG-SURVIVE', 'P1-134 降级门禁客户') RETURNING id"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO customer_finished_storage_area_preferences "
            "(customer_id, warehouse_area_id, priority) VALUES (?, ?, 1)",
            (surviving_customer_id, second_area_id),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="cannot downgrade P1-134"):
        command.downgrade(config, PARENT)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET
        assert connection.execute(
            "SELECT customer_id, warehouse_area_id, priority "
            "FROM customer_finished_storage_area_preferences"
        ).fetchone() == (surviving_customer_id, second_area_id, 1)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
