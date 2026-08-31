from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.orm import Session


ROOT = Path(__file__).resolve().parents[1]
REVISION = "hu56v8x9z45"
DOWN_REVISION = "ht55v8x9z44"
CONFIRMATION_TRIGGERS = {
    "trg_finance_statements_confirmation_insert",
    "trg_finance_statements_confirmation_update",
}
LEDGER_TRIGGERS = {
    "trg_finance_statements_ledger_version_insert",
    "trg_finance_statements_ledger_version_update",
}


def _config() -> Config:
    return Config(str(ROOT / "alembic.ini"))


def test_manual_finance_mutation_revision_is_the_only_linear_head() -> None:
    assert ScriptDirectory.from_config(_config()).get_heads() == [REVISION]


def test_manual_finance_mutation_migration_roundtrip_and_fact_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models.customer import Customer
    from app.models.finance import FinanceManualMutation, Statement
    from app.models.user import User

    database_path = tmp_path / "finance-manual-cas.sqlite3"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "finance-manual-cas-migration-test")
    config = _config()

    # Exercise this revision from its declared predecessor while constructing
    # only the predecessor objects that this migration reads or changes.
    engine = create_sqlite_engine(database_path)
    User.__table__.create(engine)
    Customer.__table__.create(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
                CREATE TABLE finance_statements (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    statement_number VARCHAR(40) NOT NULL,
                    customer_id INTEGER NOT NULL,
                    settlement_entity_id INTEGER,
                    settlement_name_snapshot VARCHAR(200),
                    settlement_customer_ids_snapshot_json TEXT,
                    statement_cycle_start_day_snapshot INTEGER,
                    statement_month VARCHAR(7) NOT NULL,
                total_receivable NUMERIC(14, 2) NOT NULL,
                total_gross_profit NUMERIC(14, 2) NOT NULL,
                invoiced_amount NUMERIC(14, 2) NOT NULL,
                settled_amount NUMERIC(14, 2) NOT NULL,
                status VARCHAR(20) NOT NULL,
                confirmation_status VARCHAR(20) DEFAULT 'draft' NOT NULL,
                version INTEGER DEFAULT 1 NOT NULL,
                confirmed_by INTEGER,
                confirmed_at DATETIME,
                created_by INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                CONSTRAINT ck_finance_statements_status
                    CHECK (status IN ('unsettled', 'settled')),
                CONSTRAINT ck_finance_statements_confirmation_status
                    CHECK (confirmation_status IN ('draft', 'confirmed', 'cancelled')),
                CONSTRAINT ck_finance_statements_version CHECK (version >= 1),
                CONSTRAINT uq_finance_statements_number UNIQUE (statement_number),
                FOREIGN KEY(customer_id) REFERENCES customers (id) ON DELETE RESTRICT,
                FOREIGN KEY(confirmed_by) REFERENCES users (id) ON DELETE SET NULL,
                FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE SET NULL
            )
            """
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_finance_statements_customer_month "
            "ON finance_statements (customer_id, statement_month)"
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER trg_finance_statements_confirmation_insert
            BEFORE INSERT ON finance_statements
            WHEN NEW.confirmation_status NOT IN ('draft','confirmed','cancelled')
              OR NEW.version < 1
              OR (NEW.confirmed_by IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.confirmed_by))
            BEGIN SELECT RAISE(ABORT, 'invalid statement confirmation'); END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER trg_finance_statements_confirmation_update
            BEFORE UPDATE OF confirmation_status, version ON finance_statements
            WHEN NEW.confirmation_status NOT IN ('draft','confirmed','cancelled')
              OR NEW.version < 1
              OR (NEW.confirmed_by IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.confirmed_by))
            BEGIN SELECT RAISE(ABORT, 'invalid statement confirmation'); END
            """
        )
    with Session(engine) as session:
        user = User(
            username="finance-migration-guard",
            password_hash=hash_password("RolePass123!"),
            role="finance",
            real_name="财务迁移验证",
            display_name="财务迁移验证",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=99001,
            customer_code="FIN-MIGRATION-GUARD",
            name="财务迁移保护测试客户",
            payment_term_days=30,
            credit_limit=Decimal("1000.00"),
        )
        session.add_all([user, customer])
        session.flush()
        session.execute(
            text(
                """
                INSERT INTO finance_statements (
                    statement_number, customer_id, statement_month,
                    total_receivable, total_gross_profit, invoiced_amount,
                    settled_amount, status, confirmation_status, version,
                    confirmed_by, created_by
                ) VALUES (
                    'ST-209901-MIGRATION-GUARD', :customer_id, '2099-01',
                    1.00, 0.00, 0.00, 0.00, 'unsettled', 'confirmed', 1,
                    :user_id, :user_id
                )
                """
            ),
            {"customer_id": customer.id, "user_id": user.id},
        )
        session.commit()
    engine.dispose()
    command.stamp(config, DOWN_REVISION)
    command.upgrade(config, REVISION)
    with sqlite3.connect(database_path) as connection:
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        statement_columns = {
            row[1]: row
            for row in connection.execute(
                "PRAGMA table_info('finance_statements')"
            ).fetchall()
        }
        legacy_ledger_version = connection.execute(
            "SELECT ledger_version FROM finance_statements "
            "WHERE statement_number = 'ST-209901-MIGRATION-GUARD'"
        ).fetchone()[0]
        triggers_after_upgrade = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='finance_statements'"
            )
        }
        indexes = connection.execute(
            "PRAGMA index_list('finance_manual_mutations')"
        ).fetchall()
        unique_index_columns = {
            tuple(
                item[2]
                for item in connection.execute(
                    f"PRAGMA index_info('{row[1]}')"
                ).fetchall()
            )
            for row in indexes
            if row[2]
        }
    assert "finance_manual_mutations" in table_names
    assert ("idempotency_key",) in unique_index_columns
    assert statement_columns["ledger_version"][3] == 1
    assert str(statement_columns["ledger_version"][4]).strip("'\"") == "1"
    assert legacy_ledger_version == 1
    assert CONFIRMATION_TRIGGERS | LEDGER_TRIGGERS <= triggers_after_upgrade
    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE finance_statements SET ledger_version = 0"
            )
        for key, mutation_type in (
            ("system:invoice-task-result:42", "register_invoice"),
            ("client-key-cannot-create-system-task", "register_invoice_task"),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO finance_manual_mutations (
                        idempotency_key, mutation_type, statement_id,
                        request_hash, actor_id, response_json
                    ) VALUES (?, ?, 1, ?, 1, '{}')
                    """,
                    (key, mutation_type, "A" * 64),
                )

    engine = create_sqlite_engine(database_path)
    with Session(engine) as session:
        user = session.query(User).one()
        statement = session.query(Statement).one()
        session.add(
            FinanceManualMutation(
                idempotency_key="migration-fact-guard-001",
                mutation_type="register_invoice",
                statement_id=statement.id,
                request_hash="A" * 64,
                actor_id=user.id,
                response_json="{}",
            )
        )
        session.commit()

    with pytest.raises(RuntimeError, match="cannot downgrade manual finance"):
        command.downgrade(config, DOWN_REVISION)

    with Session(engine) as session:
        session.query(FinanceManualMutation).delete()
        session.commit()

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE finance_statements SET ledger_version = 2"
        )
    with pytest.raises(RuntimeError, match="cannot downgrade statement ledger"):
        command.downgrade(config, DOWN_REVISION)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE finance_statements SET ledger_version = 1"
        )
    engine.dispose()

    command.downgrade(config, DOWN_REVISION)
    with sqlite3.connect(database_path) as connection:
        table_after_down = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='finance_manual_mutations'"
        ).fetchone()
        ledger_after_down = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info('finance_statements')"
            ).fetchall()
        }
        triggers_after_down = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='finance_statements'"
            )
        }
    assert table_after_down is None
    assert "ledger_version" not in ledger_after_down
    assert CONFIRMATION_TRIGGERS <= triggers_after_down
    assert LEDGER_TRIGGERS.isdisjoint(triggers_after_down)

    command.upgrade(config, REVISION)
    with sqlite3.connect(database_path) as connection:
        final_table = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='finance_manual_mutations'"
        ).fetchone()
        final_ledger_version = connection.execute(
            "SELECT ledger_version FROM finance_statements"
        ).fetchone()[0]
        final_triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='finance_statements'"
            )
        }
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        current_revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
    assert final_table == (1,)
    assert final_ledger_version == 1
    assert CONFIRMATION_TRIGGERS | LEDGER_TRIGGERS <= final_triggers
    assert integrity == "ok"
    assert foreign_key_errors == []
    assert current_revision == REVISION
