from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "cu77v8x9z66"
TARGET_REVISION = "ct76v8x9z65"
NEXT_REVISION = "cv78v8x9z67"


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "p0-replenishment-incoming-migration-test")
    return Config(str(ROOT / "alembic.ini"))


def _columns(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        str(row[1]): int(row[3])
        for row in connection.execute("PRAGMA table_info(incoming_receipt_items)")
    }


def _assert_delivery_void_columns(connection: sqlite3.Connection) -> None:
    delivery_columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(sales_deliveries)")
    }
    assert {
        "ever_dispatched_at",
        "voided_by",
        "voided_at",
    } <= delivery_columns


def _assert_database(database: Path, revision: str) -> None:
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == revision
        _assert_delivery_void_columns(connection)


def test_ct76_is_linear_parent_of_cv78(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(monkeypatch, tmp_path / "lineage.sqlite3")
    script = ScriptDirectory.from_config(config)
    assert script.get_revision(TARGET_REVISION).down_revision == PARENT_REVISION
    assert script.get_revision(NEXT_REVISION).down_revision == TARGET_REVISION


def test_ct76_round_trip_preserves_old_receipt_structure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "replenishment-incoming-roundtrip.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, PARENT_REVISION)
    _assert_database(database, PARENT_REVISION)

    with sqlite3.connect(database) as connection:
        old_indexes = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA index_list(incoming_receipt_items)"
            )
        }

    command.upgrade(config, TARGET_REVISION)
    _assert_database(database, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        columns = _columns(connection)
        assert columns["order_id"] == 0
        assert columns["order_item_id"] == 0
        assert columns["stock_replenishment_item_id"] == 0
        assert columns["received_inventory_lot_id"] == 0
        indexes = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA index_list(incoming_receipt_items)"
            )
        }
        assert old_indexes <= indexes
        assert {
            "ix_incoming_receipt_items_stock_replenishment_item",
            "ix_incoming_receipt_items_received_inventory_lot",
        } <= indexes

    command.downgrade(config, PARENT_REVISION)
    _assert_database(database, PARENT_REVISION)
    with sqlite3.connect(database) as connection:
        columns = _columns(connection)
        assert columns["order_id"] == 1
        assert columns["order_item_id"] == 1
        assert "stock_replenishment_item_id" not in columns
        assert "received_inventory_lot_id" not in columns

    command.upgrade(config, TARGET_REVISION)
    _assert_database(database, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        columns = _columns(connection)
        assert columns["order_id"] == 0
        assert columns["order_item_id"] == 0
        assert "stock_replenishment_item_id" in columns
        assert "received_inventory_lot_id" in columns


def test_ct76_downgrade_fails_closed_after_replenishment_receipt_fact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "replenishment-incoming-fail-closed.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """
            INSERT INTO incoming_receipt_items (
                receipt_id,
                order_id,
                order_item_id,
                stock_replenishment_item_id,
                planned_quantity,
                received_quantity,
                cumulative_received_quantity,
                variance_quantity,
                variance_type,
                resolution_status,
                status
            ) VALUES (
                1, NULL, NULL, 1, 10, 10, 10, 0,
                'matched', 'not_required', 'posted'
            )
            """
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="禁止破坏性降级"):
        command.downgrade(config, PARENT_REVISION)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET_REVISION
