from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from pathlib import Path
from threading import Barrier

from fastapi import HTTPException
import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from tests.test_phase11_requisition import requisition_app


PDF_A = b"%PDF-1.7\nsupplier-a\n%%EOF\n"
PDF_B = b"%PDF-1.7\nsupplier-b\n%%EOF\n"
PDF_SHARED = b"%PDF-1.7\nsupplier-shared\n%%EOF\n"


class AsyncUpload:
    def __init__(self, filename: str, content: bytes) -> None:
        self.filename = filename
        self._content = content
        self.read_count = 0

    async def read(self) -> bytes:
        self.read_count += 1
        return self._content


def _seed_statement_invoices(session_factory) -> dict[str, int]:
    from app.models.supplier import Supplier
    from app.models.supplier_settlement import (
        SupplierMonthlyInvoice,
        SupplierMonthlyStatement,
    )
    from app.models.user import User

    with session_factory() as db:
        supplier = db.scalar(select(Supplier).order_by(Supplier.id))
        admin = db.scalar(select(User).where(User.username == "admin"))
        sales = db.scalar(select(User).where(User.username == "sales"))
        assert supplier is not None and admin is not None and sales is not None
        statement = SupplierMonthlyStatement(
            statement_number="OPT10-02-202609",
            supplier_id=supplier.id,
            supplier_name_snapshot=supplier.standard_name,
            settlement_month="2026-09",
            period_start=date(2026, 8, 21),
            period_end=date(2026, 9, 20),
            currency="CNY",
            tax_basis="tax_inclusive",
            status="confirmed_pending_invoice",
            erp_amount=Decimal("3.00"),
            adjustment_amount=Decimal("0.00"),
            adjusted_amount=Decimal("3.00"),
            invoice_allocated_amount=Decimal("3.00"),
            paid_amount=Decimal("0.00"),
            document_revision=1,
            settlement_day_snapshot=20,
            generation_origin="manual",
            version=1,
        )
        db.add(statement)
        db.flush()
        invoices = [
            SupplierMonthlyInvoice(
                statement_id=statement.id,
                supplier_id=supplier.id,
                invoice_number=f"OPT10-02-{number}",
                invoice_date=date(2026, 9, 20),
                received_date=date(2026, 9, 20),
                invoice_total_amount=Decimal("1.00"),
                allocated_amount=Decimal("1.00"),
                tax_amount=Decimal("0.13"),
            )
            for number in ("A", "B", "C")
        ]
        db.add_all(invoices)
        db.commit()
        return {
            "statement_id": statement.id,
            "invoice_a": invoices[0].id,
            "invoice_b": invoices[1].id,
            "invoice_c": invoices[2].id,
            "admin_id": admin.id,
            "sales_id": sales.id,
        }


@pytest.fixture()
def attachment_context(requisition_app, monkeypatch, tmp_path: Path):
    from app.api import supplier_settlements as api

    _app, session_factory = requisition_app
    root = tmp_path / "supplier-invoice-attachments"
    monkeypatch.setenv("ERP_SUPPLIER_INVOICE_ATTACHMENT_DIR", str(root))
    monkeypatch.setattr(api, "_audit", lambda *args, **kwargs: None)
    return api, session_factory, _seed_statement_invoices(session_factory), root


def _user(db, user_id: int):
    from app.models.user import User

    user = db.get(User, user_id)
    assert user is not None
    return user


def _upload(api, db, ids: dict[str, int], invoice_id: int, content: bytes, name: str):
    return asyncio.run(
        api.upload_supplier_invoice_attachment(
            ids["statement_id"],
            invoice_id,
            AsyncUpload(name, content),
            db,
            _user(db, ids["admin_id"]),
        )
    )


def _originals(root: Path) -> list[Path]:
    return sorted((root / "originals").glob("*.pdf"))


def test_different_original_conflict_does_not_create_file(attachment_context) -> None:
    api, session_factory, ids, root = attachment_context
    with session_factory() as db:
        first = _upload(api, db, ids, ids["invoice_a"], PDF_A, "a.pdf")
    assert first["reused"] is False
    before = _originals(root)
    with session_factory() as db:
        with pytest.raises(HTTPException) as error:
            _upload(api, db, ids, ids["invoice_a"], PDF_B, "b.pdf")
    assert error.value.status_code == 409
    assert _originals(root) == before


def test_same_original_replay_is_idempotent(attachment_context) -> None:
    api, session_factory, ids, root = attachment_context
    with session_factory() as db:
        first = _upload(api, db, ids, ids["invoice_a"], PDF_A, "a.pdf")
    with session_factory() as db:
        replay = _upload(api, db, ids, ids["invoice_a"], PDF_A, "retry.pdf")
    assert first["content_hash"] == replay["content_hash"]
    assert replay["reused"] is True
    assert len(_originals(root)) == 1


def test_concurrent_different_originals_have_one_winner_and_no_loser_file(
    attachment_context, monkeypatch
) -> None:
    api, session_factory, ids, root = attachment_context
    original_store = api.store_original_invoice_pdf
    barrier = Barrier(2)

    def synchronized_store(**kwargs):
        stored = original_store(**kwargs)
        barrier.wait(timeout=10)
        return stored

    monkeypatch.setattr(api, "store_original_invoice_pdf", synchronized_store)

    def submit(content: bytes, name: str):
        with session_factory() as db:
            try:
                result = _upload(api, db, ids, ids["invoice_a"], content, name)
                return ("ok", result["content_hash"])
            except HTTPException as error:
                return ("http", error.status_code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda item: submit(*item),
                ((PDF_A, "a.pdf"), (PDF_B, "b.pdf")),
            )
        )

    assert sorted(kind for kind, _value in results) == ["http", "ok"]
    assert {value for kind, value in results if kind == "http"} == {409}
    assert len(_originals(root)) == 1


def test_commit_response_failure_verifies_durable_audit_and_returns_success(
    attachment_context, monkeypatch
) -> None:
    api, session_factory, ids, root = attachment_context
    audit_calls: list[dict] = []
    monkeypatch.setattr(api, "_audit", lambda *args, **kwargs: audit_calls.append(kwargs))

    with session_factory() as db:
        commit = db.commit

        def commit_then_fail():
            commit()
            raise SQLAlchemyError("simulated response failure after durable commit")

        monkeypatch.setattr(db, "commit", commit_then_fail)
        result = _upload(api, db, ids, ids["invoice_a"], PDF_A, "a.pdf")

    assert result["reused"] is False
    assert audit_calls
    with session_factory() as verify:
        from app.models.supplier_settlement import SupplierMonthlyInvoice

        invoice = verify.get(SupplierMonthlyInvoice, ids["invoice_a"])
        assert invoice is not None and invoice.attachment_content_hash == result["content_hash"]
    assert len(_originals(root)) == 1


def test_undurable_commit_failure_keeps_shared_content_file(attachment_context, monkeypatch) -> None:
    api, session_factory, ids, root = attachment_context
    with session_factory() as db:
        shared = _upload(api, db, ids, ids["invoice_a"], PDF_SHARED, "shared.pdf")

    with session_factory() as db:
        monkeypatch.setattr(db, "commit", lambda: (_ for _ in ()).throw(SQLAlchemyError("simulated commit failure")))
        with pytest.raises(HTTPException) as error:
            _upload(api, db, ids, ids["invoice_b"], PDF_SHARED, "shared-retry.pdf")
        assert error.value.status_code == 503
        db.rollback()

    assert (root / "originals" / f"{shared['content_hash']}.pdf").is_file()
    with session_factory() as verify:
        from app.models.supplier_settlement import SupplierMonthlyInvoice

        second = verify.get(SupplierMonthlyInvoice, ids["invoice_b"])
        assert second is not None and second.attachment_content_hash is None


def test_scope_and_statement_mismatch_reject_before_read(attachment_context) -> None:
    api, session_factory, ids, _root = attachment_context
    with session_factory() as db:
        with pytest.raises(HTTPException) as scope_error:
            api._company_scope(_user(db, ids["sales_id"]), db)
        assert scope_error.value.status_code == 403

        upload = AsyncUpload("scope.pdf", PDF_A)
        with pytest.raises(HTTPException) as statement_error:
            asyncio.run(
                api.upload_supplier_invoice_attachment(
                    ids["statement_id"] + 999,
                    ids["invoice_c"],
                    upload,
                    db,
                    _user(db, ids["admin_id"]),
                )
            )
        assert statement_error.value.status_code == 404
        assert upload.read_count == 0