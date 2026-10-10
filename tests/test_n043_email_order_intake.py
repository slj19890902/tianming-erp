from __future__ import annotations

import imaplib
import json
import os
import time
import zipfile
from dataclasses import replace
from datetime import datetime
from email.message import EmailMessage
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from collections.abc import Generator
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import get_db
from app.api import email_intake as email_api
from app.core import config as config_module
from app.core.config import Settings
from app.core.database import create_sqlite_engine
from app.core.security import hash_password
from app.main import create_app
from app.models import Base
from app.models.access_control import UserCustomerScope
from app.models.customer import Customer
from app.models.email_order_intake import (
    EmailOrderIntakeAttachment,
    EmailOrderIntakeDraft,
    EmailOrderIntakeMessage,
    EmailOrderIntakePollState,
    EmailOrderSenderMapping,
)
from app.models.order import Order
from app.models.user import User
from app.services import email_order_intake as service


def _settings(tmp_path: Path, **changes) -> Settings:
    base = Settings(
        database_path=tmp_path / "test.sqlite3",
        backup_dir=tmp_path / "backups",
        allowed_origins=("http://testserver",),
        allowed_origin_regex=None,
        trusted_hosts=(),
        trusted_proxy_ips=(),
        secret_key="n043-test-secret-that-is-long-enough",
        bind_host="127.0.0.1",
        port=18080,
        workers=1,
        health_url="http://127.0.0.1:18080/api/health",
        browser_url="http://127.0.0.1:18080/",
        environment="test",
        email_intake_storage_dir=tmp_path / "attachments",
    )
    return replace(base, **changes)


def _email_bytes(sender: str, attachments: list[tuple[str, bytes]]) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "orders@example.com"
    message["Subject"] = "Purchase order"
    message["Message-ID"] = f"<{sender}-{len(attachments)}@example.com>"
    message.set_content("Please see attached order.")
    for filename, content in attachments:
        message.add_attachment(
            content,
            maintype="application",
            subtype="octet-stream",
            filename=filename,
        )
    return message.as_bytes()


def _valid_xlsx_bytes() -> bytes:
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as workbook:
        workbook.writestr("[Content_Types].xml", "<Types />")
        workbook.writestr("xl/workbook.xml", "<workbook />")
    return stream.getvalue()


def _xlsx_with_entries(entries: dict[str, bytes]) -> bytes:
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as workbook:
        workbook.writestr("[Content_Types].xml", "<Types />")
        workbook.writestr("xl/workbook.xml", "<workbook />")
        for name, content in entries.items():
            workbook.writestr(name, content)
    return stream.getvalue()



def _valid_pdf_bytes() -> bytes:
    stream = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(stream)
    return stream.getvalue()


VALID_PDF = _valid_pdf_bytes()
VALID_XLS = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1minimal-xls"


@pytest.fixture()
def database(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "n043.sqlite3")
    Base.metadata.create_all(engine)
    SessionMaker = sessionmaker(bind=engine, expire_on_commit=False)
    with SessionMaker() as session:
        customer = Customer(name="客户甲", status="active", is_active=True)
        other = Customer(name="客户乙", status="active", is_active=True)
        inactive = Customer(name="停用客户", status="inactive", is_active=False)
        session.add_all([customer, other, inactive])
        session.flush()
        users = [
            User(username="n043-admin", password_hash=hash_password("TestOnly123!"), role="admin", real_name="管理员", is_active=True, must_change_password=False),
            User(username="n043-sales", password_hash=hash_password("TestOnly123!"), role="sales", real_name="销售", is_active=True, must_change_password=False, customer_access_mode="selected"),
            User(username="n043-workshop", password_hash=hash_password("TestOnly123!"), role="workshop", real_name="车间", is_active=True, must_change_password=False),
        ]
        session.add_all(users)
        session.flush()
        session.add(UserCustomerScope(user_id=users[1].id, customer_id=customer.id, assigned_by=users[0].id))
        session.commit()
        ids = {"customer": customer.id, "other": other.id, "inactive": inactive.id}
    yield SessionMaker, ids, tmp_path
    engine.dispose()


def _fake_pdf_pipeline(customer_id: int):
    return SimpleNamespace(
        draft={
            "source_name": "order.pdf",
            "customer_route": {"status": "locked", "template_customer_id": customer_id},
            "customer_name": "客户甲",
            "parse_status": "recognized",
            "recognition_status": "recognized",
            "items": [{"product_code": "P001", "matched_product_id": 1}],
        },
        parse_method="text",
    )


def _fake_match(_db: Session, draft: dict, customer_id: int | None = None) -> dict:
    result = dict(draft)
    result["matched_customer_id"] = customer_id
    result["customer_match_status"] = "matched" if customer_id else "unmatched"
    result["recognition_status"] = "recognized" if customer_id else "needs_confirmation"
    result.setdefault("items", [{"matched_product_id": 1}])
    return result


@pytest.mark.parametrize(
    ("file_type", "content", "expected"),
    [
        ("pdf", VALID_PDF, True),
        ("pdf", b"not-a-pdf", False),
        ("xls", VALID_XLS, True),
        ("xls", b"PK\x03\x04not-an-xls", False),
        ("xlsx", _valid_xlsx_bytes(), True),
        ("xlsx", b"PK\x03\x04not-a-valid-zip", False),
        ("xlsx", VALID_XLS, False),
    ],
)
def test_attachment_content_signature_validation(
    file_type: str, content: bytes, expected: bool
) -> None:
    assert service._attachment_content_is_valid(file_type, content) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Orders <PO@Example.com>", "po@example.com"),
        ("missing-domain@example", ""),
        ("two@@example.com", ""),
        ("plain text", ""),
    ],
)
def test_sender_email_normalization_requires_complete_address(raw, expected) -> None:
    assert service.normalize_sender_email(raw) == expected


def test_email_resource_limit_defaults_and_environment_ranges(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ERP_ENVIRONMENT", "test")
    monkeypatch.setenv("ERP_SECRET_KEY", "n043-test-secret-that-is-long-enough")
    monkeypatch.setenv("ERP_DATABASE_PATH", str(tmp_path / "config.sqlite3"))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("ERP_ALLOWED_ORIGINS", "http://127.0.0.1:8000")
    monkeypatch.setenv("ERP_WORKERS", "1")
    monkeypatch.setenv("ERP_EMAIL_INTAKE_ENABLED", "false")
    for name in (
        "ERP_EMAIL_INTAKE_MAILBOX_KEY",
        "ERP_EMAIL_IMAP_TIMEOUT_SECONDS",
        "ERP_EMAIL_INTAKE_POLL_TIMEOUT_SECONDS",
        "ERP_EMAIL_INTAKE_MAX_RAW_MESSAGE_BYTES",
        "ERP_EMAIL_INTAKE_MAX_ATTACHMENTS",
        "ERP_EMAIL_INTAKE_MAX_TOTAL_ATTACHMENT_BYTES",
        "ERP_EMAIL_INTAKE_XLSX_MAX_ZIP_MEMBERS",
        "ERP_EMAIL_INTAKE_XLSX_MAX_UNCOMPRESSED_BYTES",
        "ERP_EMAIL_INTAKE_XLSX_MAX_COMPRESSION_RATIO",
        "ERP_EMAIL_INTAKE_MAX_PDF_PAGES",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = config_module.load_settings()
    assert settings.email_intake_max_raw_message_bytes == 50 * 1024 * 1024
    assert settings.email_imap_timeout_seconds == 30
    assert settings.email_intake_poll_timeout_seconds == 300
    assert settings.email_intake_max_attachments == 20
    assert settings.email_intake_max_total_attachment_bytes == 40 * 1024 * 1024
    assert settings.email_intake_xlsx_max_zip_members == 2_000
    assert settings.email_intake_xlsx_max_uncompressed_bytes == 100 * 1024 * 1024
    assert settings.email_intake_xlsx_max_compression_ratio == 100
    assert settings.email_intake_max_pdf_pages == 100

    monkeypatch.setenv("ERP_EMAIL_INTAKE_MAX_RAW_MESSAGE_BYTES", "1048575")
    with pytest.raises(ValueError, match="ERP_EMAIL_INTAKE_MAX_RAW_MESSAGE_BYTES"):
        config_module.load_settings()


def test_default_mailbox_key_is_stable_non_sensitive_and_mailbox_specific(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ERP_ENVIRONMENT", "test")
    monkeypatch.setenv("ERP_SECRET_KEY", "n043-test-secret-that-is-long-enough")
    monkeypatch.setenv("ERP_DATABASE_PATH", str(tmp_path / "mailbox-key.sqlite3"))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("ERP_ALLOWED_ORIGINS", "http://127.0.0.1:8000")
    monkeypatch.setenv("ERP_WORKERS", "1")
    monkeypatch.setenv("ERP_EMAIL_INTAKE_ENABLED", "false")
    monkeypatch.setenv("ERP_EMAIL_IMAP_HOST", "Imap.Secret-Example.com")
    monkeypatch.setenv("ERP_EMAIL_IMAP_USERNAME", "Orders@Secret-Example.com")
    monkeypatch.setenv("ERP_EMAIL_IMAP_FOLDER", "INBOX")
    monkeypatch.delenv("ERP_EMAIL_INTAKE_MAILBOX_KEY", raising=False)

    first = config_module.load_settings().email_intake_mailbox_key
    again = config_module.load_settings().email_intake_mailbox_key
    assert first == again
    assert first.startswith("imap-")
    assert "secret-example" not in first
    assert "orders" not in first

    monkeypatch.setenv("ERP_EMAIL_IMAP_FOLDER", "Orders")
    second_folder = config_module.load_settings().email_intake_mailbox_key
    assert second_folder != first

    monkeypatch.setenv("ERP_EMAIL_INTAKE_MAILBOX_KEY", "explicit-mailbox")
    assert config_module.load_settings().email_intake_mailbox_key == "explicit-mailbox"


def test_xlsx_member_limit_is_checked_before_zipfile_builds_a_member_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _xlsx_with_entries({"xl/extra.xml": b"extra"})
    settings = _settings(tmp_path, email_intake_xlsx_max_zip_members=2)

    def fail_if_zipfile_is_opened(*_args, **_kwargs):
        raise AssertionError("member limit must be rejected before ZipFile opens")

    monkeypatch.setattr(service.zipfile, "ZipFile", fail_if_zipfile_is_opened)
    assert service._xlsx_resource_error(raw, settings) == "XLSX ZIP 成员数量超过安全限制"


def test_xlsx_validation_does_not_call_namelist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _valid_xlsx_bytes()

    def forbidden_namelist(_self):
        raise AssertionError("full namelist must not be materialized")

    monkeypatch.setattr(zipfile.ZipFile, "namelist", forbidden_namelist)
    assert service._attachment_content_is_valid("xlsx", raw) is True
    assert service._xlsx_resource_error(raw, _settings(tmp_path)) is None


@pytest.mark.parametrize(
    ("settings_changes", "content", "expected_error"),
    [
        (
            {"email_intake_xlsx_max_zip_members": 2},
            _xlsx_with_entries({"xl/extra.xml": b"extra"}),
            "ZIP 成员数量",
        ),
        (
            {"email_intake_xlsx_max_uncompressed_bytes": 16},
            _xlsx_with_entries({"xl/sharedStrings.xml": b"x" * 100}),
            "解压后累计大小",
        ),
        (
            {"email_intake_xlsx_max_compression_ratio": 2},
            _xlsx_with_entries({"xl/sharedStrings.xml": b"A" * 10_000}),
            "压缩比",
        ),
    ],
)
def test_xlsx_zip_resource_limits_create_failed_draft(
    database, settings_changes: dict, content: bytes, expected_error: str
) -> None:
    SessionMaker, _ids, tmp_path = database
    with SessionMaker() as db:
        message, created = service.ingest_raw_message(
            db,
            mailbox_key="sales",
            uidvalidity="zip-limits",
            uid=expected_error,
            raw_message=_email_bytes("buyer@example.com", [("order.xlsx", content)]),
            settings=_settings(tmp_path, **settings_changes),
        )
        assert created is True
        assert message.attachments[0].status == "failed"
        assert expected_error in (message.attachments[0].error_message or "")
        assert message.attachments[0].storage_path is None
        assert message.drafts[0].status == "failed"


def test_attachment_count_and_cumulative_bytes_limits_stop_processing(
    database, monkeypatch: pytest.MonkeyPatch
) -> None:
    SessionMaker, ids, tmp_path = database
    monkeypatch.setattr(service, "parse_pdf_bytes", lambda *_args, **_kwargs: _fake_pdf_pipeline(ids["customer"]))
    monkeypatch.setattr(service, "match_import_draft", _fake_match)
    monkeypatch.setattr(service, "load_active_pdf_template_rules", lambda _db: [])
    raw = _email_bytes("buyer@example.com", [("one.pdf", VALID_PDF), ("two.pdf", VALID_PDF), ("three.pdf", VALID_PDF)])
    with SessionMaker() as db:
        by_count, _ = service.ingest_raw_message(
            db,
            mailbox_key="sales",
            uidvalidity="attachment-limits",
            uid="count",
            raw_message=raw,
            settings=_settings(tmp_path, email_intake_max_attachments=1),
        )
        assert [item.status for item in by_count.attachments] == ["parsed", "failed"]
        assert "附件数量" in (by_count.attachments[1].error_message or "")
        assert by_count.attachments[1].storage_path is None

        by_total, _ = service.ingest_raw_message(
            db,
            mailbox_key="sales",
            uidvalidity="attachment-limits",
            uid="total",
            raw_message=raw,
            settings=_settings(
                tmp_path, email_intake_max_total_attachment_bytes=len(VALID_PDF)
            ),
        )
        assert [item.status for item in by_total.attachments] == ["parsed", "failed"]
        assert "累计大小" in (by_total.attachments[1].error_message or "")
        assert by_total.attachments[1].storage_path is None


def test_pdf_page_limit_and_unreadable_pdf_create_safe_failed_drafts(
    database, monkeypatch: pytest.MonkeyPatch
) -> None:
    SessionMaker, _ids, tmp_path = database

    class TooManyPages:
        pages = [object(), object()]

    with SessionMaker() as db:
        monkeypatch.setattr(service, "PdfReader", lambda *_args, **_kwargs: TooManyPages())
        too_many, _ = service.ingest_raw_message(
            db,
            mailbox_key="sales",
            uidvalidity="pdf-limits",
            uid="pages",
            raw_message=_email_bytes("buyer@example.com", [("order.pdf", VALID_PDF)]),
            settings=_settings(tmp_path, email_intake_max_pdf_pages=1),
        )
        assert too_many.drafts[0].status == "failed"
        assert too_many.drafts[0].last_error == "PDF 页数超过安全限制"

        monkeypatch.setattr(
            service,
            "PdfReader",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad PDF")),
        )
        unreadable, _ = service.ingest_raw_message(
            db,
            mailbox_key="sales",
            uidvalidity="pdf-limits",
            uid="unreadable",
            raw_message=_email_bytes("buyer@example.com", [("bad.pdf", VALID_PDF)]),
            settings=_settings(tmp_path),
        )
        assert unreadable.drafts[0].status == "failed"
        assert unreadable.drafts[0].last_error == "PDF 无法安全解析，请检查文件后重试"


def test_ingest_is_idempotent_keeps_multiple_attachments_and_marks_forwarded_sha(
    database, monkeypatch: pytest.MonkeyPatch
) -> None:
    SessionMaker, ids, tmp_path = database
    monkeypatch.setattr(service, "parse_pdf_bytes", lambda *_args, **_kwargs: _fake_pdf_pipeline(ids["customer"]))
    monkeypatch.setattr(service, "match_import_draft", _fake_match)
    monkeypatch.setattr(service, "load_active_pdf_template_rules", lambda _db: [])
    raw = _email_bytes(
        "Orders <buyer@example.com>",
        [("order.pdf", VALID_PDF), ("order.xlsx", _valid_xlsx_bytes()), ("unsafe.xlsm", b"macro")],
    )
    settings = _settings(tmp_path)
    with SessionMaker() as db:
        db.add(EmailOrderSenderMapping(customer_id=ids["customer"], sender_email="buyer@example.com", normalized_sender_email="buyer@example.com"))
        db.commit()
        first, created = service.ingest_raw_message(db, mailbox_key="sales", uidvalidity="77", uid="100", raw_message=raw, settings=settings)
        again, created_again = service.ingest_raw_message(db, mailbox_key="sales", uidvalidity="77", uid="100", raw_message=raw, settings=settings)
        forwarded, forwarded_created = service.ingest_raw_message(db, mailbox_key="sales", uidvalidity="77", uid="101", raw_message=raw, settings=settings)
        assert created is True and created_again is False and forwarded_created is True
        assert again.id == first.id
        assert db.scalar(select(func.count(EmailOrderIntakeMessage.id))) == 2
        first_attachments = db.scalars(select(EmailOrderIntakeAttachment).where(EmailOrderIntakeAttachment.message_id == first.id).order_by(EmailOrderIntakeAttachment.part_index)).all()
        assert [item.file_type for item in first_attachments] == ["pdf", "xlsx", "unsupported"]
        assert first_attachments[2].storage_path is None
        assert db.scalar(select(EmailOrderIntakeDraft).where(EmailOrderIntakeDraft.attachment_id == first_attachments[1].id)).status == "waiting_excel_parser"
        assert all(item.is_duplicate_content for item in forwarded.attachments)
        assert "preview_safety_token" not in (first.drafts[0].draft_json or "")


def test_unmapped_inactive_and_parser_customer_conflict_require_review(database, monkeypatch: pytest.MonkeyPatch) -> None:
    SessionMaker, ids, tmp_path = database
    monkeypatch.setattr(service, "match_import_draft", _fake_match)
    monkeypatch.setattr(service, "load_active_pdf_template_rules", lambda _db: [])
    settings = _settings(tmp_path)
    with SessionMaker() as db:
        monkeypatch.setattr(service, "parse_pdf_bytes", lambda *_args, **_kwargs: _fake_pdf_pipeline(ids["customer"]))
        unmapped, _ = service.ingest_raw_message(db, mailbox_key="sales", uidvalidity="1", uid="1", raw_message=_email_bytes("unknown@example.com", [("a.pdf", VALID_PDF)]), settings=settings)
        assert unmapped.status == "needs_mapping"

        db.add(EmailOrderSenderMapping(customer_id=ids["inactive"], sender_email="inactive@example.com", normalized_sender_email="inactive@example.com"))
        db.add(EmailOrderSenderMapping(customer_id=ids["other"], sender_email="conflict@example.com", normalized_sender_email="conflict@example.com"))
        db.commit()
        inactive, _ = service.ingest_raw_message(db, mailbox_key="sales", uidvalidity="1", uid="2", raw_message=_email_bytes("inactive@example.com", [("b.pdf", VALID_PDF)]), settings=settings)
        conflict, _ = service.ingest_raw_message(db, mailbox_key="sales", uidvalidity="1", uid="3", raw_message=_email_bytes("conflict@example.com", [("c.pdf", VALID_PDF)]), settings=settings)
        assert inactive.status == "needs_mapping"
        assert conflict.status == "needs_confirmation"
        assert conflict.drafts[0].customer_id == ids["other"]


def test_oversized_attachment_creates_visible_failed_draft(
    api_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, SessionMaker, ids, tmp_path = api_client
    settings = _settings(tmp_path, email_intake_max_attachment_bytes=8)
    raw = _email_bytes("large@example.com", [("too-large.pdf", VALID_PDF)])
    with SessionMaker() as db:
        message, created = service.ingest_raw_message(
            db,
            mailbox_key="sales",
            uidvalidity="oversize",
            uid="1",
            raw_message=raw,
            settings=settings,
        )
        assert created is True
        assert message.status == "failed"
        attachment = message.attachments[0]
        draft = message.drafts[0]
        assert attachment.status == "failed"
        assert attachment.storage_path is None
        assert attachment.error_message == "附件超过配置的大小限制"
        assert draft.status == "failed"
        assert draft.parse_method == "rejected"
        assert draft.last_error == attachment.error_message
        draft_id = draft.id

    _login(client, "n043-admin")
    response = client.get("/api/email-order-intake/drafts")
    assert response.status_code == 200, response.text
    visible = next(item for item in response.json()["items"] if item["id"] == draft_id)
    assert visible["status"] == "failed"
    assert visible["attachment_name"] == "too-large.pdf"
    assert visible["file_type"] == "pdf"
    assert visible["error_message"] == "附件超过配置的大小限制"


def test_needs_mapping_retry_uses_new_sender_mapping(
    api_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, SessionMaker, ids, tmp_path = api_client
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        service,
        "parse_pdf_bytes",
        lambda *_args, **_kwargs: _fake_pdf_pipeline(ids["customer"]),
    )
    monkeypatch.setattr(service, "match_import_draft", _fake_match)
    monkeypatch.setattr(service, "load_active_pdf_template_rules", lambda _db: [])
    monkeypatch.setattr(service, "load_settings", lambda: settings)
    monkeypatch.setattr("app.api.email_intake.load_settings", lambda: settings)
    with SessionMaker() as db:
        message, _ = service.ingest_raw_message(
            db,
            mailbox_key="sales",
            uidvalidity="mapping",
            uid="1",
            raw_message=_email_bytes(
                "new-buyer@example.com", [("order.pdf", VALID_PDF)]
            ),
            settings=settings,
        )
        draft_id = message.drafts[0].id
        assert message.drafts[0].status == "needs_mapping"
        assert message.drafts[0].customer_id is None

    _login(client, "n043-admin")
    mapping = client.post(
        "/api/email-order-intake/sender-mappings",
        json={
            "customer_id": ids["customer"],
            "sender_email": "new-buyer@example.com",
        },
    )
    assert mapping.status_code == 201, mapping.text
    retried = client.post(f"/api/email-order-intake/drafts/{draft_id}/retry")
    assert retried.status_code == 200, retried.text
    payload = retried.json()
    assert payload["status"] == "review_ready"
    assert payload["customer_id"] == ids["customer"]
    assert payload["customer_name"] == "客户甲"
    assert payload["draft"]["matched_customer_id"] == ids["customer"]
    assert payload["created_at"].endswith("Z")
    assert payload["updated_at"].endswith("Z")

    rematched = client.post(
        f"/api/email-order-intake/drafts/{draft_id}/rematch",
        json={"customer_id": ids["customer"]},
    )
    assert rematched.status_code == 200, rematched.text
    assert rematched.json()["created_at"].endswith("Z")
    assert rematched.json()["updated_at"].endswith("Z")
    with SessionMaker() as db:
        draft = db.get(EmailOrderIntakeDraft, draft_id)
        assert draft.customer_id == ids["customer"]
        assert draft.message.mapped_customer_id == ids["customer"]


def test_draft_list_exposes_review_summary_and_duplicate_details(
    api_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, SessionMaker, ids, tmp_path = api_client
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        service,
        "parse_pdf_bytes",
        lambda *_args, **_kwargs: _fake_pdf_pipeline(ids["customer"]),
    )
    monkeypatch.setattr(service, "match_import_draft", _fake_match)
    monkeypatch.setattr(service, "load_active_pdf_template_rules", lambda _db: [])
    raw = _email_bytes("summary@example.com", [("summary.pdf", VALID_PDF)])
    with SessionMaker() as db:
        db.add(
            EmailOrderSenderMapping(
                customer_id=ids["customer"],
                sender_email="summary@example.com",
                normalized_sender_email="summary@example.com",
            )
        )
        db.commit()
        first, _ = service.ingest_raw_message(
            db,
            mailbox_key="sales",
            uidvalidity="summary",
            uid="1",
            raw_message=raw,
            settings=settings,
        )
        duplicate, _ = service.ingest_raw_message(
            db,
            mailbox_key="sales",
            uidvalidity="summary",
            uid="2",
            raw_message=raw,
            settings=settings,
        )
        first_attachment_id = first.attachments[0].id
        duplicate_draft_id = duplicate.drafts[0].id

    _login(client, "n043-admin")
    response = client.get("/api/email-order-intake/drafts")
    assert response.status_code == 200, response.text
    row = next(
        item for item in response.json()["items"] if item["id"] == duplicate_draft_id
    )
    assert row["customer_id"] == ids["customer"]
    assert row["customer_name"] == "客户甲"
    assert row["source_name"] == "summary.pdf"
    assert row["attachment_name"] == "summary.pdf"
    assert row["file_type"] == "pdf"
    assert row["sender_email"] == "summary@example.com"
    assert row["subject"] == "Purchase order"
    assert row["received_at"] is None or row["received_at"].endswith("Z")
    assert row["created_at"].endswith("Z")
    assert row["updated_at"] is None or row["updated_at"].endswith("Z")
    assert row["is_duplicate_content"] is True
    assert row["duplicate_of_attachment_id"] == first_attachment_id
    assert row["error_message"] is None
    assert row["file_sha256"]


@pytest.fixture()
def api_client(database):
    SessionMaker, ids, tmp_path = database
    application = create_app()

    def override_get_db() -> Generator[Session, None, None]:
        with SessionMaker() as session:
            yield session

    application.dependency_overrides[get_db] = override_get_db
    with TestClient(application) as client:
        yield client, SessionMaker, ids, tmp_path
    application.dependency_overrides.clear()


def _login(client: TestClient, username: str) -> None:
    response = client.post("/api/auth/login", json={"username": username, "password": "TestOnly123!"})
    assert response.status_code == 200, response.text


def test_mapping_permissions_and_customer_scope(api_client) -> None:
    client, SessionMaker, ids, _tmp_path = api_client
    _login(client, "n043-admin")
    created = client.post(
        "/api/email-order-intake/sender-mappings",
        json={
            "customer_id": ids["customer"],
            "sender_email": "PO@Example.com",
            "notes": "供应商甲采购价 2.45",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["notes"] == "供应商甲采购价 2.45"
    client.post("/api/auth/logout")

    _login(client, "n043-workshop")
    assert client.get("/api/email-order-intake/status").status_code == 403
    client.post("/api/auth/logout")

    _login(client, "n043-sales")
    rows = client.get("/api/email-order-intake/sender-mappings")
    assert rows.status_code == 200
    assert [row["customer_id"] for row in rows.json()["items"]] == [ids["customer"]]
    assert rows.json()["items"][0]["notes"] is None
    assert client.post("/api/email-order-intake/sender-mappings", json={"customer_id": ids["customer"], "sender_email": "other@example.com"}).status_code == 403
    client.post("/api/auth/logout")

    _login(client, "n043-admin")
    mapping_id = created.json()["id"]
    updated = client.put(
        f"/api/email-order-intake/sender-mappings/{mapping_id}",
        json={"sender_email": "orders-updated@example.com", "is_active": False},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["sender_email"] == "orders-updated@example.com"
    assert updated.json()["is_active"] is False
    assert client.delete(f"/api/email-order-intake/sender-mappings/{mapping_id}").json() == {"ok": True}


def test_all_email_intake_api_timestamps_are_explicit_rfc3339(
    api_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, SessionMaker, ids, tmp_path = api_client
    settings = _settings(tmp_path)
    timestamp = datetime(2026, 7, 20, 1, 2, 3)
    with SessionMaker() as db:
        mapping = EmailOrderSenderMapping(
            customer_id=ids["customer"],
            sender_email="time@example.com",
            normalized_sender_email="time@example.com",
            created_at=timestamp,
            updated_at=timestamp,
        )
        message = EmailOrderIntakeMessage(
            mailbox_key=settings.email_intake_mailbox_key,
            imap_uidvalidity="time",
            imap_uid="1",
            sender_email="time@example.com",
            normalized_sender_email="time@example.com",
            raw_message_sha256="4" * 64,
            mapped_customer_id=ids["customer"],
            status="review_ready",
            received_at=timestamp,
            processed_at=timestamp,
            created_at=timestamp,
        )
        db.add_all([mapping, message])
        db.flush()
        attachment = EmailOrderIntakeAttachment(
            message_id=message.id,
            part_index=1,
            filename="time.pdf",
            content_type="application/pdf",
            byte_size=len(VALID_PDF),
            file_sha256="5" * 64,
            file_type="pdf",
            status="parsed",
            created_at=timestamp,
        )
        db.add(attachment)
        db.flush()
        draft = EmailOrderIntakeDraft(
            message_id=message.id,
            attachment_id=attachment.id,
            customer_id=ids["customer"],
            parser_type="pdf",
            status="review_ready",
            draft_json="{}",
            source_name="time.pdf",
            file_sha256="5" * 64,
            created_at=timestamp,
            updated_at=timestamp,
        )
        poll_state = EmailOrderIntakePollState(
            mailbox_key=settings.email_intake_mailbox_key,
            last_started_at=timestamp,
            last_finished_at=timestamp,
            last_status="success",
        )
        db.add_all([draft, poll_state])
        db.commit()
        message_id = message.id

    monkeypatch.setattr("app.api.email_intake.load_settings", lambda: settings)
    _login(client, "n043-admin")

    mapping_row = client.get("/api/email-order-intake/sender-mappings").json()["items"][0]
    assert mapping_row["created_at"] == "2026-07-20T01:02:03Z"
    assert mapping_row["updated_at"] == "2026-07-20T01:02:03Z"

    message_row = client.get("/api/email-order-intake/messages").json()["items"][0]
    assert message_row["received_at"] == "2026-07-20T01:02:03Z"
    assert message_row["processed_at"] == "2026-07-20T01:02:03Z"
    assert message_row["created_at"] == "2026-07-20T01:02:03Z"

    message_detail = client.get(
        f"/api/email-order-intake/messages/{message_id}"
    ).json()
    assert message_detail["attachments"][0]["created_at"] == "2026-07-20T01:02:03Z"
    assert message_detail["drafts"][0]["created_at"] == "2026-07-20T01:02:03Z"
    assert message_detail["drafts"][0]["updated_at"] == "2026-07-20T01:02:03Z"

    status_payload = client.get("/api/email-order-intake/status").json()
    assert status_payload["last_poll_at"] == "2026-07-20T01:02:03Z"


def test_customer_scope_hides_other_customer_messages_and_drafts(api_client) -> None:
    client, SessionMaker, ids, _tmp_path = api_client
    with SessionMaker() as db:
        own = EmailOrderIntakeMessage(mailbox_key="sales", imap_uidvalidity="8", imap_uid="1", sender_email="own@example.com", normalized_sender_email="own@example.com", raw_message_sha256="1" * 64, mapped_customer_id=ids["customer"], status="review_ready")
        other = EmailOrderIntakeMessage(mailbox_key="sales", imap_uidvalidity="8", imap_uid="2", sender_email="other@example.com", normalized_sender_email="other@example.com", raw_message_sha256="2" * 64, mapped_customer_id=ids["other"], status="review_ready")
        db.add_all([own, other])
        db.flush()
        attachment = EmailOrderIntakeAttachment(message_id=other.id, part_index=1, filename="other.pdf", content_type="application/pdf", byte_size=1, file_sha256="3" * 64, storage_path="33/33/" + "3" * 64 + ".pdf", file_type="pdf", status="parsed")
        db.add(attachment)
        db.flush()
        draft = EmailOrderIntakeDraft(message_id=other.id, attachment_id=attachment.id, customer_id=ids["other"], parser_type="pdf", status="review_ready", source_name="other.pdf", file_sha256="3" * 64)
        db.add(draft)
        db.commit()
        other_message_id, other_draft_id = other.id, draft.id
    _login(client, "n043-sales")
    response = client.get("/api/email-order-intake/messages")
    assert response.status_code == 200
    assert [item["mapped_customer_id"] for item in response.json()["items"]] == [ids["customer"]]
    assert client.get(f"/api/email-order-intake/messages/{other_message_id}").status_code == 403
    assert client.get(f"/api/email-order-intake/drafts/{other_draft_id}").status_code == 403


def test_mixed_customer_message_filters_sibling_drafts_attachments_and_duplicates(
    api_client,
) -> None:
    client, SessionMaker, ids, _tmp_path = api_client
    with SessionMaker() as db:
        message = EmailOrderIntakeMessage(
            mailbox_key="mixed",
            imap_uidvalidity="1",
            imap_uid="1",
            sender_email="mixed@example.com",
            normalized_sender_email="mixed@example.com",
            subject="Multiple attachments",
            raw_message_sha256="a" * 64,
            mapped_customer_id=ids["other"],
            status="failed",
            last_error="供应商甲采购价 2.45",
        )
        db.add(message)
        db.flush()
        own_attachment = EmailOrderIntakeAttachment(
            message_id=message.id,
            part_index=1,
            filename="own.pdf",
            content_type="application/pdf",
            byte_size=1,
            file_sha256="b" * 64,
            file_type="pdf",
            status="parsed",
        )
        other_attachment = EmailOrderIntakeAttachment(
            message_id=message.id,
            part_index=2,
            filename="other-secret.pdf",
            content_type="application/pdf",
            byte_size=1,
            file_sha256="c" * 64,
            file_type="pdf",
            status="failed",
        )
        db.add_all([own_attachment, other_attachment])
        db.flush()
        own_attachment.is_duplicate_content = True
        own_attachment.duplicate_of_attachment_id = other_attachment.id
        db.add_all(
            [
                EmailOrderIntakeDraft(
                    message_id=message.id,
                    attachment_id=own_attachment.id,
                    customer_id=ids["customer"],
                    parser_type="pdf",
                    status="review_ready",
                    draft_json='{"items": [{"product_code": "OWN"}]}',
                    source_name="own.pdf",
                    file_sha256="b" * 64,
                ),
                EmailOrderIntakeDraft(
                    message_id=message.id,
                    attachment_id=other_attachment.id,
                    customer_id=ids["other"],
                    parser_type="pdf",
                    status="failed",
                    draft_json='{"items": [{"product_code": "SECRET"}]}',
                    source_name="other-secret.pdf",
                    file_sha256="c" * 64,
                ),
            ]
        )
        db.commit()
        message_id = message.id

    _login(client, "n043-sales")
    scoped_list = client.get("/api/email-order-intake/messages").json()["items"]
    scoped_row = next(item for item in scoped_list if item["id"] == message_id)
    assert scoped_row["status"] == "review_ready"
    assert scoped_row["mapped_customer_id"] is None
    assert scoped_row["last_error"] is None
    detail = client.get(f"/api/email-order-intake/messages/{message_id}")
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert [item["filename"] for item in payload["attachments"]] == ["own.pdf"]
    assert [item["source_name"] for item in payload["drafts"]] == ["own.pdf"]
    assert payload["attachments"][0]["is_duplicate_content"] is False
    assert payload["attachments"][0]["duplicate_of_attachment_id"] is None
    assert payload["drafts"][0]["duplicate_of_attachment_id"] is None
    assert "other-secret.pdf" not in detail.text
    assert "SECRET" not in detail.text
    assert "2.45" not in detail.text

    client.post("/api/auth/logout")
    _login(client, "n043-admin")
    admin_payload = client.get(
        f"/api/email-order-intake/messages/{message_id}"
    ).json()
    assert {item["filename"] for item in admin_payload["attachments"]} == {
        "own.pdf",
        "other-secret.pdf",
    }
    own_row = next(
        item for item in admin_payload["attachments"] if item["filename"] == "own.pdf"
    )
    assert own_row["duplicate_of_attachment_id"] is None


def test_mapping_update_and_retry_rematch_revalidate_customer_scope(
    api_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, SessionMaker, ids, _tmp_path = api_client
    with SessionMaker() as db:
        scoped_user = db.scalar(select(User).where(User.username == "n043-sales"))
        mapping_own = EmailOrderSenderMapping(
            customer_id=ids["customer"],
            sender_email="own-map@example.com",
            normalized_sender_email="own-map@example.com",
        )
        mapping_other = EmailOrderSenderMapping(
            customer_id=ids["other"],
            sender_email="other-map@example.com",
            normalized_sender_email="other-map@example.com",
        )
        retry_mapping = EmailOrderSenderMapping(
            customer_id=ids["customer"],
            sender_email="retry-scope@example.com",
            normalized_sender_email="retry-scope@example.com",
        )
        message = EmailOrderIntakeMessage(
            mailbox_key="scope-recheck",
            imap_uidvalidity="1",
            imap_uid="1",
            sender_email="retry-scope@example.com",
            normalized_sender_email="retry-scope@example.com",
            raw_message_sha256="d" * 64,
            mapped_customer_id=ids["customer"],
            status="review_ready",
            subject="供应商报价 2.45",
        )
        db.add_all([mapping_own, mapping_other, retry_mapping, message])
        db.flush()
        attachment = EmailOrderIntakeAttachment(
            message_id=message.id,
            part_index=1,
            filename="scope.pdf",
            content_type="application/pdf",
            byte_size=1,
            file_sha256="e" * 64,
            file_type="pdf",
            status="parsed",
        )
        db.add(attachment)
        db.flush()
        draft = EmailOrderIntakeDraft(
            message_id=message.id,
            attachment_id=attachment.id,
            customer_id=ids["customer"],
            parser_type="pdf",
            status="review_ready",
            draft_json="{}",
            source_name="scope.pdf",
            file_sha256="e" * 64,
        )
        db.add(draft)
        db.commit()
        own_mapping_id = mapping_own.id
        other_mapping_id = mapping_other.id
        draft_id = draft.id
        db.expunge(scoped_user)

    client.app.dependency_overrides[email_api.can_manage] = lambda: scoped_user
    try:
        # The original mapping customer is checked before a scoped operator can
        # move the record into an allowed customer.
        old_scope = client.put(
            f"/api/email-order-intake/sender-mappings/{other_mapping_id}",
            json={"customer_id": ids["customer"]},
        )
        assert old_scope.status_code == 403
        # The target customer is checked independently as well.
        target_scope = client.put(
            f"/api/email-order-intake/sender-mappings/{own_mapping_id}",
            json={"customer_id": ids["other"]},
        )
        assert target_scope.status_code == 403

        def escaping_retry(_db, row, _settings=None, *, commit=True):
            assert commit is False
            row.customer_id = ids["other"]
            return row

        monkeypatch.setattr(email_api, "retry_draft", escaping_retry)
        retried = client.post(f"/api/email-order-intake/drafts/{draft_id}/retry")
        assert retried.status_code == 403

        def escaping_rematch(_db, row, _customer, *, commit=True):
            assert commit is False
            row.customer_id = ids["other"]
            return row

        monkeypatch.setattr(email_api, "rematch_draft", escaping_rematch)
        rematched = client.post(
            f"/api/email-order-intake/drafts/{draft_id}/rematch",
            json={"customer_id": ids["customer"]},
        )
        assert rematched.status_code == 403
    finally:
        client.app.dependency_overrides.pop(email_api.can_manage, None)

    with SessionMaker() as db:
        assert db.get(EmailOrderIntakeDraft, draft_id).customer_id == ids["customer"]


def test_email_draft_detail_reuses_cost_redaction_and_hides_supplier_data(
    api_client,
) -> None:
    client, SessionMaker, ids, _tmp_path = api_client
    source_draft = {
        "customer_name": "客户甲",
        "customer_po": "PO-REDaction-1",
        "items": [
            {
                "product_code": "P001",
                "product_name": "测试纸箱",
                "spec": "100x200",
                "unit_price": 8.8,
                "product_default_price": 9.0,
                "amount": 88.0,
                "estimated_cost": 3.2,
                "estimated_gross_profit": 5.6,
                "supplier_name": "供应商甲",
                "standard_material_label": "D4B｜供应商甲｜180g｜2.45",
                "nested": {
                    "vendor_quote": 2.45,
                    "safe_note": "保留业务备注",
                },
            }
        ],
        "supplier_candidates": [
            {"supplier_name": "供应商甲", "supplier_price": 2.45}
        ],
        "warnings": ["产品待确认", "供应商报价需要确认"],
        "integrity_errors": [
            "PDF 合计金额 88.00 与系统金额不一致",
            "昆山鸣朋采购价 2.45，请人工核对供应商",
        ],
    }
    with SessionMaker() as db:
        message = EmailOrderIntakeMessage(
            mailbox_key="sales",
            imap_uidvalidity="redaction",
            imap_uid="1",
            sender_email="buyer@example.com",
            normalized_sender_email="buyer@example.com",
            raw_message_sha256="6" * 64,
            mapped_customer_id=ids["customer"],
            status="review_ready",
        )
        db.add(message)
        db.flush()
        attachment = EmailOrderIntakeAttachment(
            message_id=message.id,
            part_index=1,
            filename="供应商报价.pdf",
            content_type="application/pdf",
            byte_size=len(VALID_PDF),
            file_sha256="7" * 64,
            file_type="pdf",
            status="parsed",
        )
        db.add(attachment)
        db.flush()
        draft = EmailOrderIntakeDraft(
            message_id=message.id,
            attachment_id=attachment.id,
            customer_id=ids["customer"],
            parser_type="pdf",
            parse_status="recognized",
            status="review_ready",
            draft_json=json.dumps(source_draft, ensure_ascii=False),
            source_name="供应商报价.pdf",
            file_sha256="7" * 64,
        )
        db.add(draft)
        db.commit()
        message_id = message.id
        draft_id = draft.id

    def assert_no_commercial_data(payload: dict) -> None:
        assert payload["subject"] is None
        assert payload["source_name"] is None
        assert payload["attachment_name"] is None
        parsed = payload["draft"]
        item = parsed["items"][0]
        assert item["product_code"] == "P001"
        assert item["product_name"] == "测试纸箱"
        assert item["spec"] == "100x200"
        assert "standard_material_label" not in item
        assert "nested" not in item
        assert "warnings" not in parsed
        assert "integrity_errors" not in parsed
        assert "supplier_candidates" not in parsed
        for key in (
            "unit_price",
            "product_default_price",
            "amount",
            "estimated_cost",
            "estimated_gross_profit",
            "supplier_name",
        ):
            assert key not in item
        assert "供应商甲" not in json.dumps(parsed, ensure_ascii=False)
        assert "昆山鸣朋" not in json.dumps(parsed, ensure_ascii=False)
        assert "2.45" not in json.dumps(parsed, ensure_ascii=False)

    _login(client, "n043-sales")
    draft_response = client.get(f"/api/email-order-intake/drafts/{draft_id}")
    assert draft_response.status_code == 200, draft_response.text
    assert_no_commercial_data(draft_response.json())
    message_response = client.get(f"/api/email-order-intake/messages/{message_id}")
    assert message_response.status_code == 200, message_response.text
    assert message_response.json()["attachments"][0]["filename"] is None
    assert_no_commercial_data(message_response.json()["drafts"][0])

    client.post("/api/auth/logout")
    _login(client, "n043-admin")
    admin_payload = client.get(
        f"/api/email-order-intake/drafts/{draft_id}"
    ).json()["draft"]
    admin_item = admin_payload["items"][0]
    assert admin_item["unit_price"] == 8.8
    assert admin_item["estimated_cost"] == 3.2
    assert admin_item["supplier_name"] == "供应商甲"
    assert admin_item["standard_material_label"] == "D4B｜供应商甲｜180g｜2.45"
    assert admin_payload["integrity_errors"][0].endswith("不一致")


def test_status_counts_and_poll_statistics_follow_customer_scope(
    api_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, SessionMaker, ids, tmp_path = api_client
    settings = _settings(tmp_path)
    with SessionMaker() as db:
        mixed = EmailOrderIntakeMessage(
            mailbox_key=settings.email_intake_mailbox_key,
            imap_uidvalidity="scope-status",
            imap_uid="3",
            sender_email="mixed@example.com",
            normalized_sender_email="mixed@example.com",
            raw_message_sha256="7" * 64,
            mapped_customer_id=ids["other"],
            status="failed",
        )
        db.add_all(
            [
                EmailOrderIntakeMessage(
                    mailbox_key=settings.email_intake_mailbox_key,
                    imap_uidvalidity="scope-status",
                    imap_uid="1",
                    sender_email="own@example.com",
                    normalized_sender_email="own@example.com",
                    raw_message_sha256="8" * 64,
                    mapped_customer_id=ids["customer"],
                    status="review_ready",
                ),
                EmailOrderIntakeMessage(
                    mailbox_key=settings.email_intake_mailbox_key,
                    imap_uidvalidity="scope-status",
                    imap_uid="2",
                    sender_email="other@example.com",
                    normalized_sender_email="other@example.com",
                    raw_message_sha256="9" * 64,
                    mapped_customer_id=ids["other"],
                    status="failed",
                ),
                EmailOrderIntakePollState(
                    mailbox_key=settings.email_intake_mailbox_key,
                    last_started_at=datetime(2026, 7, 20, 1, 0, 0),
                    last_finished_at=datetime(2026, 7, 20, 1, 1, 0),
                    last_status="success",
                    examined_count=77,
                    created_count=66,
                    duplicate_count=55,
                    failed_count=44,
                ),
                mixed,
                EmailOrderIntakeMessage(
                    mailbox_key="another-mailbox",
                    imap_uidvalidity="scope-status",
                    imap_uid="4",
                    sender_email="other-mailbox@example.com",
                    normalized_sender_email="other-mailbox@example.com",
                    raw_message_sha256="5" * 64,
                    mapped_customer_id=ids["customer"],
                    status="ignored",
                ),
            ]
        )
        db.flush()
        mixed_attachment = EmailOrderIntakeAttachment(
            message_id=mixed.id,
            part_index=1,
            filename="mixed-own.pdf",
            content_type="application/pdf",
            byte_size=1,
            file_sha256="6" * 64,
            file_type="pdf",
            status="parsed",
        )
        db.add(mixed_attachment)
        db.flush()
        db.add(
            EmailOrderIntakeDraft(
                message_id=mixed.id,
                attachment_id=mixed_attachment.id,
                customer_id=ids["customer"],
                parser_type="pdf",
                status="needs_confirmation",
                draft_json="{}",
                source_name="mixed-own.pdf",
                file_sha256="6" * 64,
            )
        )
        db.commit()

    monkeypatch.setattr("app.api.email_intake.load_settings", lambda: settings)
    _login(client, "n043-sales")
    scoped = client.get("/api/email-order-intake/status")
    assert scoped.status_code == 200, scoped.text
    assert scoped.json()["statistics_scope"] == "customer_scope"
    assert scoped.json()["message_counts"] == {
        "needs_confirmation": 1,
        "review_ready": 1,
    }
    assert scoped.json()["last_poll_counts"] == {}
    assert scoped.json()["last_poll_status"] == "scope_filtered"
    assert scoped.json()["last_poll_at"] is None
    assert "77" not in scoped.text and "66" not in scoped.text

    client.post("/api/auth/logout")
    _login(client, "n043-admin")
    unrestricted = client.get("/api/email-order-intake/status")
    assert unrestricted.status_code == 200, unrestricted.text
    assert unrestricted.json()["statistics_scope"] == "all_customers"
    assert unrestricted.json()["message_counts"] == {
        "failed": 2,
        "review_ready": 1,
    }
    assert unrestricted.json()["last_poll_counts"] == {
        "examined": 77,
        "created": 66,
        "duplicates": 55,
        "failed": 44,
    }


def test_excel_attachment_remains_waiting_for_dedicated_parser(api_client) -> None:
    client, SessionMaker, ids, tmp_path = api_client
    settings = _settings(tmp_path)
    with SessionMaker() as db:
        db.add(
            EmailOrderSenderMapping(
                customer_id=ids["customer"],
                sender_email="excel@example.com",
                normalized_sender_email="excel@example.com",
            )
        )
        db.commit()
        message, created = service.ingest_raw_message(
            db,
            mailbox_key="sales",
            uidvalidity="excel-waiting",
            uid="1",
            raw_message=_email_bytes(
                "excel@example.com", [("order.xls", VALID_XLS)]
            ),
            settings=settings,
        )
        assert created is True
        attachment = message.attachments[0]
        draft = message.drafts[0]
        assert message.status == "waiting_excel_parser"
        assert attachment.status == "waiting_excel_parser"
        assert draft.status == "waiting_excel_parser"
        assert draft.parse_status == "waiting_excel_parser"
        assert json.loads(draft.draft_json or "{}")["items"] == []
        draft_id = draft.id

    _login(client, "n043-sales")
    detail = client.get(f"/api/email-order-intake/drafts/{draft_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["status"] == "waiting_excel_parser"
    assert detail.json()["parse_status"] == "waiting_excel_parser"
    assert detail.json()["draft"]["recognition_status"] == "waiting_excel_parser"
    preview = client.post(f"/api/email-order-intake/drafts/{draft_id}/open-preview")
    assert preview.status_code == 409
    assert "Excel" in preview.text


def test_duplicate_attachment_links_are_isolated_by_mapped_customer(database) -> None:
    SessionMaker, ids, tmp_path = database
    settings = _settings(tmp_path)
    raw_content = _valid_xlsx_bytes()
    with SessionMaker() as db:
        db.add_all(
            [
                EmailOrderSenderMapping(
                    customer_id=ids["customer"],
                    sender_email="first@example.com",
                    normalized_sender_email="first@example.com",
                ),
                EmailOrderSenderMapping(
                    customer_id=ids["other"],
                    sender_email="second@example.com",
                    normalized_sender_email="second@example.com",
                ),
            ]
        )
        db.commit()
        first, _ = service.ingest_raw_message(
            db,
            mailbox_key="sales",
            uidvalidity="customer-dedup",
            uid="1",
            raw_message=_email_bytes(
                "first@example.com", [("same.xlsx", raw_content)]
            ),
            settings=settings,
        )
        other, _ = service.ingest_raw_message(
            db,
            mailbox_key="sales",
            uidvalidity="customer-dedup",
            uid="2",
            raw_message=_email_bytes(
                "second@example.com", [("same.xlsx", raw_content)]
            ),
            settings=settings,
        )
        same_customer, _ = service.ingest_raw_message(
            db,
            mailbox_key="sales",
            uidvalidity="customer-dedup",
            uid="3",
            raw_message=_email_bytes(
                "first@example.com", [("same.xlsx", raw_content)]
            ),
            settings=settings,
        )
        assert first.attachments[0].is_duplicate_content is False
        assert other.attachments[0].is_duplicate_content is False
        assert other.attachments[0].duplicate_of_attachment_id is None
        assert same_customer.attachments[0].is_duplicate_content is True
        assert (
            same_customer.attachments[0].duplicate_of_attachment_id
            == first.attachments[0].id
        )


def test_poll_lock_blocks_parallel_run_and_recovers_stale_lock(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.email_intake_storage_dir.mkdir(parents=True, exist_ok=True)
    lock_path = settings.email_intake_storage_dir / ".poll.lock"
    lock_path.write_text("pid=123", encoding="ascii")

    with pytest.raises(service.EmailIntakeBusyError, match="正在执行"):
        with service._poll_lock(settings):
            pass
    assert lock_path.exists()

    old_timestamp = time.time() - service.POLL_LOCK_STALE_SECONDS - 5
    os.utime(lock_path, (old_timestamp, old_timestamp))
    with service._poll_lock(settings):
        assert lock_path.exists()
    assert not lock_path.exists()


def test_poll_lock_owner_does_not_delete_a_replacement_lock(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    replacement = "token=replacement-owner\npid=999\n"
    lock_path = settings.email_intake_storage_dir / ".poll.lock"
    with service._poll_lock(settings):
        lock_path.unlink()
        lock_path.write_text(replacement, encoding="ascii")
    assert lock_path.read_text(encoding="ascii") == replacement
    lock_path.unlink()


def test_regular_message_uid_integrity_race_recovers_the_winner(tmp_path: Path) -> None:
    raced = SimpleNamespace(id=99, imap_uid="42")
    db = MagicMock(spec=Session)
    db.scalar.side_effect = [None, None, raced]
    db.flush.side_effect = IntegrityError("insert", {}, Exception("duplicate"))

    result, created = service.ingest_raw_message(
        db,
        mailbox_key="imap-race",
        uidvalidity="7",
        uid="42",
        raw_message=_email_bytes("race@example.com", []),
        settings=_settings(tmp_path),
    )

    assert result is raced
    assert created is False
    db.rollback.assert_called_once_with()


def test_manual_poll_requires_manage_permission(api_client, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _SessionMaker, _ids, _tmp_path = api_client
    monkeypatch.setattr("app.api.email_intake.poll_mailbox_once", lambda _db: {"examined": 2, "created": 1, "duplicates": 1, "failed": 0})
    _login(client, "n043-sales")
    assert client.post("/api/email-order-intake/poll-once").status_code == 403
    client.post("/api/auth/logout")
    _login(client, "n043-admin")
    response = client.post("/api/email-order-intake/poll-once")
    assert response.status_code == 200
    assert response.json()["created"] == 1
    assert response.json()["received_count"] == 1


def test_backend_routes_match_frontend_contract(api_client) -> None:
    client, _SessionMaker, _ids, _tmp_path = api_client
    paths = {route.path for route in client.app.routes}
    assert {
        "/api/email-order-intake/status",
        "/api/email-order-intake/drafts",
        "/api/email-order-intake/sender-mappings",
        "/api/email-order-intake/poll-once",
        "/api/email-order-intake/drafts/{draft_id}/retry",
        "/api/email-order-intake/drafts/{draft_id}/open-preview",
    } <= paths
    assert "/api/email-intake/status" not in paths


def test_open_preview_returns_fresh_token_and_never_creates_order(api_client, monkeypatch: pytest.MonkeyPatch) -> None:
    client, SessionMaker, ids, tmp_path = api_client
    settings = _settings(tmp_path)
    content = b"%PDF-preview"
    digest = __import__("hashlib").sha256(content).hexdigest()
    relative = service._store_allowed_attachment(content, digest, "pdf", settings)
    with SessionMaker() as db:
        message = EmailOrderIntakeMessage(mailbox_key="sales", imap_uidvalidity="2", imap_uid="2", sender_email="buyer@example.com", normalized_sender_email="buyer@example.com", raw_message_sha256="a" * 64, mapped_customer_id=ids["customer"], status="review_ready")
        db.add(message)
        db.flush()
        attachment = EmailOrderIntakeAttachment(message_id=message.id, part_index=1, filename="order.pdf", content_type="application/pdf", byte_size=len(content), file_sha256=digest, storage_path=relative, file_type="pdf", status="parsed")
        db.add(attachment)
        db.flush()
        draft = EmailOrderIntakeDraft(message_id=message.id, attachment_id=attachment.id, customer_id=ids["customer"], parser_type="pdf", parse_method="text", parse_status="recognized", status="review_ready", draft_json="{}", source_name="order.pdf", file_sha256=digest)
        db.add(draft)
        db.commit()
        draft_id = draft.id
        before = db.scalar(select(func.count(Order.id)))

    counter = {"value": 0}
    monkeypatch.setattr("app.api.email_intake.load_settings", lambda: settings)
    monkeypatch.setattr("app.api.email_intake.read_stored_attachment", lambda _attachment: content)
    monkeypatch.setattr("app.api.email_intake.load_active_pdf_template_rules", lambda _db: [])
    monkeypatch.setattr("app.api.email_intake._parse_order_pdf_preview", lambda *_args: {"customer_route": {"status": "locked", "template_customer_id": ids["customer"]}, "items": [], "recognition_status": "recognized"})
    monkeypatch.setattr("app.api.email_intake._match_pdf_preview_for_user", lambda _db, value, _user, customer_id=None: {**value, "matched_customer_id": customer_id})
    def finalize(value, _user):
        counter["value"] += 1
        return {**value, "preview_safety_token": f"fresh-{counter['value']}"}
    monkeypatch.setattr("app.api.email_intake._finalize_pdf_preview_for_user", finalize)

    _login(client, "n043-sales")
    first = client.post(f"/api/email-order-intake/drafts/{draft_id}/open-preview")
    second = client.post(f"/api/email-order-intake/drafts/{draft_id}/open-preview")
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["preview_safety_token"] != second.json()["preview_safety_token"]
    with SessionMaker() as db:
        assert db.scalar(select(func.count(Order.id))) == before
        persisted = db.get(EmailOrderIntakeDraft, draft_id)
        assert "preview_safety_token" not in (persisted.draft_json or "")


def test_poll_backlog_drains_oldest_unprocessed_and_records_success(
    database, monkeypatch: pytest.MonkeyPatch
) -> None:
    SessionMaker, _ids, tmp_path = database
    password_file = tmp_path / "poll-password.txt"
    password_file.write_text("poll-test-secret", encoding="utf-8")
    settings = _settings(
        tmp_path,
        email_intake_enabled=True,
        email_imap_host="imap.example.com",
        email_imap_username="orders@example.com",
        email_imap_password_file=password_file,
        email_intake_max_messages=2,
    )
    fetched_uids: list[str] = []
    raw_messages = {
        str(uid): _email_bytes(f"buyer-{uid}@example.com", [])
        for uid in range(1, 6)
    }

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def login(self, username, password):
            assert username == "orders@example.com"
            assert password == "poll-test-secret"
            return "OK", []

        def select(self, _folder, readonly=True):
            assert readonly is True
            return "OK", []

        def response(self, name):
            assert name == "UIDVALIDITY"
            return "OK", [b"901"]

        def status(self, _folder, _query):
            return "OK", [b"UIDVALIDITY 901"]

        def uid(self, command, *args):
            if command == "search":
                return "OK", [b"1 2 3 4 5"]
            assert command == "fetch"
            raw_uid = args[0]
            uid = raw_uid.decode("ascii")
            if args[1] == "(RFC822.SIZE)":
                return "OK", [(f"{uid} (RFC822.SIZE {len(raw_messages[uid])})".encode(), b"")]
            assert args[1] == "(BODY.PEEK[])"
            fetched_uids.append(uid)
            return "OK", [(b"BODY[]", raw_messages[uid])]

        def logout(self):
            return "BYE", []

    with SessionMaker() as db:
        for uid in (3, 4, 5):
            db.add(
                EmailOrderIntakeMessage(
                    mailbox_key=settings.email_intake_mailbox_key,
                    imap_uidvalidity="901",
                    imap_uid=str(uid),
                    sender_email=f"existing-{uid}@example.com",
                    normalized_sender_email=f"existing-{uid}@example.com",
                    raw_message_sha256=str(uid) * 64,
                    status="ignored",
                )
            )
        db.commit()

        result = service.poll_mailbox_once(
            db, settings=settings, client_factory=FakeClient
        )
        assert result == {
            "examined": 2,
            "created": 2,
            "duplicates": 0,
            "failed": 0,
        }
        assert fetched_uids == ["1", "2"]
        stored_uids = set(
            db.scalars(
                select(EmailOrderIntakeMessage.imap_uid).where(
                    EmailOrderIntakeMessage.mailbox_key
                    == settings.email_intake_mailbox_key
                )
            ).all()
        )
        assert stored_uids == {"1", "2", "3", "4", "5"}
        poll_state = db.get(
            EmailOrderIntakePollState, settings.email_intake_mailbox_key
        )
        assert poll_state.last_status == "success"
        assert poll_state.last_started_at is not None
        assert poll_state.last_finished_at is not None
        assert poll_state.examined_count == 2
        assert poll_state.created_count == 2
        assert poll_state.duplicate_count == 0
        assert poll_state.failed_count == 0
        assert poll_state.last_error is None


def test_poll_rejects_oversized_raw_message_before_body_download(database) -> None:
    SessionMaker, _ids, tmp_path = database
    password_file = tmp_path / "poll-password.txt"
    password_file.write_text("poll-test-secret", encoding="utf-8")
    settings = _settings(
        tmp_path,
        email_intake_enabled=True,
        email_imap_host="imap.example.com",
        email_imap_username="orders@example.com",
        email_imap_password_file=password_file,
        email_intake_max_raw_message_bytes=1024,
    )
    body_fetches: list[str] = []

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def login(self, *_args):
            return "OK", []

        def select(self, *_args, **_kwargs):
            return "OK", []

        def response(self, _name):
            return "OK", [b"902"]

        def status(self, _folder, _query):
            return "OK", [b"UIDVALIDITY 902"]

        def uid(self, command, *args):
            if command == "search":
                return "OK", [b"1"]
            assert command == "fetch"
            assert args[0] == b"1"
            if args[1] == "(RFC822.SIZE)":
                return "OK", [(b"1 (RFC822.SIZE 1025)", b"")]
            body_fetches.append(args[1])
            return "OK", [(b"BODY[]", b"must not be downloaded")]

        def logout(self):
            return "BYE", []

    with SessionMaker() as db:
        result = service.poll_mailbox_once(db, settings=settings, client_factory=FakeClient)
        assert result == {"examined": 1, "created": 0, "duplicates": 0, "failed": 1}
        assert body_fetches == []
        rejected = db.scalar(
            select(EmailOrderIntakeMessage).where(
                EmailOrderIntakeMessage.mailbox_key
                == settings.email_intake_mailbox_key,
                EmailOrderIntakeMessage.imap_uidvalidity == "902",
                EmailOrderIntakeMessage.imap_uid == "1",
            )
        )
        assert rejected is not None
        assert rejected.status == "ignored"
        assert rejected.sender_email == "unknown"
        assert rejected.processed_at is not None
        assert "已拒收且记录 UID" in (rejected.last_error or "")

        repeated = service.poll_mailbox_once(
            db, settings=settings, client_factory=FakeClient
        )
        assert repeated == {
            "examined": 0,
            "created": 0,
            "duplicates": 0,
            "failed": 0,
        }
        assert body_fetches == []
        assert db.scalar(select(func.count(EmailOrderIntakeMessage.id))) == 1


def test_poll_size_failure_does_not_starve_later_valid_message(database) -> None:
    SessionMaker, _ids, tmp_path = database
    password_file = tmp_path / "poll-password.txt"
    password_file.write_text("poll-test-secret", encoding="utf-8")
    settings = _settings(
        tmp_path,
        email_intake_enabled=True,
        email_imap_host="imap.example.com",
        email_imap_username="orders@example.com",
        email_imap_password_file=password_file,
        email_intake_max_messages=1,
        email_intake_max_raw_message_bytes=1024,
    )
    valid_message = _email_bytes("buyer@example.com", [])
    body_fetches: list[str] = []

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def login(self, *_args):
            return "OK", []

        def select(self, *_args, **_kwargs):
            return "OK", []

        def response(self, _name):
            return "OK", [b"902"]

        def status(self, *_args):
            return "OK", [b"UIDVALIDITY 902"]

        def uid(self, command, *args):
            if command == "search":
                return "OK", [b"1 2 3 4 5 6"]
            raw_uid = args[0]
            uid = raw_uid.decode("ascii")
            if args[1] == "(RFC822.SIZE)":
                if uid != "6":
                    return "NO", []
                return "OK", [
                    (f"{uid} (RFC822.SIZE {len(valid_message)})".encode(), b"")
                ]
            body_fetches.append(uid)
            return "OK", [(b"BODY[]", valid_message)]

        def logout(self):
            return "BYE", []

    with SessionMaker() as db:
        result = service.poll_mailbox_once(
            db, settings=settings, client_factory=FakeClient
        )
        assert result == {
            "examined": 6,
            "created": 1,
            "duplicates": 0,
            "failed": 5,
        }
        assert body_fetches == ["6"]


def test_poll_rechecks_actual_downloaded_message_size(database) -> None:
    SessionMaker, _ids, tmp_path = database
    password_file = tmp_path / "poll-password.txt"
    password_file.write_text("poll-test-secret", encoding="utf-8")
    settings = _settings(
        tmp_path,
        email_intake_enabled=True,
        email_imap_host="imap.example.com",
        email_imap_username="orders@example.com",
        email_imap_password_file=password_file,
        email_intake_max_raw_message_bytes=1024,
    )
    body_fetches: list[str] = []

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def login(self, *_args):
            return "OK", []

        def select(self, *_args, **_kwargs):
            return "OK", []

        def response(self, _name):
            return "OK", [b"903"]

        def status(self, *_args):
            return "OK", [b"UIDVALIDITY 903"]

        def uid(self, command, *args):
            if command == "search":
                return "OK", [b"1"]
            if args[1] == "(RFC822.SIZE)":
                return "OK", [(b"1 (RFC822.SIZE 100)", b"")]
            body_fetches.append("1")
            return "OK", [(b"BODY[]", b"x" * 1025)]

        def logout(self):
            return "BYE", []

    with SessionMaker() as db:
        result = service.poll_mailbox_once(
            db, settings=settings, client_factory=FakeClient
        )
        assert result == {
            "examined": 1,
            "created": 0,
            "duplicates": 0,
            "failed": 1,
        }
        assert body_fetches == ["1"]
        rejected = db.scalar(
            select(EmailOrderIntakeMessage).where(
                EmailOrderIntakeMessage.imap_uidvalidity == "903",
                EmailOrderIntakeMessage.imap_uid == "1",
            )
        )
        assert rejected is not None
        assert "实际大小" in (rejected.last_error or "")


def test_poll_sets_imap_socket_timeout_and_total_deadline(database, monkeypatch) -> None:
    SessionMaker, _ids, tmp_path = database
    password_file = tmp_path / "poll-password.txt"
    password_file.write_text("poll-test-secret", encoding="utf-8")
    settings = _settings(
        tmp_path,
        email_intake_enabled=True,
        email_imap_host="imap.example.com",
        email_imap_username="orders@example.com",
        email_imap_password_file=password_file,
        email_imap_timeout_seconds=7,
        email_intake_poll_timeout_seconds=5,
    )
    constructor_kwargs: list[dict] = []
    socket_timeouts: list[int] = []

    class FakeClient:
        def __init__(self, *_args, **kwargs):
            constructor_kwargs.append(kwargs)
            self.sock = SimpleNamespace(
                settimeout=lambda value: socket_timeouts.append(value)
            )

        def logout(self):
            return "BYE", []

    monotonic_values = iter([0.0, 0.0, 6.0])
    monkeypatch.setattr(service.time, "monotonic", lambda: next(monotonic_values))
    with SessionMaker() as db, pytest.raises(
        service.EmailIntakeError, match="超过安全时限"
    ):
        service.poll_mailbox_once(db, settings=settings, client_factory=FakeClient)
    assert constructor_kwargs[0]["timeout"] == 7
    assert socket_timeouts == [7]
    with SessionMaker() as db:
        poll_state = db.get(
            EmailOrderIntakePollState, settings.email_intake_mailbox_key
        )
        assert poll_state.last_status == "failed"
        assert "超过安全时限" in (poll_state.last_error or "")


def test_password_file_secret_never_leaks_from_status_or_imap_error(api_client, monkeypatch: pytest.MonkeyPatch) -> None:
    client, SessionMaker, _ids, tmp_path = api_client
    secret = "NeverReturnThisMailboxPassword!"
    password_file = tmp_path / "imap-password.txt"
    password_file.write_text(secret, encoding="utf-8")
    settings = _settings(
        tmp_path,
        email_intake_enabled=True,
        email_imap_host="imap.example.com",
        email_imap_username="orders@example.com",
        email_imap_password_file=password_file,
    )
    monkeypatch.setattr("app.api.email_intake.load_settings", lambda: settings)
    _login(client, "n043-admin")
    response = client.get("/api/email-order-intake/status")
    assert response.status_code == 200
    assert response.json()["configured"] is True
    assert response.json()["masked_email"] == "o***@example.com"
    assert secret not in response.text
    assert str(password_file) not in response.text

    class FailingClient:
        def __init__(self, *_args, **_kwargs):
            pass
        def login(self, _username, password):
            raise imaplib.IMAP4.error(f"bad password: {password}")
        def logout(self):
            return "BYE", []

    with SessionMaker() as db, pytest.raises(service.EmailIntakeError) as captured:
        service.poll_mailbox_once(db, settings=settings, client_factory=FailingClient)
    assert secret not in str(captured.value)
    assert str(password_file) not in str(captured.value)
    with SessionMaker() as db:
        poll_state = db.get(
            EmailOrderIntakePollState, settings.email_intake_mailbox_key
        )
        assert poll_state.last_status == "failed"
        assert poll_state.last_finished_at is not None
        assert poll_state.last_error == "邮箱连接或认证失败"
        assert secret not in (poll_state.last_error or "")
        assert str(password_file) not in (poll_state.last_error or "")

    status_response = client.get("/api/email-order-intake/status")
    assert status_response.status_code == 200
    assert status_response.json()["last_poll_status"] == "failed"
    assert status_response.json()["last_poll_message"] == "邮箱连接或认证失败"
    assert secret not in status_response.text
    assert str(password_file) not in status_response.text


def test_storage_path_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(service.EmailIntakeError, match="路径"):
        service.resolve_stored_attachment_path("../../outside.pdf", _settings(tmp_path))
