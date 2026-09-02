from __future__ import annotations

import importlib.util
import io
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory


ROOT = Path(__file__).resolve().parents[1]
PARENT = "jn72v8x9z61"
TARGET = "jo73v8x9z62"
NEW_TABLES = {
    "supplier_payment_batches",
    "supplier_credit_lots",
    "finance_utility_expenses",
    "finance_utility_month_modes",
}
P0_39_TRIGGERS = {
    "trg_supplier_receipt_price_facts_immutable_update",
    "trg_supplier_receipt_price_facts_immutable_delete",
    "trg_supplier_statement_receipt_reverse_guard",
    "trg_supplier_statement_price_fact_match_insert",
    "trg_supplier_statement_price_fact_match_update",
    "trg_stock_replenishment_route_validate_insert",
    "trg_stock_replenishment_route_immutable_update",
}
P1_149_TRIGGERS = {
    "trg_supplier_statement_revision_validate_insert",
    "trg_supplier_statement_revision_immutable_update",
    "trg_supplier_monthly_adjustment_validate_insert",
    "trg_supplier_monthly_adjustments_immutable_update",
    "trg_supplier_monthly_adjustments_immutable_delete",
    "trg_supplier_payment_batch_validate_insert",
    "trg_supplier_payment_batch_immutable_update",
    "trg_supplier_credit_lot_validate_insert",
    "trg_supplier_credit_lot_validate_update",
    "trg_supplier_monthly_payment_validate_insert",
    "trg_supplier_monthly_payment_validate_update",
    "trg_finance_acceptance_supplier_link_update",
    "trg_finance_utility_expense_month_guard",
    "trg_finance_utility_expense_month_guard_update",
    "trg_finance_utility_reading_month_guard",
    "trg_finance_utility_reading_month_guard_update",
    "trg_finance_utility_month_mode_update",
    "trg_finance_utility_month_mode_delete",
}


def _load_migration_module():
    path = (
        ROOT
        / "alembic"
        / "versions"
        / f"{TARGET}_p1_149_supplier_settlement_cycles.py"
    )
    spec = importlib.util.spec_from_file_location("p1_149_cycle_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "p1-149-supplier-cycle-migration-test")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _triggers(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )
    }


def _health(connection: sqlite3.Connection, revision: str) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    assert connection.execute(
        "SELECT version_num FROM alembic_version"
    ).fetchone() == (revision,)
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _insert_legacy_supplier_and_statement(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO supplier_master_records(
            id, standard_name, normalized_name, display_name,
            sort_order, is_active, version
        ) VALUES (90149, 'P1-149 迁移供应商', 'p1-149迁移供应商',
                  '迁移供应商', 100, 1, 1)
        """
    )
    connection.execute(
        """
        INSERT INTO supplier_monthly_statements(
            id, statement_number, supplier_id, supplier_name_snapshot,
            settlement_month, period_start, period_end,
            currency, tax_basis, status, active_guard,
            erp_amount, adjustment_amount, adjusted_amount,
            invoice_allocated_amount, paid_amount, version
        ) VALUES (
            90149, 'AP-P1149-LEGACY', 90149, '迁移供应商',
            '2026-08', '2026-07-21', '2026-08-20',
            'CNY', 'tax_inclusive', 'draft', 1,
            100, 0, 100, 0, 0, 1
        )
        """
    )
    connection.execute(
        """
        INSERT INTO supplier_monthly_statements(
            id, statement_number, supplier_id, supplier_name_snapshot,
            settlement_month, period_start, period_end,
            currency, tax_basis, status, active_guard,
            erp_amount, adjustment_amount, adjusted_amount,
            invoice_allocated_amount, paid_amount, version
        ) VALUES (
            90148, 'AP-P1149-LEGACY-VOIDED', 90149, '迁移供应商',
            '2026-08', '2026-07-21', '2026-08-20',
            'CNY', 'tax_inclusive', 'voided', NULL,
            80, 0, 80, 0, 0, 1
        )
        """
    )
    connection.commit()


def _insert_cost_entry(
    connection: sqlite3.Connection,
    *,
    cost_month: str,
    fingerprint: str,
) -> tuple[int, int]:
    center = connection.execute(
        "SELECT id, code, name, center_type FROM finance_cost_centers "
        "WHERE code='PROD'"
    ).fetchone()
    assert center is not None
    entry_id = connection.execute(
        """
        INSERT INTO finance_cost_pool_entries(
            cost_month, document_date, cost_center_id,
            cost_center_code_snapshot, cost_center_name_snapshot,
            cost_center_type_snapshot, cost_category, accounting_class,
            allocation_basis, description, amount, tax_amount,
            source_type, source_reference, source_fingerprint,
            status, version
        ) VALUES (
            ?, ?, ?, ?, ?, ?, 'factory_utilities', 'manufacturing',
            'unallocated', 'P1-149 水电迁移约束', 100, 0,
            'utility_reading', 'P1-149', ?, 'draft', 1
        ) RETURNING id
        """,
        (
            cost_month,
            f"{cost_month}-01",
            center[0],
            center[1],
            center[2],
            center[3],
            fingerprint,
        ),
    ).fetchone()[0]
    return int(center[0]), int(entry_id)


def test_p1_149_migration_is_linear_backfills_defaults_and_round_trips(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p1-149-round-trip.sqlite3"
    config = _config(monkeypatch, database)
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == [TARGET]
    assert script.get_revision(TARGET).down_revision == PARENT

    command.upgrade(config, PARENT)
    with sqlite3.connect(database) as connection:
        _health(connection, PARENT)
        _insert_legacy_supplier_and_statement(connection)
        parent_triggers = _triggers(connection)
        assert P0_39_TRIGGERS <= parent_triggers
        assert "settlement_day" not in _columns(
            connection, "supplier_master_records"
        )

    command.upgrade(config, TARGET)
    with sqlite3.connect(database) as connection:
        _health(connection, TARGET)
        assert NEW_TABLES <= _tables(connection)
        assert {"settlement_day"} <= _columns(
            connection, "supplier_master_records"
        )
        assert {
            "document_revision",
            "settlement_day_snapshot",
            "generation_origin",
            "source_hash",
            "supersedes_statement_id",
            "supersede_reason",
        } <= _columns(connection, "supplier_monthly_statements")
        assert {
            "payment_batch_id",
            "supplier_credit_id",
        } <= _columns(connection, "supplier_monthly_payments")
        assert {
            "statement_version_before",
            "amount_before",
            "amount_after",
            "is_post_confirmation",
        } <= _columns(connection, "supplier_monthly_adjustments")
        assert connection.execute(
            "SELECT settlement_day FROM supplier_master_records WHERE id=90149"
        ).fetchone() == (20,)
        assert connection.execute(
            "SELECT document_revision, settlement_day_snapshot, generation_origin "
            "FROM supplier_monthly_statements WHERE id=90149"
        ).fetchone() == (1, 20, "legacy")
        assert connection.execute(
            "SELECT id, document_revision, supersedes_statement_id, generation_origin "
            "FROM supplier_monthly_statements WHERE id IN (90148, 90149) ORDER BY id"
        ).fetchall() == [
            (90148, 1, None, "legacy"),
            (90149, 1, None, "legacy"),
        ]
        assert P0_39_TRIGGERS <= _triggers(connection)
        assert P1_149_TRIGGERS <= _triggers(connection)

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE supplier_master_records SET settlement_day=0 WHERE id=90149"
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE supplier_master_records SET settlement_day=32 WHERE id=90149"
            )
        connection.rollback()
        assert connection.execute(
            "SELECT settlement_day FROM supplier_master_records WHERE id=90149"
        ).fetchone() == (20,)

    command.downgrade(config, PARENT)
    with sqlite3.connect(database) as connection:
        _health(connection, PARENT)
        assert NEW_TABLES.isdisjoint(_tables(connection))
        assert "settlement_day" not in _columns(
            connection, "supplier_master_records"
        )
        assert "document_revision" not in _columns(
            connection, "supplier_monthly_statements"
        )
        assert "payment_batch_id" not in _columns(
            connection, "supplier_monthly_payments"
        )
        assert connection.execute(
            "SELECT statement_number, status FROM supplier_monthly_statements "
            "WHERE id=90149"
        ).fetchone() == ("AP-P1149-LEGACY", "draft")
        assert P0_39_TRIGGERS <= _triggers(connection)

    command.upgrade(config, TARGET)
    with sqlite3.connect(database) as connection:
        _health(connection, TARGET)
        assert NEW_TABLES <= _tables(connection)
        assert P0_39_TRIGGERS <= _triggers(connection)


@pytest.mark.parametrize("used_fact", ["custom_day", "supplier_credit"])
def test_p1_149_migration_fails_closed_after_new_cycle_or_credit_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    used_fact: str,
) -> None:
    database = tmp_path / f"p1-149-downgrade-{used_fact}.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, PARENT)
    with sqlite3.connect(database) as connection:
        _insert_legacy_supplier_and_statement(connection)
    command.upgrade(config, TARGET)

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        if used_fact == "custom_day":
            connection.execute(
                "UPDATE supplier_master_records SET settlement_day=25 WHERE id=90149"
            )
        else:
            connection.execute(
                """
                UPDATE supplier_monthly_statements
                SET status='paid', adjusted_amount=99, confirmed_amount=99,
                    invoice_allocated_amount=100, paid_amount=99, version=2
                WHERE id=90149
                """
            )
            connection.execute(
                """
                INSERT INTO supplier_monthly_adjustments(
                    statement_id, difference_type, amount,
                    statement_version_before, amount_before, amount_after,
                    is_post_confirmation
                ) VALUES (90149, 'other', -1, 1, 100, 99, 1)
                """
            )
            connection.execute(
                """
                INSERT INTO supplier_credit_lots(
                    supplier_id, supplier_name_snapshot, source_type,
                    source_statement_id, original_amount, available_amount,
                    status, version
                ) VALUES (
                    90149, '迁移供应商', 'statement_adjustment',
                    90149, 1, 1, 'available', 1
                )
                """
            )
        connection.commit()

    with pytest.raises(RuntimeError, match="P1-149 downgrade blocked"):
        command.downgrade(config, PARENT)

    with sqlite3.connect(database) as connection:
        _health(connection, TARGET)
        if used_fact == "custom_day":
            assert connection.execute(
                "SELECT settlement_day FROM supplier_master_records WHERE id=90149"
            ).fetchone() == (25,)
        else:
            assert connection.execute(
                "SELECT original_amount, available_amount FROM supplier_credit_lots"
            ).fetchone() == (1, 1)


def test_p1_149_sqlite_rejects_invalid_cross_table_finance_and_cycle_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p1-149-database-guards.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, PARENT)
    with sqlite3.connect(database) as connection:
        _insert_legacy_supplier_and_statement(connection)
    command.upgrade(config, TARGET)

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """
            UPDATE supplier_monthly_statements
            SET status='invoiced_pending_payment', confirmed_amount=100,
                adjusted_amount=100, invoice_allocated_amount=100,
                paid_amount=0, version=1
            WHERE id=90149
            """
        )
        connection.execute(
            """
            INSERT INTO supplier_master_records(
                id, standard_name, normalized_name, display_name,
                sort_order, is_active, version
            ) VALUES (
                90150, 'P1-149 第二供应商', 'p1-149第二供应商',
                '第二供应商', 101, 1, 1
            )
            """
        )
        connection.execute(
            """
            INSERT INTO supplier_monthly_statements(
                id, statement_number, supplier_id, supplier_name_snapshot,
                settlement_month, period_start, period_end,
                currency, tax_basis, status, active_guard,
                erp_amount, adjustment_amount, adjusted_amount,
                confirmed_amount, invoice_allocated_amount, paid_amount,
                version, generation_origin
            ) VALUES (
                90150, 'AP-P1149-SECOND', 90150, '第二供应商',
                '2026-09', '2026-08-21', '2026-09-20',
                'CNY', 'tax_inclusive', 'confirmed_pending_invoice', 1,
                100, -10, 90, 90, 90, 0, 2, 'manual'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO customers(
                id, name, payment_term_days, statement_cycle_start_day,
                credit_limit, delivery_method, default_tax_rate,
                status, is_active, version
            ) VALUES (
                90149, 'P1-149 承兑客户', 0, 20,
                0, '配送', 0.13, 'active', 1, 1
            )
            """
        )
        connection.execute(
            """
            INSERT INTO finance_acceptance_notes(
                id, bill_number, customer_id, customer_name_snapshot,
                amount, received_date, maturity_date, status, version
            ) VALUES
                (90149, 'AC-P1149-VALID', 90149, 'P1-149 承兑客户',
                 120, '2026-09-01', '2026-12-01', 'held', 1),
                (90150, 'AC-P1149-WRONG-FACE', 90149, 'P1-149 承兑客户',
                 120, '2026-09-01', '2026-12-01', 'held', 1)
            """
        )
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO supplier_payment_batches(
                    id, statement_id, supplier_id, payment_date,
                    acceptance_face_amount, acceptance_applied_amount,
                    credit_created_amount, settled_amount
                ) VALUES (91001, 90149, 90149, '2026-09-03', 100, 100, 0, 100)
                """
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO supplier_payment_batches(
                    id, statement_id, supplier_id, payment_date,
                    bank_amount, settled_amount
                ) VALUES (91002, 90149, 90150, '2026-09-03', 10, 10)
                """
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO supplier_payment_batches(
                    id, statement_id, supplier_id, payment_date,
                    bank_amount, settled_amount
                ) VALUES (91003, 90149, 90149, '2026-09-03', 101, 101)
                """
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO supplier_payment_batches(
                    id, statement_id, supplier_id, acceptance_note_id,
                    payment_date, acceptance_face_amount,
                    acceptance_applied_amount, credit_created_amount,
                    settled_amount
                ) VALUES (
                    91004, 90149, 90149, 90150,
                    '2026-09-03', 130, 100, 30, 100
                )
                """
            )
        connection.rollback()

        connection.execute(
            """
            INSERT INTO supplier_payment_batches(
                id, statement_id, supplier_id, acceptance_note_id,
                payment_date, acceptance_face_amount,
                acceptance_applied_amount, credit_created_amount,
                settled_amount
            ) VALUES (
                92000, 90149, 90149, 90149,
                '2026-09-03', 120, 100, 20, 100
            )
            """
        )
        connection.execute(
            """
            UPDATE finance_acceptance_notes
            SET status='endorsed', supplier_id=90149,
                supplier_name_snapshot='迁移供应商',
                supplier_statement_id=90149, endorsed_date='2026-09-03', version=2
            WHERE id=90149
            """
        )
        connection.execute(
            """
            INSERT INTO supplier_monthly_payments(
                id, statement_id, payment_date, amount, payment_method,
                acceptance_note_id, payment_batch_id
            ) VALUES (92000, 90149, '2026-09-03', 100, 'acceptance', 90149, 92000)
            """
        )
        connection.execute(
            "UPDATE finance_acceptance_notes SET supplier_payment_id=92000 WHERE id=90149"
        )
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE finance_acceptance_notes SET supplier_id=90150 WHERE id=90149"
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO supplier_credit_lots(
                    supplier_id, supplier_name_snapshot, source_type,
                    source_acceptance_note_id, source_statement_id,
                    source_payment_batch_id, original_amount,
                    available_amount, status, version
                ) VALUES (
                    90149, '迁移供应商', 'acceptance_overpayment',
                    90149, 90149, 92000, 19, 19, 'available', 1
                )
                """
            )
        connection.rollback()
        connection.execute(
            """
            INSERT INTO supplier_credit_lots(
                id, supplier_id, supplier_name_snapshot, source_type,
                source_acceptance_note_id, source_statement_id,
                source_payment_batch_id, original_amount,
                available_amount, status, version
            ) VALUES (
                92000, 90149, '迁移供应商', 'acceptance_overpayment',
                90149, 90149, 92000, 20, 20, 'available', 1
            )
            """
        )
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO supplier_monthly_adjustments(
                    statement_id, difference_type, amount, is_post_confirmation
                ) VALUES (90150, 'other', -10, 1)
                """
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO supplier_monthly_adjustments(
                    statement_id, difference_type, amount,
                    statement_version_before, amount_before, amount_after,
                    is_post_confirmation
                ) VALUES (90150, 'other', -10, 99, 100, 90, 1)
                """
            )
        connection.rollback()
        connection.execute(
            """
            INSERT INTO supplier_monthly_adjustments(
                id, statement_id, difference_type, amount,
                statement_version_before, amount_before, amount_after,
                is_post_confirmation
            ) VALUES (93000, 90150, 'other', -10, 1, 100, 90, 1)
            """
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE supplier_monthly_adjustments SET note='tampered' WHERE id=93000"
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM supplier_monthly_adjustments WHERE id=93000")
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO supplier_credit_lots(
                    supplier_id, supplier_name_snapshot, source_type,
                    source_statement_id, original_amount,
                    available_amount, status, version
                ) VALUES (
                    90149, '迁移供应商', 'statement_adjustment',
                    90150, 5, 5, 'available', 1
                )
                """
            )
        connection.rollback()

        connection.execute(
            """
            INSERT INTO supplier_payment_batches(
                id, statement_id, supplier_id, payment_date,
                credit_applied_amount, settled_amount
            ) VALUES (92001, 90150, 90150, '2026-09-03', 5, 5)
            """
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO supplier_monthly_payments(
                    statement_id, payment_date, amount, payment_method,
                    payment_batch_id, supplier_credit_id
                ) VALUES (90150, '2026-09-03', 5, 'credit', 92001, 92000)
                """
            )
        connection.rollback()

        center_id, legacy_entry = _insert_cost_entry(
            connection, cost_month="2026-09", fingerprint="p1149-utility-legacy"
        )
        _, combined_conflict_entry = _insert_cost_entry(
            connection, cost_month="2026-09", fingerprint="p1149-utility-combined-conflict"
        )
        _, combined_entry = _insert_cost_entry(
            connection, cost_month="2026-10", fingerprint="p1149-utility-combined"
        )
        _, legacy_conflict_entry = _insert_cost_entry(
            connection, cost_month="2026-10", fingerprint="p1149-utility-legacy-conflict"
        )
        connection.execute(
            """
            INSERT INTO finance_utility_readings(
                cost_month, utility_type, cost_center_id,
                previous_reading, current_reading, usage_quantity,
                unit_price, calculated_amount, paid_amount,
                cost_pool_entry_id, version
            ) VALUES ('2026-09', 'water', ?, 10, 20, 10, 2, 20, 0, ?, 1)
            """,
            (center_id, legacy_entry),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO finance_utility_expenses(
                    cost_month, cost_center_id, total_amount,
                    paid_amount, cost_pool_entry_id, version
                ) VALUES ('2026-09', ?, 100, 0, ?, 1)
                """,
                (center_id, combined_conflict_entry),
            )
        connection.rollback()
        connection.execute(
            """
            INSERT INTO finance_utility_expenses(
                cost_month, cost_center_id, total_amount,
                paid_amount, cost_pool_entry_id, version
            ) VALUES ('2026-10', ?, 100, 0, ?, 1)
            """,
            (center_id, combined_entry),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO finance_utility_readings(
                    cost_month, utility_type, cost_center_id,
                    previous_reading, current_reading, usage_quantity,
                    unit_price, calculated_amount, paid_amount,
                    cost_pool_entry_id, version
                ) VALUES ('2026-10', 'electricity', ?, 10, 20, 10, 2, 20, 0, ?, 1)
                """,
                (center_id, legacy_conflict_entry),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO finance_utility_expenses(
                    cost_month, cost_center_id, total_amount,
                    paid_amount, cost_pool_entry_id, version
                ) VALUES ('2026-99', ?, 100, 0, ?, 1)
                """,
                (center_id, combined_conflict_entry),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE finance_utility_readings SET cost_month='2026-10' "
                "WHERE cost_pool_entry_id=?",
                (legacy_entry,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE finance_utility_expenses SET cost_month='2026-09' "
                "WHERE cost_pool_entry_id=?",
                (combined_entry,),
            )
        connection.rollback()

        connection.execute(
            """
            INSERT INTO supplier_monthly_statements(
                id, statement_number, supplier_id, supplier_name_snapshot,
                settlement_month, period_start, period_end,
                currency, tax_basis, status, active_guard,
                erp_amount, adjustment_amount, adjusted_amount,
                invoice_allocated_amount, paid_amount, version,
                generation_origin, document_revision
            ) VALUES (
                94000, 'AP-P1149-REV-1', 90150, '第二供应商',
                '2026-11', '2026-10-21', '2026-11-20',
                'CNY', 'tax_inclusive', 'voided', NULL,
                50, 0, 50, 0, 0, 1, 'manual', 1
            )
            """
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO supplier_monthly_statements(
                    statement_number, supplier_id, supplier_name_snapshot,
                    settlement_month, period_start, period_end,
                    currency, tax_basis, status, active_guard,
                    erp_amount, adjustment_amount, adjusted_amount,
                    invoice_allocated_amount, paid_amount, version,
                    generation_origin, document_revision, supersedes_statement_id
                ) VALUES (
                    'AP-P1149-REV-BAD-N', 90150, '第二供应商',
                    '2026-11', '2026-10-21', '2026-11-20',
                    'CNY', 'tax_inclusive', 'draft', 1,
                    50, 0, 50, 0, 0, 1, 'regenerate', 3, 94000
                )
                """
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO supplier_monthly_statements(
                    statement_number, supplier_id, supplier_name_snapshot,
                    settlement_month, period_start, period_end,
                    currency, tax_basis, status, active_guard,
                    erp_amount, adjustment_amount, adjusted_amount,
                    invoice_allocated_amount, paid_amount, version,
                    generation_origin, document_revision, supersedes_statement_id
                ) VALUES (
                    'AP-P1149-REV-BAD-KEY', 90150, '第二供应商',
                    '2026-12', '2026-11-21', '2026-12-20',
                    'CNY', 'tax_inclusive', 'draft', 1,
                    50, 0, 50, 0, 0, 1, 'regenerate', 2, 94000
                )
                """
            )
        connection.rollback()
        connection.execute(
            """
            INSERT INTO supplier_monthly_statements(
                id, statement_number, supplier_id, supplier_name_snapshot,
                settlement_month, period_start, period_end,
                currency, tax_basis, status, active_guard,
                erp_amount, adjustment_amount, adjusted_amount,
                invoice_allocated_amount, paid_amount, version,
                generation_origin, document_revision, supersedes_statement_id
            ) VALUES (
                94002, 'AP-P1149-REV-2', 90150, '第二供应商',
                '2026-11', '2026-10-21', '2026-11-20',
                'CNY', 'tax_inclusive', 'draft', 1,
                50, 0, 50, 0, 0, 1, 'regenerate', 2, 94000
            )
            """
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE supplier_monthly_statements SET settlement_month='2026-12' "
                "WHERE id=94002"
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO supplier_monthly_statements(
                    statement_number, supplier_id, supplier_name_snapshot,
                    settlement_month, period_start, period_end,
                    currency, tax_basis, status, active_guard,
                    erp_amount, adjustment_amount, adjusted_amount,
                    invoice_allocated_amount, paid_amount, version,
                    generation_origin, document_revision, supersedes_statement_id
                ) VALUES (
                    'AP-P1149-REV-DUP', 90150, '第二供应商',
                    '2026-11', '2026-10-21', '2026-11-20',
                    'CNY', 'tax_inclusive', 'voided', NULL,
                    50, 0, 50, 0, 0, 1, 'regenerate', 2, 94000
                )
                """
            )
        connection.rollback()
        _health(connection, TARGET)


def test_p1_149_postgresql_ddl_contains_bidirectional_guards_and_safe_boolean() -> None:
    migration = _load_migration_module()
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    migration.op = Operations(context)
    migration.upgrade()
    upgrade_sql = output.getvalue()

    assert "fn_supplier_statement_revision_validate" in upgrade_sql
    assert "fn_supplier_monthly_adjustment_validate" in upgrade_sql
    assert "fn_supplier_payment_batch_validate" in upgrade_sql
    assert "fn_supplier_credit_lot_validate" in upgrade_sql
    assert "fn_supplier_monthly_payment_validate" in upgrade_sql
    assert "fn_finance_utility_month_guard" in upgrade_sql
    assert "ON CONFLICT (cost_month) DO UPDATE" in upgrade_sql
    assert "BEFORE INSERT OR UPDATE OF cost_month ON finance_utility_expenses" in upgrade_sql
    assert "BEFORE INSERT OR UPDATE OF cost_month ON finance_utility_readings" in upgrade_sql
    assert "DEFERRABLE INITIALLY DEFERRED" in upgrade_sql

    captured: list[str] = []

    class ZeroResult:
        def scalar_one(self) -> int:
            return 0

    class PostgresBind:
        dialect = SimpleNamespace(name="postgresql")

        def execute(self, statement):
            captured.append(str(statement))
            return ZeroResult()

    migration.op = SimpleNamespace(get_bind=lambda: PostgresBind())
    migration._assert_safe_downgrade()
    downgrade_guard_sql = "\n".join(captured)
    assert "is_post_confirmation IS TRUE" in downgrade_guard_sql
    assert "is_post_confirmation <> 0" not in downgrade_guard_sql
    assert "settlement_day_snapshot <> 20" in downgrade_guard_sql
    assert "source_hash IS NOT NULL" in downgrade_guard_sql
    assert "supersede_reason IS NOT NULL" in downgrade_guard_sql
    assert "payment_batch_id IS NOT NULL" in downgrade_guard_sql

    migration = _load_migration_module()
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    migration.op = Operations(context)
    migration._assert_safe_downgrade = lambda: None
    migration.downgrade()
    downgrade_sql = output.getvalue()
    assert "DROP FUNCTION IF EXISTS fn_finance_utility_month_guard" in downgrade_sql
    assert "DROP FUNCTION IF EXISTS fn_supplier_payment_batch_validate" in downgrade_sql
    assert "DROP TABLE finance_utility_month_modes" in downgrade_sql
