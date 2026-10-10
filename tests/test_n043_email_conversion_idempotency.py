from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.api import email_intake
from app.api import orders
from app.api.orders import OrderCreate, OrderItemCreate, PdfImportConfirmation
from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.customer import Customer
from app.models.email_order_intake import (
    EmailOrderIntakeAttachment,
    EmailOrderIntakeDraft,
    EmailOrderIntakeMessage,
)
from app.models.order import Order
from app.models.product import Product
from app.models.user import User


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TARGET_REVISION = "cf62v8x9z51"


@pytest.fixture()
def conversion_db(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "conversion.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        customer = Customer(name="Email conversion customer", status="active", is_active=True)
        user = User(
            username="email-conversion-admin",
            password_hash="not-used-by-direct-route-test",
            role="admin",
            real_name="Email Conversion Admin",
            is_active=True,
            must_change_password=False,
        )
        db.add_all([customer, user])
        db.flush()
        product = Product(
            customer_id=customer.id,
            product_code="EMAIL-CONVERSION-001",
            customer_material_code="EMAIL-CONVERSION-001",
            product_name="Email conversion carton",
            box_category="normal",
            is_active=True,
        )
        db.add(product)
        db.flush()
        message = EmailOrderIntakeMessage(
            mailbox_key="orders",
            imap_uidvalidity="test",
            imap_uid="1",
            sender_email="buyer@example.com",
            normalized_sender_email="buyer@example.com",
            raw_message_sha256="a" * 64,
            mapped_customer_id=customer.id,
            status="review_ready",
        )
        db.add(message)
        db.flush()
        attachment = EmailOrderIntakeAttachment(
            message_id=message.id,
            part_index=1,
            filename="email-order.pdf",
            byte_size=1,
            file_sha256="b" * 64,
            file_type="pdf",
            status="parsed",
        )
        db.add(attachment)
        db.flush()
        draft = EmailOrderIntakeDraft(
            message_id=message.id,
            attachment_id=attachment.id,
            customer_id=customer.id,
            parser_type="pdf",
            status="review_ready",
            source_name="email-order.pdf",
            file_sha256="b" * 64,
        )
        db.add(draft)
        db.commit()
    yield factory, {"customer_id": customer.id, "user_id": user.id, "product_id": product.id, "draft_id": draft.id}
    engine.dispose()


def _payload(ids: dict[str, int], *, draft_id: int | None) -> OrderCreate:
    return OrderCreate(
        customer_id=ids["customer_id"],
        customer_po="EMAIL-IDEMPOTENCY-001",
        import_draft=True,
        email_intake_draft_id=draft_id,
        pdf_import_confirmation=PdfImportConfirmation(
            preview_safety_token="test-token", confirmed=True
        ),
        items=[
            OrderItemCreate(
                product_id=ids["product_id"],
                product_code="EMAIL-CONVERSION-001",
                product_name="Email conversion carton",
                quantity=2,
                unit_price=Decimal("3.50"),
            )
        ],
    )


def _patch_order_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        orders,
        "_validate_pdf_import_safety",
        lambda _payload, _user: (
            [],
            {
                "source_hash": "b" * 64,
                "source_name": "email-order.pdf",
                "email_intake_draft_id": _payload.email_intake_draft_id,
            },
        ),
    )
    monkeypatch.setattr(orders, "is_composite_product", lambda _product: False)
    monkeypatch.setattr(orders, "create_or_refresh_production_task", lambda *_args: None)
    monkeypatch.setattr(orders, "refresh_production_task", lambda *_args: None)
    monkeypatch.setattr(orders, "refresh_order_production_status", lambda *_args: None)
    monkeypatch.setattr(orders, "_apply_order_reservation_plans", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        orders,
        "_order_response",
        lambda order, *_args, **_kwargs: {"id": order.id, "items": [{}]},
    )


def test_email_draft_conversion_is_transactional_and_idempotent(
    conversion_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, ids = conversion_db
    _patch_order_side_effects(monkeypatch)
    with factory() as db:
        user = db.get(User, ids["user_id"])
        assert user is not None
        result = orders.create_order(_payload(ids, draft_id=ids["draft_id"]), db, user)
        order = db.get(Order, result["id"])
        draft = db.get(EmailOrderIntakeDraft, ids["draft_id"])
        assert order is not None
        assert draft is not None
        assert order.email_intake_draft_id == draft.id
        assert draft.status == "converted"
        assert draft.converted_order_id == order.id

        with pytest.raises(HTTPException) as repeated:
            orders.create_order(_payload(ids, draft_id=draft.id), db, user)
        assert repeated.value.status_code == 409
        assert repeated.value.detail["code"] == "EMAIL_INTAKE_DRAFT_CONVERTED"
        with pytest.raises(HTTPException) as reopened:
            email_intake.open_email_pdf_preview(draft.id, None, db, user)
        assert reopened.value.status_code == 409
        assert reopened.value.detail["code"] == "EMAIL_INTAKE_DRAFT_CONVERTED"
        assert db.scalars(select(Order).where(Order.email_intake_draft_id == draft.id)).all() == [order]

        duplicate = Order(
            order_number="EMAIL-IDEMPOTENCY-DUPLICATE",
            customer_id=ids["customer_id"],
            order_date=order.order_date,
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("0"),
            email_intake_draft_id=draft.id,
        )
        db.add(duplicate)
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()


def test_email_conversion_rejects_stale_pdf_provenance_before_order_write(
    conversion_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, ids = conversion_db
    _patch_order_side_effects(monkeypatch)
    monkeypatch.setattr(
        orders,
        "_validate_pdf_import_safety",
        lambda _payload, _user: (
            [],
            {"source_hash": "c" * 64, "source_name": "email-order.pdf"},
        ),
    )
    with factory() as db:
        user = db.get(User, ids["user_id"])
        assert user is not None
        with pytest.raises(HTTPException) as rejected:
            orders.create_order(_payload(ids, draft_id=ids["draft_id"]), db, user)
        assert rejected.value.status_code == 409
        assert rejected.value.detail["code"] == "EMAIL_INTAKE_DRAFT_SOURCE_STALE"
        assert db.scalars(select(Order)).all() == []


def test_email_conversion_token_cannot_be_reused_for_another_draft(
    conversion_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, ids = conversion_db
    _patch_order_side_effects(monkeypatch)
    monkeypatch.setattr(
        orders,
        "_validate_pdf_import_safety",
        lambda _payload, _user: (
            [],
            {
                "source_hash": "b" * 64,
                "source_name": "email-order.pdf",
                "email_intake_draft_id": ids["draft_id"] + 1,
            },
        ),
    )
    with factory() as db:
        user = db.get(User, ids["user_id"])
        assert user is not None
        with pytest.raises(HTTPException) as rejected:
            orders.create_order(_payload(ids, draft_id=ids["draft_id"]), db, user)
        assert rejected.value.status_code == 409
        assert rejected.value.detail["code"] == "EMAIL_INTAKE_DRAFT_TOKEN_STALE"
        assert db.scalars(select(Order)).all() == []


def test_email_conversion_rejects_draft_that_is_not_ready(
    conversion_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, ids = conversion_db
    _patch_order_side_effects(monkeypatch)
    with factory() as db:
        draft = db.get(EmailOrderIntakeDraft, ids["draft_id"])
        user = db.get(User, ids["user_id"])
        assert draft is not None and user is not None
        draft.status = "archived"
        db.commit()
        with pytest.raises(HTTPException) as rejected:
            orders.create_order(_payload(ids, draft_id=draft.id), db, user)
        assert rejected.value.status_code == 409
        assert rejected.value.detail["code"] == "EMAIL_INTAKE_DRAFT_NOT_READY"
        assert db.scalars(select(Order)).all() == []


def test_n043_migration_creates_unique_email_order_link(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    database = tmp_path / "n043-link.sqlite3"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "n043-link-migration-test-secret")
    monkeypatch.setenv("ERP_ENVIRONMENT", "test")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))

    command.upgrade(config, TARGET_REVISION)

    with sqlite3.connect(database) as connection:
        sales_order_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(sales_orders)")
        }
        assert "email_intake_draft_id" in sales_order_columns
        indexes = connection.execute("PRAGMA index_list(sales_orders)").fetchall()
        assert any(
            row[1] == "uq_sales_orders_email_intake_draft_id" and row[2] == 1
            for row in indexes
        )
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(sales_orders)"
        ).fetchall()
        assert any(
            row[2] == "email_order_intake_drafts"
            and row[3] == "email_intake_draft_id"
            and row[4] == "id"
            for row in foreign_keys
        )
        draft_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'email_order_intake_drafts'"
        ).fetchone()[0]
        assert "converted_order_id" in draft_sql
        assert "'converted'" in draft_sql
