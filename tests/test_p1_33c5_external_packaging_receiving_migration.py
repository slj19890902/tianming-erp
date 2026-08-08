from __future__ import annotations

from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect

from app.core.database import create_sqlite_engine


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "ds01v8x9z90"
TARGET_REVISION = "dt02v8x9z91"


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


def test_receipt_migration_is_linear_and_models_are_registered() -> None:
    source = (
        ROOT / "alembic/versions/dt02v8x9z91_external_packaging_receipts.py"
    ).read_text(encoding="utf-8")
    assert 'revision = "dt02v8x9z91"' in source
    assert 'down_revision = "ds01v8x9z90"' in source
    assert "禁止破坏性降级" in source
    assert "immutable_{action.lower()}" in source

    from app.models import Base

    assert {
        "external_packaging_receipts",
        "external_packaging_receipt_items",
    } <= set(Base.metadata.tables)


def test_receipt_migration_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "external-receipt-roundtrip.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET_REVISION)
    tables = set(inspect(create_sqlite_engine(path)).get_table_names())
    assert "external_packaging_receipts" in tables
    assert "external_packaging_receipt_items" in tables
    assert _checks(path) == ("ok", 0)
    command.downgrade(config, PARENT_REVISION)
    tables = set(inspect(create_sqlite_engine(path)).get_table_names())
    assert "external_packaging_receipts" not in tables
    assert _checks(path) == ("ok", 0)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET_REVISION
    assert _checks(path) == ("ok", 0)


def test_receipt_facts_are_immutable_and_downgrade_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "external-receipt-fail-closed.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        receipt_id = connection.execute(
            """
            INSERT INTO external_packaging_receipts(
                purchase_order_id,receipt_number,idempotency_key,request_fingerprint
            ) VALUES (999999,'ER-MIGRATION-01','migration-receipt-key',?)
            """,
            ("a" * 64,),
        ).lastrowid
        connection.commit()
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute(
                "UPDATE external_packaging_receipts SET request_fingerprint=? WHERE id=?",
                ("b" * 64, receipt_id),
            )
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute(
                "DELETE FROM external_packaging_receipts WHERE id=?", (receipt_id,)
            )

    with pytest.raises(RuntimeError, match="禁止破坏性降级"):
        command.downgrade(config, PARENT_REVISION)
