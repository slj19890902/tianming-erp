from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker


OLD_HEAD = "gn49v8x9z38"
MONTH_HEAD = "go50v8x9z39"
NEW_HEAD = "gp51v8x9z40"


def _config() -> Config:
    return Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))


def test_p0_31_old_new_old_new_preserves_historical_counts_and_amounts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.customer import Customer
    from app.models.delivery import Delivery
    from app.models.finance import ReturnReceipt, Statement

    database_path = tmp_path / "p0-31-roundtrip.sqlite3"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    engine = create_sqlite_engine(database_path)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        customer = Customer(
            customer_number=3101,
            customer_code="P031",
            name="P0-31迁移隔离客户",
            payment_term_days=30,
            credit_limit=Decimal("10000"),
        )
        session.add(customer)
        session.flush()
        delivery = Delivery(
            delivery_number="P0-31-MIGRATION-DELIVERY",
            customer_id=customer.id,
            delivery_date=date(2026, 6, 13),
            status="dispatched",
            total_quantity=80,
            is_historical_backfill=False,
            version=1,
        )
        session.add(delivery)
        session.flush()
        session.add(
            ReturnReceipt(
                delivery_id=delivery.id,
                actual_received_date=date(2026, 6, 14),
                reconciliation_month=None,
                version=1,
                status="confirmed",
            )
        )
        session.add(
            Statement(
                statement_number="ST-202606-P031",
                customer_id=customer.id,
                statement_month="2026-06",
                total_receivable=Decimal("280.80"),
                total_gross_profit=Decimal("70.20"),
                status="unsettled",
            )
        )
        session.commit()

    config = _config()
    command.stamp(config, NEW_HEAD)
    command.downgrade(config, OLD_HEAD)
    with engine.connect() as connection:
        receipt_before = connection.execute(
            text("SELECT COUNT(*), MIN(actual_received_date) FROM finance_return_receipts")
        ).one()
        delivery_before = connection.execute(
            text("SELECT COUNT(*), MIN(delivery_date) FROM sales_deliveries")
        ).one()
        amount_before = connection.execute(
            text("SELECT SUM(total_receivable) FROM finance_statements")
        ).scalar_one()
        assert "reconciliation_month" not in {
            column["name"]
            for column in inspect(connection).get_columns("finance_return_receipts")
        }
        assert "is_historical_backfill" not in {
            column["name"]
            for column in inspect(connection).get_columns("sales_deliveries")
        }

    command.upgrade(config, NEW_HEAD)
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT reconciliation_month FROM finance_return_receipts")
        ).scalar_one() is None
        assert connection.execute(
            text("SELECT is_historical_backfill FROM sales_deliveries")
        ).scalar_one() == 0
    command.downgrade(config, OLD_HEAD)
    command.upgrade(config, NEW_HEAD)

    with engine.connect() as connection:
        assert receipt_before == connection.execute(
            text("SELECT COUNT(*), MIN(actual_received_date) FROM finance_return_receipts")
        ).one()
        assert delivery_before == connection.execute(
            text("SELECT COUNT(*), MIN(delivery_date) FROM sales_deliveries")
        ).one()
        assert Decimal(str(amount_before)) == Decimal(
            str(
                connection.execute(
                    text("SELECT SUM(total_receivable) FROM finance_statements")
                ).scalar_one()
            )
        )
        assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == NEW_HEAD


def test_p0_31_downgrade_guards_preserve_explicit_month_and_backfill_facts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.core.database import create_sqlite_engine
    from app.models import Base

    month_path = tmp_path / "p0-31-month-guard.sqlite3"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(month_path))
    month_engine = create_sqlite_engine(month_path)
    Base.metadata.create_all(month_engine)
    config = _config()
    command.stamp(config, NEW_HEAD)
    with month_engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=OFF"))
        connection.execute(
            text(
                "INSERT INTO finance_return_receipts "
                "(delivery_id, actual_received_date, reconciliation_month, version, status) "
                "VALUES (999999, '2026-08-28', '2026-08', 1, 'confirmed')"
            )
        )
    command.downgrade(config, MONTH_HEAD)
    try:
        command.downgrade(config, OLD_HEAD)
    except RuntimeError as error:
        assert "explicit reconciliation month" in str(error)
    else:
        raise AssertionError("explicit reconciliation month must block downgrade")

    backfill_path = tmp_path / "p0-31-backfill-guard.sqlite3"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(backfill_path))
    backfill_engine = create_sqlite_engine(backfill_path)
    Base.metadata.create_all(backfill_engine)
    command.stamp(config, NEW_HEAD)
    with backfill_engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=OFF"))
        connection.execute(
            text(
                "INSERT INTO sales_deliveries "
                "(delivery_number, customer_id, delivery_date, source_mode, status, "
                "total_quantity, is_historical_backfill, backfilled_by, backfilled_at, version) "
                "VALUES ('P031-GUARD', 999999, '2026-08-01', 'order', 'pending', "
                "0, 1, 999999, CURRENT_TIMESTAMP, 1)"
            )
        )
    try:
        command.downgrade(config, MONTH_HEAD)
    except RuntimeError as error:
        assert "historical delivery" in str(error)
    else:
        raise AssertionError("historical delivery fact must block downgrade")
