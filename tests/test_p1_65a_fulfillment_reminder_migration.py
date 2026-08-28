from __future__ import annotations

from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect

from app.core.database import create_sqlite_engine
from app.models import Base


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "nn22v8x9z11"
TARGET_REVISION = "oo23v8x9z12"


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def _tables(path: Path) -> set[str]:
    engine = create_sqlite_engine(path)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _checks(path: Path) -> tuple[str, int]:
    with sqlite3.connect(path) as connection:
        return (
            connection.execute("PRAGMA integrity_check").fetchone()[0],
            len(connection.execute("PRAGMA foreign_key_check").fetchall()),
        )


def test_p1_65a_model_registers_internal_reminder_and_replay_tables() -> None:
    reminder = Base.metadata.tables["fulfillment_reminders"]
    mutation = Base.metadata.tables["fulfillment_reminder_mutations"]
    assert reminder.c.content.type.length is None
    assert reminder.c.version.server_default.arg == "1"
    assert reminder.c.source_valid.server_default is not None
    assert mutation.c.idempotency_key.type.length == 120
    assert mutation.c.response_json.type.length is None


def test_p1_65a_upgrade_downgrade_upgrade_round_trip_is_empty_and_clean(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "p1-65a-roundtrip.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, PARENT_REVISION)
    assert "fulfillment_reminders" not in _tables(path)

    command.upgrade(config, TARGET_REVISION)
    assert {
        "fulfillment_reminders",
        "fulfillment_reminder_mutations",
    } <= _tables(path)
    assert _checks(path) == ("ok", 0)

    command.downgrade(config, PARENT_REVISION)
    assert "fulfillment_reminders" not in _tables(path)
    assert "fulfillment_reminder_mutations" not in _tables(path)
    assert _checks(path) == ("ok", 0)

    command.upgrade(config, TARGET_REVISION)
    assert "fulfillment_reminders" in _tables(path)
    assert _checks(path) == ("ok", 0)


def test_p1_65a_downgrade_fails_closed_after_reminder_fact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "p1-65a-fail-closed.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET_REVISION)
    # This database intentionally stops at the historical oo23 schema.  Seed
    # the fact with revision-local SQL so future ORM columns do not invalidate
    # a migration downgrade contract that predates them.
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        user_id = connection.execute(
            """
            INSERT INTO users (
                username, password_hash, role, real_name, display_name,
                must_change_password
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "p165-migration",
                "test-only",
                "admin",
                "迁移测试",
                "迁移测试",
                0,
            ),
        ).lastrowid
        customer_id = connection.execute(
            "INSERT INTO customers (name) VALUES (?)",
            ("P1-65A 迁移测试客户",),
        ).lastrowid
        delivery_id = connection.execute(
            """
            INSERT INTO sales_deliveries (
                delivery_number, customer_id, delivery_date, status,
                total_quantity, created_by
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("P1-65A-MIGRATION", customer_id, "2026-08-16", "dispatched", 1, user_id),
        ).lastrowid
        receipt_id = connection.execute(
            """
            INSERT INTO finance_return_receipts (
                delivery_id, actual_received_date, status, created_by
            ) VALUES (?, ?, ?, ?)
            """,
            (delivery_id, "2026-08-16", "confirmed", user_id),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO fulfillment_reminders (
                source_return_receipt_id,
                source_return_receipt_id_snapshot,
                source_delivery_id_snapshot,
                source_delivery_number_snapshot,
                source_received_date_snapshot,
                source_valid,
                customer_id,
                customer_name_snapshot,
                scope_type,
                reminder_type,
                content,
                cadence,
                status,
                version,
                created_by,
                created_by_name_snapshot
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt_id,
                receipt_id,
                delivery_id,
                "P1-65A-MIGRATION",
                "2026-08-16",
                1,
                customer_id,
                "P1-65A 迁移测试客户",
                "customer",
                "delivery_attention",
                "迁移测试内部备忘",
                "continuous",
                "active",
                1,
                user_id,
                "迁移测试",
            ),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="拒绝破坏性降级"):
        command.downgrade(config, PARENT_REVISION)
    assert "fulfillment_reminders" in _tables(path)
    assert _checks(path) == ("ok", 0)
