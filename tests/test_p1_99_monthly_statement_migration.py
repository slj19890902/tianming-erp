from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT
    / "alembic"
    / "versions"
    / "p199v8x9z29_monthly_statement_summary.py"
)
OLD_HEAD = "de39v8x9z28"
NEW_HEAD = "p199v8x9z29"


def _config(monkeypatch: pytest.MonkeyPatch, database_path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option(
        "sqlalchemy.url",
        f"sqlite+pysqlite:///{database_path.as_posix()}",
    )
    return config


def _create_new_head_fixture(database_path: Path, *, new_fact: bool = False) -> None:
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.customer import Customer
    from app.models.finance import Statement
    from sqlalchemy.orm import sessionmaker

    engine = create_sqlite_engine(database_path)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        customer = Customer(
            customer_number=19901,
            customer_code="P199",
            name="P1-99迁移匿名客户",
            payment_term_days=30,
            credit_limit=Decimal("10000"),
        )
        session.add(customer)
        session.flush()
        session.add(
            Statement(
                statement_number=(
                    "ST-202608-P199-NEW" if new_fact else "ST-202608-P199-LEGACY"
                ),
                customer_id=customer.id,
                statement_month="2026-08",
                generation_mode=("monthly_summary" if new_fact else "legacy"),
                total_receivable=Decimal("280.80"),
                total_gross_profit=Decimal("70.20"),
                invoiced_amount=Decimal("0"),
                settled_amount=Decimal("0"),
                status="unsettled",
                confirmation_status="draft",
                version=1,
            )
        )
        session.commit()
    engine.dispose()


def test_p1_99_migration_is_linear_and_does_not_reclassify_history() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    compile(source, str(MIGRATION), "exec")
    assert 'revision = "p199v8x9z29"' in source
    assert 'down_revision = "de39v8x9z28"' in source
    assert "server_default=\"legacy\"" in source
    assert "monthly_summary" in source
    assert "separate" in source
    assert "UPDATE finance_statements" not in source


def test_p1_99_old_new_old_new_roundtrip_preserves_history_and_enforces_one_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "p1-99-roundtrip.sqlite3"
    _create_new_head_fixture(database_path)
    config = _config(monkeypatch, database_path)
    command.stamp(config, NEW_HEAD)
    command.downgrade(config, OLD_HEAD)

    with sqlite3.connect(database_path) as connection:
        before = connection.execute(
            "SELECT statement_number, total_receivable, confirmation_status, version "
            "FROM finance_statements"
        ).fetchone()
        assert "generation_mode" not in {
            row[1] for row in connection.execute("PRAGMA table_info(finance_statements)")
        }

    command.upgrade(config, NEW_HEAD)
    with sqlite3.connect(database_path) as connection:
        after_upgrade = connection.execute(
            "SELECT statement_number, total_receivable, confirmation_status, version, "
            "generation_mode FROM finance_statements"
        ).fetchone()
        assert after_upgrade[:4] == before
        assert after_upgrade[4] == "legacy"

    command.downgrade(config, OLD_HEAD)
    command.upgrade(config, NEW_HEAD)

    with sqlite3.connect(database_path) as connection:
        final = connection.execute(
            "SELECT statement_number, total_receivable, confirmation_status, version, "
            "generation_mode FROM finance_statements"
        ).fetchone()
        assert final[:4] == before
        assert final[4] == "legacy"
        connection.execute(
            "INSERT INTO finance_statements "
            "(statement_number, customer_id, statement_month, generation_mode, "
            "total_receivable, total_gross_profit, invoiced_amount, settled_amount, "
            "status, confirmation_status, version) "
            "VALUES ('ST-202608-P199-SUMMARY-1', 1, '2026-08', 'monthly_summary', "
            "10, 1, 0, 0, 'unsettled', 'draft', 1)"
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO finance_statements "
                "(statement_number, customer_id, statement_month, generation_mode, "
                "total_receivable, total_gross_profit, invoiced_amount, settled_amount, "
                "status, confirmation_status, version) "
                "VALUES ('ST-202608-P199-SUMMARY-2', 1, '2026-08', 'monthly_summary', "
                "20, 2, 0, 0, 'unsettled', 'draft', 1)"
            )
        connection.rollback()
        connection.execute(
            "INSERT INTO finance_statements "
            "(statement_number, customer_id, statement_month, generation_mode, "
            "total_receivable, total_gross_profit, invoiced_amount, settled_amount, "
            "status, confirmation_status, version) "
            "VALUES ('ST-202608-P199-SEPARATE', 1, '2026-08', 'separate', "
            "20, 2, 0, 0, 'unsettled', 'draft', 1)"
        )
        connection.commit()
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == NEW_HEAD


def test_p1_99_downgrade_fails_closed_after_new_generation_fact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "p1-99-fail-closed.sqlite3"
    _create_new_head_fixture(database_path, new_fact=True)
    config = _config(monkeypatch, database_path)
    command.stamp(config, NEW_HEAD)

    with pytest.raises(RuntimeError, match="monthly-summary"):
        command.downgrade(config, OLD_HEAD)
