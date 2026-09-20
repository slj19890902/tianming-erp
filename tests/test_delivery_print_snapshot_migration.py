from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


SNAPSHOT_COLUMNS = {
    "print_snapshot_version",
    "customer_name_snapshot",
    "customer_contact_snapshot",
    "customer_phone_snapshot",
    "customer_address_snapshot",
    "sender_company_name_snapshot",
    "sender_address_snapshot",
    "sender_phone_snapshot",
    "sender_fax_snapshot",
    "sender_tax_number_snapshot",
    "sender_bank_name_snapshot",
    "sender_bank_account_snapshot",
    "sender_contact_snapshot",
    "sender_contact_phone_snapshot",
}


def _config(
    monkeypatch: pytest.MonkeyPatch,
    database_path: Path,
) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database_path.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "delivery-print-snapshot-test")
    project_root = Path(__file__).resolve().parents[1]
    return Config(str(project_root / "alembic.ini"))


def _columns(database_path: Path) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        return {
            row[1]
            for row in connection.execute("PRAGMA table_info(sales_deliveries)")
        }


def test_delivery_print_snapshot_migration_round_trip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "delivery_print_snapshot.sqlite3"
    config = _config(monkeypatch, database_path)

    command.upgrade(config, "db0919")
    assert SNAPSHOT_COLUMNS.isdisjoint(_columns(database_path))

    command.upgrade(config, "dc0920")
    assert SNAPSHOT_COLUMNS <= _columns(database_path)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("dc0920",)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    command.downgrade(config, "db0919")
    assert SNAPSHOT_COLUMNS.isdisjoint(_columns(database_path))
    command.upgrade(config, "dc0920")
    assert SNAPSHOT_COLUMNS <= _columns(database_path)


def test_delivery_print_snapshot_migration_rejects_lossy_downgrade(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "delivery_print_snapshot_guard.sqlite3"
    config = _config(monkeypatch, database_path)
    command.upgrade(config, "dc0920")

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO customers (name) VALUES (?)",
            ("迁移保护客户",),
        )
        customer_id = connection.execute(
            "SELECT id FROM customers WHERE name = ?",
            ("迁移保护客户",),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO sales_deliveries "
            "(delivery_number, customer_id, delivery_date, status, "
            "total_quantity, print_snapshot_version, "
            "customer_name_snapshot) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "MIGRATION-GUARD-001",
                customer_id,
                "2026-09-20",
                "pending",
                0,
                1,
                "迁移保护客户",
            ),
        )
        connection.commit()

    before = hashlib.sha256(database_path.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="已有送货打印快照事实"):
        command.downgrade(config, "db0919")
    assert hashlib.sha256(database_path.read_bytes()).hexdigest() == before

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("dc0920",)
        assert connection.execute(
            "SELECT print_snapshot_version, customer_name_snapshot "
            "FROM sales_deliveries WHERE delivery_number = ?",
            ("MIGRATION-GUARD-001",),
        ).fetchone() == (1, "迁移保护客户")
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
