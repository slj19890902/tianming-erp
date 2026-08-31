from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


REVISION = "iv57v8x9z46"
DOWN_REVISION = "hu56v8x9z45"
TRIGGERS = {
    "trg_finance_receipt_resolution_action_insert",
    "trg_finance_receipt_resolution_action_update",
}


def _config() -> Config:
    return Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))


def _trigger_names(database_path: Path) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'trigger' "
                "AND tbl_name = 'finance_return_receipt_items'"
            )
        }


def test_resolution_action_guards_restore_after_ht55_batch_rebuild(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "finance-receipt-guards.sqlite3"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "finance-receipt-guard-migration-test-key")
    config = _config()

    command.upgrade(config, DOWN_REVISION)
    assert not (TRIGGERS & _trigger_names(database_path))

    command.upgrade(config, REVISION)
    assert TRIGGERS <= _trigger_names(database_path)
    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="invalid finance receipt resolution_action"):
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                "INSERT INTO finance_return_receipt_items "
                "(return_receipt_id, delivery_item_id, actual_received_quantity, resolution_action) "
                "VALUES (1, 1, 1, 'invalid_action')"
            )

    command.downgrade(config, DOWN_REVISION)
    assert not (TRIGGERS & _trigger_names(database_path))
    command.upgrade(config, REVISION)
    assert TRIGGERS <= _trigger_names(database_path)
