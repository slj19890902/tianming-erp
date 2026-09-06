from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from test_p1_33c3_external_packaging_purchase_confirmation import purchase_app
from test_p1_33c5_external_packaging_receiving import (
    _confirm as confirm_external_purchase,
)
from test_p1_33c5_external_packaging_receiving import _login as login_external
from test_p1_33c5_external_packaging_receiving import _pending as pending_external
from test_p1_33c5_external_packaging_receiving import _root_line
from tests.test_p1_81_receipt_purpose_flow import (
    _create_frozen_sources,
    _freeze_receipt_fact,
    _receive,
    _seed_material_and_staging,
    _use_p181_published_map_identity,
)
from tests.test_phase11_requisition import _login, requisition_app


def _include_supplier_router(app) -> None:
    from app.api.supplier_settlements import router

    app.include_router(router, prefix="/api/finance")


def _error_code(response) -> str:
    detail = response.json()["detail"]
    assert isinstance(detail, dict), response.text
    return str(detail["code"])


def _paperboard_receipt(client: TestClient, session_factory) -> int:
    _seed_material_and_staging(session_factory)
    source = _create_frozen_sources(
        client,
        session_factory,
        order_quantity=10,
        purchase_total=10,
        order_purpose=10,
        stock_purpose=0,
    )[0]
    frozen = _freeze_receipt_fact(
        client,
        source,
        idempotency_key="p132-paperboard-price",
        unit_price="2.5000",
        tax_included=True,
        tax_rate="0.13",
    )
    assert frozen.status_code == 200, frozen.text
    # Freeze the clock before receipt and price are created together. Rewriting
    # only received_at afterwards would move quantity without its immutable price.
    with patch("app.services.incoming_receipts.utc_now_naive", return_value=datetime(2026, 8, 15, 3, 0, 0)):
        received = _receive(
            client,
            source,
            frozen.json(),
            quantity=10,
            idempotency_key="p132-paperboard-receipt",
        )
    assert received.status_code == 200, received.text
    receipt_item_id = int(received.json()["receipt_item_id"])
    return receipt_item_id


def test_paperboard_statement_review_adjust_confirm_invoice_and_payment(
    requisition_app,
    monkeypatch,
    tmp_path,
) -> None:
    from app.models.finance_payable import FinancePayable
    from app.models.supplier_settlement import SupplierMonthlyStatementLine

    app, session_factory = requisition_app
    _use_p181_published_map_identity(monkeypatch)
    monkeypatch.setenv(
        "ERP_SUPPLIER_INVOICE_ATTACHMENT_DIR",
        str(tmp_path / "supplier-invoice-attachments"),
    )
    _include_supplier_router(app)
    with TestClient(app) as client:
        _login(client, "admin")
        receipt_item_id = _paperboard_receipt(client, session_factory)

        payload = {
            "settlement_month": "2026-08",
            "idempotency_key": "p132-generate-august",
        }
        generated = client.post(
            "/api/finance/supplier-settlements/generate", json=payload
        )
        replay = client.post(
            "/api/finance/supplier-settlements/generate", json=payload
        )
        assert generated.status_code == replay.status_code == 200
        assert generated.json() == replay.json()
        body = generated.json()
        assert body["period_start"] == "2026-07-21"
        assert body["period_end"] == "2026-08-20"
        assert body["added_line_count"] == 1
        statement = body["items"][0]
        assert statement["erp_amount"] == "25.00"
        assert statement["lines"][0]["quantity_unit"] == "张"
        assert statement["lines"][0]["specification_snapshot"] == "800×200mm"

        # A new key refreshes from the same immutable receipt facts without
        # creating duplicate active lines.
        refreshed = client.post(
            "/api/finance/supplier-settlements/generate",
            json={
                "settlement_month": "2026-08",
                "idempotency_key": "p132-refresh-august",
            },
        )
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["added_line_count"] == 0

        reviewed = client.post(
            f"/api/finance/supplier-settlements/{statement['id']}/review",
            json={
                "expected_version": statement["version"],
                "supplier_statement_number": "SUP-ST-202608",
                "supplier_statement_date": "2026-08-25",
                "supplier_statement_amount": "26.00",
                "idempotency_key": "p132-review-difference",
            },
        )
        assert reviewed.status_code == 200, reviewed.text
        statement = reviewed.json()
        assert statement["status"] == "difference"
        assert statement["difference_amount"] == "1.00"

        adjusted = client.post(
            f"/api/finance/supplier-settlements/{statement['id']}/adjustments",
            json={
                "expected_version": statement["version"],
                "statement_line_id": statement["lines"][0]["id"],
                "difference_type": "price",
                "amount": "1.00",
                "note": "供应商账单单价差异",
                "idempotency_key": "p132-adjust-one-yuan",
            },
        )
        assert adjusted.status_code == 201, adjusted.text
        statement = adjusted.json()
        assert statement["adjusted_amount"] == "26.00"
        assert statement["status"] == "draft"

        confirmed = client.post(
            f"/api/finance/supplier-settlements/{statement['id']}/confirm",
            json={
                "expected_version": statement["version"],
                "idempotency_key": "p132-confirm-august",
            },
        )
        assert confirmed.status_code == 200, confirmed.text
        statement = confirmed.json()
        assert statement["status"] == "confirmed_pending_invoice"

        blocked = client.put(
            f"/api/incoming/receipt-items/{receipt_item_id}/revert",
            json={
                "reason": "月结确认后的错误撤销测试",
                "idempotency_key": "p132-block-reversal",
            },
        )
        assert blocked.status_code == 409, blocked.text
        assert _error_code(blocked) == "SUPPLIER_SETTLEMENT_RECEIPT_FROZEN"

        invoiced = client.post(
            f"/api/finance/supplier-settlements/{statement['id']}/invoices",
            json={
                "expected_version": statement["version"],
                "invoice_number": "INV-202608-001",
                "invoice_date": "2026-08-28",
                "received_date": "2026-08-30",
                "invoice_total_amount": "10.00",
                "allocated_amount": "10.00",
                "tax_amount": "1.15",
                "note": "本期第一张发票",
                "idempotency_key": "p132-invoice-august",
            },
        )
        assert invoiced.status_code == 201, invoiced.text
        statement = invoiced.json()
        assert statement["status"] == "invoiced_pending_payment"
        invoice_id = int(statement["created_invoice_id"])
        second_invoice = client.post(
            f"/api/finance/supplier-settlements/{statement['id']}/invoices",
            json={
                "expected_version": statement["version"],
                "invoice_number": "INV-202608-002",
                "invoice_date": "2026-08-29",
                "received_date": "2026-08-31",
                "invoice_total_amount": "16.00",
                "allocated_amount": "16.00",
                "tax_amount": "1.84",
                "note": "本期第二张发票",
                "idempotency_key": "p132-invoice-august-second",
            },
        )
        assert second_invoice.status_code == 201, second_invoice.text
        statement = second_invoice.json()
        assert statement["invoice_allocated_amount"] == "26.00"
        assert len(statement["invoices"]) == 2
        pdf = b"%PDF-1.7\nP1-132 supplier invoice\n%%EOF\n"
        uploaded = client.post(
            f"/api/finance/supplier-settlements/{statement['id']}/invoices/"
            f"{invoice_id}/attachment",
            files={"file": ("supplier-invoice.pdf", pdf, "application/pdf")},
        )
        assert uploaded.status_code == 201, uploaded.text
        assert uploaded.json()["reused"] is False
        replayed_upload = client.post(
            f"/api/finance/supplier-settlements/{statement['id']}/invoices/"
            f"{invoice_id}/attachment",
            files={"file": ("supplier-invoice.pdf", pdf, "application/pdf")},
        )
        assert replayed_upload.status_code == 201, replayed_upload.text
        assert replayed_upload.json()["reused"] is True
        downloaded = client.get(
            f"/api/finance/supplier-settlements/{statement['id']}/invoices/"
            f"{invoice_id}/attachment"
        )
        assert downloaded.status_code == 200
        assert downloaded.content == pdf

        paid = client.post(
            f"/api/finance/supplier-settlements/{statement['id']}/payments",
            json={
                "expected_version": statement["version"],
                "payment_date": "2026-09-01",
                "amount": "10.00",
                "reference": "BANK-001",
                "idempotency_key": "p132-payment-first",
            },
        )
        assert paid.status_code == 201, paid.text
        statement = paid.json()
        assert statement["status"] == "partial_payment"
        assert statement["paid_amount"] == "10.00"
        assert statement["remaining_payable_amount"] == "16.00"

        overpaid = client.post(
            f"/api/finance/supplier-settlements/{statement['id']}/payments",
            json={
                "expected_version": statement["version"],
                "payment_date": "2026-09-02",
                "amount": "17.00",
                "reference": "BANK-OVER",
                "idempotency_key": "p132-payment-over",
            },
        )
        assert overpaid.status_code == 422, overpaid.text
        assert _error_code(overpaid) == "SUPPLIER_PAYMENT_EXCEEDS_PAYABLE"

        settled = client.post(
            f"/api/finance/supplier-settlements/{statement['id']}/payments",
            json={
                "expected_version": statement["version"],
                "payment_date": "2026-09-02",
                "amount": "16.00",
                "reference": "BANK-002",
                "idempotency_key": "p132-payment-final",
            },
        )
        assert settled.status_code == 201, settled.text
        assert settled.json()["status"] == "paid"

    with session_factory() as db:
        assert (
            db.scalar(
                select(func.count(SupplierMonthlyStatementLine.id)).where(
                    SupplierMonthlyStatementLine.active_guard == 1
                )
            )
            == 1
        )
        payable = db.scalar(select(FinancePayable))
        assert payable is not None
        assert payable.document_date == date(2026, 8, 20)
        assert payable.status == "paid"


def test_external_packaging_keeps_frozen_native_unit_and_tax_contract(
    purchase_app,
) -> None:
    from app.models.external_packaging_purchase import ExternalPackagingReceipt

    _include_supplier_router(purchase_app)
    order_id = purchase_app.state.fixture["order_id"]
    with TestClient(purchase_app) as client:
        login_external(client, "purchase-admin")
        confirm_external_purchase(client, order_id)
        purchase, line = _root_line(pending_external(client))
        received = client.post(
            f"/api/external-packaging-purchases/{purchase['id']}/receipts",
            json={
                "idempotency_key": "p132-external-receipt",
                "lines": [
                    {
                        "purchase_item_id": line["purchase_item_id"],
                        "received_quantity": "40",
                    }
                ],
            },
        )
        assert received.status_code == 200, received.text
        with purchase_app.state.session_factory() as db:
            receipt = db.scalar(select(ExternalPackagingReceipt))
            assert receipt is not None
            receipt.received_at = datetime(2026, 8, 15, 3, 0, 0)
            db.commit()

        generated = client.post(
            "/api/finance/supplier-settlements/generate",
            json={
                "settlement_month": "2026-08",
                "idempotency_key": "p132-external-generate",
            },
        )
        assert generated.status_code == 200, generated.text
        rows = [
            detail
            for statement in generated.json()["items"]
            for detail in statement["lines"]
        ]
        assert len(rows) == 1
        assert rows[0]["source_type"] == "external_packaging"
        assert rows[0]["quantity_unit"] == "根"
        assert rows[0]["tax_basis"] == "tax_inclusive"
        assert rows[0]["received_quantity"] == "40.000000"


def test_supplier_settlement_requires_company_wide_finance_and_cost_scope(
    requisition_app,
) -> None:
    app, _session_factory = requisition_app
    _include_supplier_router(app)
    with TestClient(app) as client:
        _login(client, "workshop")
        response = client.get(
            "/api/finance/supplier-settlements?settlement_month=2026-08"
        )
        assert response.status_code == 403


def test_period_contract_waits_until_the_twentieth_is_fully_closed() -> None:
    from app.services.supplier_monthly_settlement import (
        default_closed_settlement_month,
        settlement_period,
    )

    assert settlement_period("2026-08") == (
        date(2026, 7, 21),
        date(2026, 8, 20),
    )
    assert default_closed_settlement_month(date(2026, 9, 20)) == "2026-08"
    assert default_closed_settlement_month(date(2026, 9, 21)) == "2026-09"


def test_one_supplier_invoice_can_be_manually_allocated_across_periods(
    requisition_app,
) -> None:
    from app.models.supplier import Supplier
    from app.models.supplier_settlement import SupplierMonthlyStatement
    from app.models.user import User
    from app.services.supplier_monthly_settlement import (
        SupplierSettlementError,
        add_invoice,
    )

    _app, session_factory = requisition_app
    with session_factory() as db:
        supplier = db.scalar(
            select(Supplier).where(Supplier.standard_name == "苏州纸板供应商")
        )
        user = db.scalar(select(User).where(User.username == "admin"))
        assert supplier is not None and user is not None
        statements = [
            SupplierMonthlyStatement(
                statement_number=f"AP-CROSS-{index}",
                supplier_id=supplier.id,
                supplier_name_snapshot=supplier.display_name or supplier.standard_name,
                settlement_month=settlement_month,
                period_start=period_start,
                period_end=period_end,
                currency="CNY",
                tax_basis="tax_inclusive",
                status="confirmed_pending_invoice",
                erp_amount=amount,
                adjusted_amount=amount,
                confirmed_amount=amount,
                generated_by=user.id,
            )
            for index, (settlement_month, period_start, period_end, amount) in enumerate(
                (
                    ("2026-07", date(2026, 6, 21), date(2026, 7, 20), Decimal("10")),
                    ("2026-08", date(2026, 7, 21), date(2026, 8, 20), Decimal("20")),
                    ("2026-09", date(2026, 8, 21), date(2026, 9, 20), Decimal("1")),
                ),
                start=1,
            )
        ]
        db.add_all(statements)
        db.flush()
        first, _invoice = add_invoice(
            db,
            statement_id=statements[0].id,
            expected_version=1,
            invoice_number="INV-CROSS-001",
            invoice_date=date(2026, 8, 25),
            received_date=date(2026, 8, 26),
            invoice_total_amount=Decimal("30"),
            allocated_amount=Decimal("10"),
            tax_amount=Decimal("1.15"),
            note=None,
            user=user,
        )
        second, _invoice = add_invoice(
            db,
            statement_id=statements[1].id,
            expected_version=1,
            invoice_number="INV-CROSS-001",
            invoice_date=date(2026, 8, 25),
            received_date=date(2026, 8, 26),
            invoice_total_amount=Decimal("30"),
            allocated_amount=Decimal("20"),
            tax_amount=Decimal("2.30"),
            note=None,
            user=user,
        )
        assert first.invoice_allocated_amount == Decimal("10.00")
        assert second.invoice_allocated_amount == Decimal("20.00")
        with pytest.raises(SupplierSettlementError) as error:
            add_invoice(
                db,
                statement_id=statements[2].id,
                expected_version=1,
                invoice_number="INV-CROSS-001",
                invoice_date=date(2026, 8, 25),
                received_date=date(2026, 8, 26),
                invoice_total_amount=Decimal("30"),
                allocated_amount=Decimal("1"),
                tax_amount=Decimal("0.10"),
                note=None,
                user=user,
            )
        assert error.value.code == "SUPPLIER_INVOICE_OVER_ALLOCATED"
