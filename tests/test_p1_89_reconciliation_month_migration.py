from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker


OLD_HEAD = "de39v8x9z28"
NEW_HEAD = "df40v8x9z29"


def _config() -> Config:
    return Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))


def test_p1_89_old_new_old_new_round_trip_preserves_historical_facts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.customer import Customer
    from app.models.delivery import Delivery
    from app.models.finance import ReturnReceipt, Statement

    database_path = tmp_path / "p1-89-roundtrip.sqlite3"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    engine = create_sqlite_engine(database_path)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        customer = Customer(
            customer_number=8901,
            customer_code="P189",
            name="P1-89迁移隔离客户",
            payment_term_days=30,
            credit_limit=Decimal("10000"),
        )
        session.add(customer)
        session.flush()
        delivery = Delivery(
            delivery_number="P1-89-MIGRATION-DELIVERY",
            customer_id=customer.id,
            delivery_date=date(2026, 6, 13),
            status="dispatched",
            total_quantity=80,
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
                statement_number="ST-202606-P189",
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
    # Establish the old-head fixture, then exercise the required sequence.
    command.downgrade(config, OLD_HEAD)
    with engine.connect() as connection:
        before = connection.execute(
            text(
                "SELECT COUNT(*), MIN(actual_received_date) "
                "FROM finance_return_receipts"
            )
        ).one()
        amount_before = connection.execute(
            text("SELECT SUM(total_receivable) FROM finance_statements")
        ).scalar_one()
        assert "reconciliation_month" not in {
            column["name"]
            for column in inspect(connection).get_columns(
                "finance_return_receipts"
            )
        }

    command.upgrade(config, NEW_HEAD)
    with engine.connect() as connection:
        explicit_month = connection.execute(
            text(
                "SELECT reconciliation_month FROM finance_return_receipts"
            )
        ).scalar_one()
        assert explicit_month is None
    command.downgrade(config, OLD_HEAD)
    command.upgrade(config, NEW_HEAD)

    with engine.connect() as connection:
        after = connection.execute(
            text(
                "SELECT COUNT(*), MIN(actual_received_date) "
                "FROM finance_return_receipts"
            )
        ).one()
        amount_after = connection.execute(
            text("SELECT SUM(total_receivable) FROM finance_statements")
        ).scalar_one()
        assert before == after
        assert Decimal(str(amount_before)) == Decimal(str(amount_after))
        assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == NEW_HEAD


def test_p1_89_downgrade_refuses_explicit_month_facts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.core.database import create_sqlite_engine
    from app.models import Base

    database_path = tmp_path / "p1-89-downgrade-guard.sqlite3"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    engine = create_sqlite_engine(database_path)
    Base.metadata.create_all(engine)
    config = _config()
    command.stamp(config, NEW_HEAD)
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=OFF"))
        connection.execute(
            text(
                "INSERT INTO finance_return_receipts "
                "(delivery_id, actual_received_date, reconciliation_month, version, status) "
                "VALUES (999999, '2026-08-22', '2026-08', 1, 'confirmed')"
            )
        )

    try:
        command.downgrade(config, OLD_HEAD)
    except RuntimeError as error:
        assert "explicit reconciliation-month" in str(error)
    else:
        raise AssertionError("explicit reconciliation-month fact must block downgrade")
