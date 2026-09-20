from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


PARENT = "dt0920"
TARGET = "du0920"


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    return config


def test_order_import_source_migration_is_the_unique_linear_head() -> None:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == [TARGET]
    assert script.get_revision(TARGET).down_revision == PARENT


def test_order_import_source_migration_round_trip_and_loss_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "order-import-source.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, TARGET)
    with sqlite3.connect(database) as db:
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"order_import_sources", "order_import_source_lines"} <= tables
    command.downgrade(config, PARENT)
    command.upgrade(config, TARGET)

    with sqlite3.connect(database) as db:
        customer_id = db.execute("SELECT id FROM customers ORDER BY id LIMIT 1").fetchone()
        user_id = db.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        if customer_id is None:
            db.execute(
                "INSERT INTO customers (customer_number, customer_code, name, payment_term_days, credit_limit) "
                "VALUES (990001, 'T02-MIGRATION', 'T02迁移测试客户', 0, 0)"
            )
            customer_id = db.execute("SELECT last_insert_rowid()").fetchone()
        db.execute(
            "INSERT INTO order_import_sources "
            "(source_kind, source_key, source_hash, source_name_snapshot, customer_id, "
            "payload_hash, confirmation_summary_json, created_by) "
            "VALUES ('pdf_upload', ?, ?, 'fixture.pdf', ?, ?, '{}', ?)",
            (
                "a" * 64,
                "a" * 64,
                customer_id[0],
                "b" * 64,
                user_id[0] if user_id else None,
            ),
        )
        db.commit()
    with pytest.raises(Exception, match="禁止删除"):
        command.downgrade(config, PARENT)
