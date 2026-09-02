from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


ROOT = Path(__file__).resolve().parents[1]
PREVIOUS = "jh67v8x9z56"
TARGET = "ji68v8x9z57"
TABLES = (
    "supplier_monthly_payments",
    "supplier_monthly_invoices",
    "supplier_monthly_adjustments",
    "supplier_monthly_statement_lines",
    "supplier_monthly_statements",
)


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "p1-132-supplier-settlement-migration")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def _parent_schema(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    from app.core.database import create_sqlite_engine
    from app.models import Base

    engine = create_sqlite_engine(database)
    Base.metadata.create_all(engine)
    engine.dispose()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "DROP TRIGGER IF EXISTS trg_supplier_statement_receipt_reverse_guard"
        )
        for table in TABLES:
            connection.execute(f"DROP TABLE {table}")
        connection.commit()
    config = _config(monkeypatch, database)
    command.stamp(config, PREVIOUS)
    return config


def _health(connection: sqlite3.Connection, revision: str) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    assert connection.execute(
        "SELECT version_num FROM alembic_version"
    ).fetchone() == (revision,)
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_supplier_settlement_migration_is_linear_and_round_trips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = tmp_path / "p1-132-round-trip.sqlite3"
    config = _parent_schema(monkeypatch, database)
    script = ScriptDirectory.from_config(config)
    assert script.get_revision(TARGET).down_revision == PREVIOUS
    assert script.get_heads() == [TARGET]

    command.upgrade(config, TARGET)
    with sqlite3.connect(database) as connection:
        _health(connection, TARGET)
        actual = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert set(TABLES) <= actual
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='trigger' AND name="
            "'trg_supplier_statement_receipt_reverse_guard'"
        ).fetchone() == (1,)

    command.downgrade(config, PREVIOUS)
    with sqlite3.connect(database) as connection:
        _health(connection, PREVIOUS)
        actual = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert set(TABLES).isdisjoint(actual)

    command.upgrade(config, TARGET)
    with sqlite3.connect(database) as connection:
        _health(connection, TARGET)


def test_empty_database_full_chain_and_candidate_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = tmp_path / "p1-132-full-chain.sqlite3"
    config = _config(monkeypatch, database)

    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        _health(connection, TARGET)

    command.downgrade(config, PREVIOUS)
    with sqlite3.connect(database) as connection:
        _health(connection, PREVIOUS)

    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        _health(connection, TARGET)


def test_confirmed_statement_blocks_receipt_reversal_and_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = tmp_path / "p1-132-guards.sqlite3"
    config = _parent_schema(monkeypatch, database)
    command.upgrade(config, TARGET)

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO incoming_receipts(
                id, receipt_number, status, received_at, idempotency_key
            ) VALUES (1, 'IR-P132', 'posted', '2026-08-15 03:00:00', 'ir-p132')
            """
        )
        connection.execute(
            """
            INSERT INTO incoming_receipt_items(
                id, receipt_id, order_id, order_item_id,
                planned_quantity, received_quantity,
                cumulative_received_quantity, variance_quantity,
                variance_type, resolution_status, status
            ) VALUES (
                1, 1, 1, 1, 10, 10, 10, 0,
                'matched', 'not_required', 'posted'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO supplier_monthly_statements(
                id, statement_number, supplier_id, supplier_name_snapshot,
                settlement_month, period_start, period_end, currency,
                tax_basis, status, erp_amount, adjustment_amount,
                adjusted_amount, confirmed_amount
            ) VALUES (
                1, 'AP-202608-0001', 1, '匿名供应商', '2026-08',
                '2026-07-21', '2026-08-20', 'CNY', 'tax_inclusive',
                'confirmed_pending_invoice', 25, 0, 25, 25
            )
            """
        )
        connection.execute(
            """
            INSERT INTO supplier_monthly_statement_lines(
                statement_id, source_type, source_key,
                incoming_receipt_item_id, purchase_document_number,
                receipt_number, receipt_date, category_label,
                material_or_product_snapshot, received_quantity,
                quantity_unit, frozen_unit_price, price_unit, currency,
                tax_basis, tax_rate, erp_amount, tax_amount, source_link
            ) VALUES (
                1, 'paperboard', 'paperboard:1', 1, 'PO-P132', 'IR-P132',
                '2026-08-15', '瓦楞纸板', 'K=A', 10, '张', 2.5,
                'per_sheet', 'CNY', 'tax_inclusive', 0.13, 25, 2.88,
                '/incoming.html?receipt_item_id=1'
            )
            """
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="confirmed supplier"):
            connection.execute(
                "UPDATE incoming_receipt_items SET status='reversed' WHERE id=1"
            )

    with pytest.raises(RuntimeError, match="downgrade blocked"):
        command.downgrade(config, PREVIOUS)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (TARGET,)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute(
            "SELECT status FROM incoming_receipt_items WHERE id=1"
        ).fetchone() == ("posted",)
