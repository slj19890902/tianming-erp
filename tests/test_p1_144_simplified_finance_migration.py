from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "jk69v8x9z58"
TARGET_REVISION = "jl70v8x9z59"
NEW_TABLES = {
    "finance_recurring_rules",
    "finance_utility_readings",
    "finance_acceptance_notes",
}


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "p1-144-simplified-finance-migration-test")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return config


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _schema_signature(connection: sqlite3.Connection, table: str) -> tuple:
    columns = tuple(
        tuple(row[1:6]) for row in connection.execute(f"PRAGMA table_info({table})")
    )
    foreign_keys = tuple(
        sorted(tuple(row[2:]) for row in connection.execute(f"PRAGMA foreign_key_list({table})"))
    )
    indexes = []
    for row in connection.execute(f"PRAGMA index_list({table})"):
        name = str(row[1])
        index_columns = tuple(
            str(item[2]) for item in connection.execute(f'PRAGMA index_info("{name}")')
        )
        indexes.append((name, int(row[2]), str(row[3]), int(row[4]), index_columns))
    return columns, foreign_keys, tuple(sorted(indexes))


def _assert_health(connection: sqlite3.Connection, revision: str) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
        revision,
    )
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_p1_144_migration_upgrade_downgrade_upgrade_round_trip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p1-144-round-trip.sqlite3"
    config = _config(monkeypatch, database)
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == [TARGET_REVISION]
    assert script.get_revision(TARGET_REVISION).down_revision == PREVIOUS_REVISION

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        _assert_health(connection, TARGET_REVISION)
        assert NEW_TABLES <= _tables(connection)
        assert {"payment_method", "acceptance_note_id"} <= _columns(
            connection, "supplier_monthly_payments"
        )
        center = connection.execute(
            "SELECT id, code, name, center_type FROM finance_cost_centers "
            "WHERE code = 'PROD'"
        ).fetchone()
        assert center is not None
        cost_entry_id = connection.execute(
            """
            INSERT INTO finance_cost_pool_entries (
                cost_month, document_date, cost_center_id,
                cost_center_code_snapshot, cost_center_name_snapshot,
                cost_center_type_snapshot, cost_category, accounting_class,
                allocation_basis, description, amount, tax_amount,
                source_type, source_reference, source_fingerprint,
                status, version
            ) VALUES (
                '2026-10', '2026-10-31', ?, ?, ?, ?,
                'factory_utilities', 'manufacturing', 'unallocated',
                '小数水表迁移校验', 1.29, 0,
                'utility_reading', '水费表数', 'p1144-decimal-utility',
                'draft', 1
            ) RETURNING id
            """,
            center,
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO finance_utility_readings (
                cost_month, utility_type, cost_center_id,
                previous_reading, current_reading, usage_quantity,
                unit_price, calculated_amount, paid_amount,
                cost_pool_entry_id, version
            ) VALUES (
                '2026-10', 'water', ?,
                12345.678, 12346.001, 0.323,
                4.0000, 1.29, 0, ?, 1
            )
            """,
            (center[0], cost_entry_id),
        )
        assert connection.execute(
            "SELECT usage_quantity FROM finance_utility_readings"
        ).fetchone() == (0.323,)
        connection.execute("DELETE FROM finance_utility_readings")
        connection.execute(
            "DELETE FROM finance_cost_pool_entries WHERE id = ?", (cost_entry_id,)
        )
        connection.commit()

    command.downgrade(config, PREVIOUS_REVISION)
    with sqlite3.connect(database) as connection:
        _assert_health(connection, PREVIOUS_REVISION)
        assert NEW_TABLES.isdisjoint(_tables(connection))
        assert {"payment_method", "acceptance_note_id"}.isdisjoint(
            _columns(connection, "supplier_monthly_payments")
        )

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        _assert_health(connection, TARGET_REVISION)
        assert NEW_TABLES <= _tables(connection)


def test_p1_144_migration_blocks_downgrade_when_new_facts_exist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p1-144-downgrade-guard.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        center_id = connection.execute(
            "SELECT id FROM finance_cost_centers WHERE code = 'FINANCE'"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO finance_recurring_rules (
                rule_type, name, cost_center_id, cost_category,
                start_month, monthly_amount, due_day, is_active, version
            ) VALUES (
                'fixed_monthly', '车辆月供', ?, 'finance_expense',
                '2026-09', 8000, 20, 1, 1
            )
            """,
            (center_id,),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="facts would be lost"):
        command.downgrade(config, PREVIOUS_REVISION)

    with sqlite3.connect(database) as connection:
        _assert_health(connection, TARGET_REVISION)
        assert connection.execute(
            "SELECT name, monthly_amount FROM finance_recurring_rules"
        ).fetchone() == ("车辆月供", 8000)


def test_p1_144_migration_preserves_nonempty_parent_finance_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p1-144-nonempty-parent.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, PREVIOUS_REVISION)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        center = connection.execute(
            "SELECT id, code, name, center_type FROM finance_cost_centers "
            "WHERE code = 'FINANCE'"
        ).fetchone()
        assert center is not None
        connection.execute(
            """
            INSERT INTO finance_cost_pool_entries (
                cost_month, document_date, cost_center_id,
                cost_center_code_snapshot, cost_center_name_snapshot,
                cost_center_type_snapshot, cost_category, accounting_class,
                allocation_basis, description, amount, tax_amount,
                source_type, source_reference, source_fingerprint,
                status, version
            ) VALUES (
                '2026-08', '2026-08-31', ?, ?, ?, ?,
                'finance_expense', 'finance', 'unallocated',
                '历史车贷', 8000.25, 0,
                'manual', '旧财务事实', 'p1144-old-cost', 'draft', 3
            )
            """,
            center,
        )
        supplier_id = connection.execute(
            """
            INSERT INTO supplier_master_records (
                standard_name, normalized_name, display_name,
                sort_order, is_active, version
            ) VALUES ('旧纸板供应商', '旧纸板供应商', '旧供应商', 100, 1, 1)
            RETURNING id
            """
        ).fetchone()[0]
        statement_id = connection.execute(
            """
            INSERT INTO supplier_monthly_statements (
                statement_number, supplier_id, supplier_name_snapshot,
                settlement_month, period_start, period_end, currency, tax_basis,
                status, active_guard, erp_amount, adjustment_amount,
                adjusted_amount, supplier_statement_number,
                supplier_statement_date, supplier_statement_amount,
                confirmed_amount, invoice_allocated_amount, paid_amount, version
            ) VALUES (
                'SUP-OLD-202608', ?, '旧纸板供应商',
                '2026-08', '2026-07-21', '2026-08-20', 'CNY', 'tax_inclusive',
                'partial_payment', 1, 500, 0, 500, 'BILL-OLD',
                '2026-08-21', 500, 500, 500, 100, 4
            ) RETURNING id
            """,
            (supplier_id,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO supplier_monthly_payments (
                statement_id, payment_date, amount, reference
            ) VALUES (?, '2026-08-25', 100, '旧银行付款')
            """,
            (statement_id,),
        )
        connection.commit()
        cost_schema = _schema_signature(connection, "finance_cost_pool_entries")
        payment_schema = _schema_signature(connection, "supplier_monthly_payments")
        old_cost = connection.execute(
            "SELECT cost_month, document_date, cost_center_id, cost_category, "
            "accounting_class, description, amount, source_type, source_reference, "
            "source_fingerprint, status, version FROM finance_cost_pool_entries "
            "WHERE source_fingerprint = 'p1144-old-cost'"
        ).fetchone()
        old_payment = connection.execute(
            "SELECT statement_id, payment_date, amount, reference, created_by, created_at "
            "FROM supplier_monthly_payments WHERE statement_id = ?",
            (statement_id,),
        ).fetchone()

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        _assert_health(connection, TARGET_REVISION)
        assert connection.execute(
            "SELECT cost_month, document_date, cost_center_id, cost_category, "
            "accounting_class, description, amount, source_type, source_reference, "
            "source_fingerprint, status, version FROM finance_cost_pool_entries "
            "WHERE source_fingerprint = 'p1144-old-cost'"
        ).fetchone() == old_cost
        upgraded_payment = connection.execute(
            "SELECT statement_id, payment_date, amount, reference, created_by, created_at, "
            "payment_method, acceptance_note_id FROM supplier_monthly_payments "
            "WHERE statement_id = ?",
            (statement_id,),
        ).fetchone()
        assert upgraded_payment[:6] == old_payment
        assert upgraded_payment[6:] == ("bank", None)

    command.downgrade(config, PREVIOUS_REVISION)
    with sqlite3.connect(database) as connection:
        _assert_health(connection, PREVIOUS_REVISION)
        assert _schema_signature(connection, "finance_cost_pool_entries") == cost_schema
        assert _schema_signature(connection, "supplier_monthly_payments") == payment_schema
        assert connection.execute(
            "SELECT cost_month, document_date, cost_center_id, cost_category, "
            "accounting_class, description, amount, source_type, source_reference, "
            "source_fingerprint, status, version FROM finance_cost_pool_entries "
            "WHERE source_fingerprint = 'p1144-old-cost'"
        ).fetchone() == old_cost
        assert connection.execute(
            "SELECT statement_id, payment_date, amount, reference, created_by, created_at "
            "FROM supplier_monthly_payments WHERE statement_id = ?",
            (statement_id,),
        ).fetchone() == old_payment

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        _assert_health(connection, TARGET_REVISION)
        assert connection.execute(
            "SELECT payment_method, acceptance_note_id FROM supplier_monthly_payments "
            "WHERE statement_id = ?",
            (statement_id,),
        ).fetchone() == ("bank", None)
