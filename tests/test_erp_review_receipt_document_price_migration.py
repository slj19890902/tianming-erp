from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

from alembic import command
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from tests.test_erp_review_receipt_document_price import seed_document_case
from tests.test_p0_39_supplier_receipt_price_fact_migration import _config, _health


PARENT = "rp06v8x9z65"
TARGET = "rq07v8x9z66"
TABLE = "supplier_receipt_settlement_price_facts"


def _triggers(database):
    with sqlite3.connect(database) as connection:
        return dict(connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
            "AND sql LIKE '%supplier_receipt_settlement_price_facts%'"
        ).fetchall())


def test_document_price_migration_preserves_data_guards_and_rejects_lossy_downgrade(
    monkeypatch, tmp_path,
):
    from app.core.database import create_sqlite_engine
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.material import Material
    from app.models.supplier_settlement import SupplierMonthlyStatement, SupplierReceiptSettlementPriceFact
    from app.models.user import User
    from app.services.supplier_receipt_price_facts import (
        DOCUMENT_CONFIRMATION_ORIGIN, _document_price_plan, _fact_from_plan, _price_plan,
        confirm_receipt_document_price, receipt_document_price_context,
    )

    database = tmp_path / "document-price-migration.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, PARENT)
    engine = create_sqlite_engine(database)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    fixture = seed_document_case(factory)
    old_fixture = seed_document_case(factory, suffix="-old")
    with factory() as db:
        material = db.get(Material, old_fixture["material_id"])
        material.quote_price, material.price_unit = Decimal("2.80"), "per_square_meter"
        material.purchase_currency, material.purchase_tax_included, material.purchase_tax_rate = "CNY", True, Decimal("0.13")
        db.flush()
        old_plan = _price_plan(db, item=db.get(IncomingReceiptItem, old_fixture["receipt_item_id"]),
                               allow_supplier_code_fallback=True)
        old_fact = _fact_from_plan(old_plan, origin="historical_master_adoption",
                                  user=db.get(User, old_fixture["user_id"]),
                                  adoption_evidence_reference="isolated prior price-fact fixture")
        db.add(old_fact)
        db.commit()
        old_fact_id = old_fact.id
    with sqlite3.connect(database) as connection:
        old_snapshot = connection.execute(f"SELECT * FROM {TABLE} WHERE id=?", (old_fact_id,)).fetchone()
    before = _triggers(database)
    assert before
    command.upgrade(config, TARGET)
    assert _triggers(database) == before
    with sqlite3.connect(database) as connection:
        _health(connection, TARGET)
        assert connection.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone() == (1,)
        assert connection.execute(f"SELECT * FROM {TABLE} WHERE id=?", (old_fact_id,)).fetchone() == old_snapshot
        assert connection.execute("SELECT COUNT(*) FROM incoming_receipt_items").fetchone() == (2,)
    # The schema round trip is legal while there are no new-origin facts.
    command.downgrade(config, PARENT)
    assert _triggers(database) == before
    command.upgrade(config, TARGET)
    assert _triggers(database) == before

    with factory() as db:
        context = receipt_document_price_context(db, receipt_item_id=fixture["receipt_item_id"])
        values = {"receipt_item_id": fixture["receipt_item_id"], "evidence_reference": "原采购单PO-DOCUMENT-1第1行",
                  "unit_price": Decimal("2.80"), "price_unit": "per_square_meter",
                  "document_amount": Decimal("14.00"), "currency": "CNY",
                  "tax_included": True, "tax_rate": Decimal("0.13"),
                  "shipping_fee_mode": "included", "expected_source_hash": context["source_hash"]}
        plan, preview = _document_price_plan(db, **values)
        user = db.get(User, fixture["user_id"])
        db.add(_fact_from_plan(plan, origin=DOCUMENT_CONFIRMATION_ORIGIN, user=user))
        with pytest.raises(IntegrityError, match="adoption"):
            db.flush()
        db.rollback()
        result = confirm_receipt_document_price(
            db, user=user, expected_plan_hash=preview["plan_hash"], **values
        )
        db.commit()
        fact_id = result["fact_id"]
        assert db.get(SupplierReceiptSettlementPriceFact, fact_id).fact_origin == DOCUMENT_CONFIRMATION_ORIGIN
        from app.services.supplier_monthly_settlement import (
            confirm_statement, generate_or_refresh_settlements, review_statement,
        )
        generate_or_refresh_settlements(db, settlement_month="2026-08", user=user)
        statement = db.scalar(select(SupplierMonthlyStatement).where(
            SupplierMonthlyStatement.supplier_id == fixture["supplier_id"]
        ))
        statement = review_statement(db, statement_id=statement.id, expected_version=statement.version,
                    supplier_statement_number="DOCUMENT-STATEMENT", supplier_statement_date=date(2026, 8, 21),
                    supplier_statement_amount=Decimal("14.00"), user=user)
        confirm_statement(db, statement_id=statement.id, expected_version=statement.version, user=user)
        db.commit()

    with sqlite3.connect(database) as connection:
        _health(connection, TARGET)
        for sql in (
            f"UPDATE {TABLE} SET unit_price=99 WHERE id={fact_id}",
            f"DELETE FROM {TABLE} WHERE id={fact_id}",
            f"UPDATE supplier_monthly_statement_lines SET frozen_unit_price=99 WHERE supplier_receipt_price_fact_id={fact_id}",
            f"UPDATE incoming_receipt_items SET status='reversed' WHERE id={fixture['receipt_item_id']}",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql)
            connection.rollback()
    with pytest.raises(RuntimeError, match="禁止降级"):
        command.downgrade(config, PARENT)
    assert _triggers(database) == before
    with sqlite3.connect(database) as connection:
        _health(connection, TARGET)
        assert connection.execute(f"SELECT fact_origin FROM {TABLE} WHERE id=?", (fact_id,)).fetchone() == (
            "historical_document_confirmation",
        )
        assert connection.execute(f"SELECT * FROM {TABLE} WHERE id=?", (old_fact_id,)).fetchone() == old_snapshot
    engine.dispose()
