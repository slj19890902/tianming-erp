from __future__ import annotations

from datetime import date
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.customer import Customer
from app.models.delivery import Delivery
from app.models.finance import ReturnReceipt
from app.models.fulfillment_reminder import FulfillmentReminder
from app.models.user import User


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
    engine = create_sqlite_engine(path)
    try:
        with Session(engine) as session:
            user = User(
                username="p165-migration",
                password_hash="test-only",
                role="admin",
                real_name="迁移测试",
                display_name="迁移测试",
                must_change_password=False,
            )
            customer = Customer(name="P1-65A 迁移测试客户")
            session.add_all([user, customer])
            session.flush()
            delivery = Delivery(
                delivery_number="P1-65A-MIGRATION",
                customer_id=customer.id,
                delivery_date=date(2026, 8, 16),
                status="dispatched",
                total_quantity=1,
            )
            session.add(delivery)
            session.flush()
            receipt = ReturnReceipt(
                delivery_id=delivery.id,
                actual_received_date=date(2026, 8, 16),
                status="confirmed",
                created_by=user.id,
            )
            session.add(receipt)
            session.flush()
            session.add(
                FulfillmentReminder(
                    source_return_receipt_id=receipt.id,
                    source_return_receipt_id_snapshot=receipt.id,
                    source_delivery_id_snapshot=delivery.id,
                    source_delivery_number_snapshot=delivery.delivery_number,
                    source_received_date_snapshot=receipt.actual_received_date,
                    source_valid=True,
                    customer_id=customer.id,
                    customer_name_snapshot=customer.name,
                    scope_type="customer",
                    reminder_type="delivery_attention",
                    content="迁移测试内部备忘",
                    cadence="continuous",
                    status="active",
                    version=1,
                    created_by=user.id,
                    created_by_name_snapshot="迁移测试",
                )
            )
            session.commit()
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="拒绝破坏性降级"):
        command.downgrade(config, PARENT_REVISION)
    assert "fulfillment_reminders" in _tables(path)
    assert _checks(path) == ("ok", 0)
