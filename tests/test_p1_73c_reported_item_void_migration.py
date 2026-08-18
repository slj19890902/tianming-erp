from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


ROOT = Path(__file__).resolve().parents[1]


def _config(monkeypatch: pytest.MonkeyPatch, database_path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    return config


def test_p1_73c_migration_round_trip_and_legacy_backfill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = tmp_path / "p1_73c.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, "tt28v8x9z17")
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users "
                "(username, password_hash, role, real_name, display_name, "
                "must_change_password, is_active) "
                "VALUES ('p173c', 'x', 'admin', 'P1-73C', 'P1-73C', 0, 1)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO supplier_requisition_orders "
                "(order_number, total_quantity, stock_deduction_qty, requisition_qty, status) "
                "VALUES ('P173-MIG', 10, 0, 10, 'confirmed')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO supplier_requisition_order_items "
                "(supplier_order_id, quantity, stock_deduction_qty, requisition_qty) "
                "VALUES (1, 10, 0, 10)"
            )
        )
    command.upgrade(config, "uu29v8x9z18")
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT status, version, voided_at, void_idempotency_key "
                "FROM supplier_requisition_order_items WHERE id = 1"
            )
        ).one()
        assert tuple(row) == ("active", 1, None, None)
        assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    command.downgrade(config, "tt28v8x9z17")
    assert "status" not in {
        column["name"] for column in inspect(engine).get_columns("supplier_requisition_order_items")
    }
    command.upgrade(config, "uu29v8x9z18")


def test_p1_73c_downgrade_refuses_durable_void_facts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = tmp_path / "p1_73c_fact.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, "uu29v8x9z18")
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users "
                "(username, password_hash, role, real_name, display_name, "
                "must_change_password, is_active) "
                "VALUES ('p173c', 'x', 'admin', 'P1-73C', 'P1-73C', 0, 1)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO supplier_requisition_orders "
                "(order_number, total_quantity, stock_deduction_qty, requisition_qty, status) "
                "VALUES ('P173-FACT', 10, 0, 10, 'voided')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO supplier_requisition_order_items "
                "(supplier_order_id, quantity, stock_deduction_qty, requisition_qty, "
                "status, version, voided_at, voided_by, void_idempotency_key, void_request_hash) "
                "VALUES (1, 10, 0, 10, 'voided', 2, CURRENT_TIMESTAMP, 1, "
                "'p173c-key', :request_hash)"
            ),
            {"request_hash": "a" * 64},
        )
    with pytest.raises(RuntimeError, match="cannot downgrade P1-73C"):
        command.downgrade(config, "tt28v8x9z17")
