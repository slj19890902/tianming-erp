from __future__ import annotations

from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect

from app.core.database import create_sqlite_engine


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "dq99v8x9z88"
TARGET_REVISION = "dr00v8x9z89"


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _checks(path: Path) -> tuple[str, int]:
    with sqlite3.connect(path) as connection:
        return (
            connection.execute("PRAGMA integrity_check").fetchone()[0],
            len(connection.execute("PRAGMA foreign_key_check").fetchall()),
        )


def test_purchase_migration_is_linear_and_models_are_registered() -> None:
    source = (
        ROOT
        / "alembic/versions/dr00v8x9z89_external_packaging_purchase_confirmation.py"
    ).read_text(encoding="utf-8")
    assert 'revision = "dr00v8x9z89"' in source
    assert 'down_revision = "dq99v8x9z88"' in source
    assert "禁止破坏性降级" in source
    assert "immutable_{action.lower()}" in source

    from app.models import Base

    assert {
        "external_packaging_purchase_daily_sequences",
        "external_packaging_purchase_batches",
        "external_packaging_purchase_orders",
        "external_packaging_purchase_items",
    } <= set(Base.metadata.tables)


def test_purchase_migration_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "external-purchase-roundtrip.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET_REVISION)
    tables = set(inspect(create_sqlite_engine(path)).get_table_names())
    assert "external_packaging_purchase_items" in tables
    assert _checks(path) == ("ok", 0)
    command.downgrade(config, PARENT_REVISION)
    tables = set(inspect(create_sqlite_engine(path)).get_table_names())
    assert "external_packaging_purchase_items" not in tables
    assert _checks(path) == ("ok", 0)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET_REVISION
    assert _checks(path) == ("ok", 0)


def test_purchase_facts_are_immutable_and_downgrade_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "external-purchase-fail-closed.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        customer_id = connection.execute(
            "INSERT INTO customers(name) VALUES ('迁移匿名客户')"
        ).lastrowid
        order_id = connection.execute(
            """
            INSERT INTO sales_orders(order_number,customer_id,order_date,total_amount)
            VALUES ('TM20260809002',?,'2026-08-09',0)
            """,
            (customer_id,),
        ).lastrowid
        batch_id = connection.execute(
            """
            INSERT INTO external_packaging_purchase_batches(
                sales_order_id,idempotency_key,request_fingerprint
            ) VALUES (?,?,?)
            """,
            (order_id, "migration-key", "a" * 64),
        ).lastrowid
        connection.commit()
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute(
                "UPDATE external_packaging_purchase_batches "
                "SET request_fingerprint=? WHERE id=?",
                ("b" * 64, batch_id),
            )
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute(
                "DELETE FROM external_packaging_purchase_batches WHERE id=?",
                (batch_id,),
            )

    with pytest.raises(RuntimeError, match="禁止破坏性降级"):
        command.downgrade(config, PARENT_REVISION)
    assert _checks(path) == ("ok", 0)
