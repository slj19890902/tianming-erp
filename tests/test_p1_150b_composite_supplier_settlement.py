from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from tests.test_p1_132_supplier_monthly_settlement import _include_supplier_router
from tests.test_p1_150b_composite_physical_group_planning import (
    _group_payload_from_pending,
    _login,
    physical_group_app as _planning_group_app,
)
from tests.test_p1_150b_composite_physical_group_receipts import (
    _convert_group_sources_to_supplier_items,
    _freeze_direct_group_receipt_fact,
    _group_receive_payload,
    _post_two_source_group_receipt,
    receipt_group_app,
)


def _place_receipt_in_august(factory, receipt_item_id: int) -> None:
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem

    with factory() as db:
        item = db.get(IncomingReceiptItem, receipt_item_id)
        assert item is not None
        receipt = db.get(IncomingReceipt, item.receipt_id)
        assert receipt is not None
        receipt.received_at = datetime(2026, 8, 15, 3, 0, 0)
        db.commit()


def _scan_august(factory):
    from app.services.supplier_monthly_settlement import scan_settlement_candidates

    with factory() as db:
        return scan_settlement_candidates(db, settlement_month="2026-08")


def _error_code(response) -> str | None:
    detail = response.json().get("detail")
    return str(detail.get("code")) if isinstance(detail, dict) else None


def test_group_receipt_enters_supplier_statement_once_and_confirmation_blocks_reversal(
    receipt_group_app,
) -> None:
    from app.models.supplier_settlement import (
        SupplierMonthlyStatementLine,
        SupplierReceiptSettlementPriceFact,
    )
    from app.services.supplier_monthly_settlement import settlement_period_utc_bounds
    from app.services.supplier_receipt_price_facts import (
        preview_historical_price_adoptions,
    )

    app, factory = receipt_group_app
    _include_supplier_router(app)
    with TestClient(app) as client:
        _login(client)
        _group, _receipt_payload, received = _post_two_source_group_receipt(
            client,
            factory,
            key_prefix="p1-150b-supplier-settlement",
        )
        receipt_item_id = int(received["receipt_item_id"])
        _place_receipt_in_august(factory, receipt_item_id)

        _start, _end, start_utc, end_utc = settlement_period_utc_bounds("2026-08")
        with factory() as db:
            preview = preview_historical_price_adoptions(
                db,
                settlement_month="2026-08",
                start_utc=start_utc,
                end_utc=end_utc,
            )
            assert receipt_item_id not in {
                int(row["incoming_receipt_item_id"])
                for row in [*preview["eligible"], *preview["rejected"]]
            }
            assert db.scalar(
                select(func.count(SupplierReceiptSettlementPriceFact.id))
            ) == 0

        candidates, issues, _period_start, _period_end = _scan_august(factory)
        assert issues == []
        assert len(candidates) == 1
        assert candidates[0].incoming_receipt_item_id == receipt_item_id
        assert candidates[0].received_quantity == Decimal("1")
        assert candidates[0].frozen_unit_price == Decimal("2.500000")
        assert candidates[0].erp_amount == Decimal("2.50")

        generation_payload = {
            "settlement_month": "2026-08",
            "idempotency_key": "p1-150b-generate-group-statement",
        }
        generated = client.post(
            "/api/finance/supplier-settlements/generate",
            json=generation_payload,
        )
        replayed = client.post(
            "/api/finance/supplier-settlements/generate",
            json=generation_payload,
        )
        assert generated.status_code == replayed.status_code == 200
        assert generated.json() == replayed.json()
        assert generated.json()["added_line_count"] == 1
        statement = generated.json()["items"][0]
        assert statement["erp_amount"] == "2.50"
        assert len(statement["lines"]) == 1
        assert statement["lines"][0]["source_key"] == f"paperboard:{receipt_item_id}"

        refreshed = client.post(
            "/api/finance/supplier-settlements/generate",
            json={
                "settlement_month": "2026-08",
                "idempotency_key": "p1-150b-refresh-group-statement",
            },
        )
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["added_line_count"] == 0

        reviewed = client.post(
            f"/api/finance/supplier-settlements/{statement['id']}/review",
            json={
                "expected_version": statement["version"],
                "supplier_statement_number": "P1-150B-SUPPLIER-202608",
                "supplier_statement_date": "2026-08-25",
                "supplier_statement_amount": "2.50",
                "idempotency_key": "p1-150b-review-group-statement",
            },
        )
        assert reviewed.status_code == 200, reviewed.text
        statement = reviewed.json()
        confirmed = client.post(
            f"/api/finance/supplier-settlements/{statement['id']}/confirm",
            json={
                "expected_version": statement["version"],
                "idempotency_key": "p1-150b-confirm-group-statement",
            },
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["status"] == "confirmed_pending_invoice"

        blocked = client.put(
            f"/api/incoming/receipt-items/{receipt_item_id}/revert",
            json={
                "reason": "供应商月结确认后撤销保护",
                "idempotency_key": "p1-150b-block-group-reversal",
            },
        )
        assert blocked.status_code == 409, blocked.text
        assert _error_code(blocked) == "SUPPLIER_SETTLEMENT_RECEIPT_FROZEN"

    with factory() as db:
        lines = db.scalars(select(SupplierMonthlyStatementLine)).all()
        assert len(lines) == 1
        assert lines[0].source_key == f"paperboard:{receipt_item_id}"


def test_unconfirmed_group_reversal_voids_empty_draft_idempotently(
    receipt_group_app,
) -> None:
    from app.models.finance_payable import FinancePayable
    from app.models.supplier_settlement import (
        SupplierMonthlyStatement,
        SupplierMonthlyStatementLine,
    )

    app, factory = receipt_group_app
    _include_supplier_router(app)
    with TestClient(app) as client:
        _login(client)
        _group, _receipt_payload, received = _post_two_source_group_receipt(
            client,
            factory,
            key_prefix="p1-150b-unconfirmed-group-reversal",
        )
        receipt_item_id = int(received["receipt_item_id"])
        _place_receipt_in_august(factory, receipt_item_id)

        first, first_issues, _start, _end = _scan_august(factory)
        second, second_issues, _start, _end = _scan_august(factory)
        assert first_issues == second_issues == []
        assert [row.source_key for row in first] == [f"paperboard:{receipt_item_id}"]
        assert [row.source_key for row in second] == [f"paperboard:{receipt_item_id}"]

        generated = client.post(
            "/api/finance/supplier-settlements/generate",
            json={
                "settlement_month": "2026-08",
                "idempotency_key": "p1-150b-generate-before-group-reversal",
            },
        )
        assert generated.status_code == 200, generated.text
        statement = generated.json()["items"][0]

        reversed_response = client.put(
            f"/api/incoming/receipt-items/{receipt_item_id}/revert",
            json={
                "reason": "未确认供应商月结前撤销",
                "idempotency_key": "p1-150b-reverse-before-confirm",
            },
        )
        assert reversed_response.status_code == 200, reversed_response.text

        after, after_issues, _start, _end = _scan_august(factory)
        assert after == []
        assert after_issues == []

        stale_review = client.post(
            f"/api/finance/supplier-settlements/{statement['id']}/review",
            json={
                "expected_version": statement["version"],
                "supplier_statement_number": "P1-150B-STALE-DRAFT",
                "supplier_statement_date": "2026-08-25",
                "supplier_statement_amount": "2.50",
                "idempotency_key": "p1-150b-stale-draft-review",
            },
        )
        assert stale_review.status_code == 409, stale_review.text
        assert _error_code(stale_review) == "SUPPLIER_SETTLEMENT_SOURCE_STALE"

        refresh_preview = client.post(
            "/api/finance/supplier-settlements/generate",
            json={
                "settlement_month": "2026-08",
                "idempotency_key": "p1-150b-preview-empty-group-draft",
            },
        )
        assert refresh_preview.status_code == 200, refresh_preview.text
        assert refresh_preview.json()["released_line_count"] == 0
        assert refresh_preview.json()["changed_statement_count"] == 0
        assert refresh_preview.json()["items"][0]["id"] == statement["id"]
        assert {
            issue["code"] for issue in refresh_preview.json()["issues"]
        } == {"SUPPLIER_SETTLEMENT_REGENERATION_REQUIRED"}

        regenerate_payload = {
            "expected_version": statement["version"],
            "reason": "周期内有效实收已全部撤销",
            "idempotency_key": "p1-150b-void-empty-group-draft",
        }
        regenerated = client.post(
            f"/api/finance/supplier-settlements/{statement['id']}/regenerate",
            json=regenerate_payload,
        )
        replayed_regeneration = client.post(
            f"/api/finance/supplier-settlements/{statement['id']}/regenerate",
            json=regenerate_payload,
        )
        assert regenerated.status_code == replayed_regeneration.status_code == 200
        assert regenerated.json() == replayed_regeneration.json()
        assert regenerated.json()["status"] == "voided"
        assert regenerated.json()["active"] is False
        assert regenerated.json()["can_regenerate"] is False

        refreshed = client.post(
            "/api/finance/supplier-settlements/generate",
            json={
                "settlement_month": "2026-08",
                "idempotency_key": "p1-150b-refresh-after-empty-draft-void",
            },
        )
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["released_line_count"] == 0
        assert refreshed.json()["changed_statement_count"] == 0
        assert refreshed.json()["items"] == []

    with factory() as db:
        stored = db.get(SupplierMonthlyStatement, int(statement["id"]))
        assert stored is not None
        assert stored.status == "voided"
        assert stored.active_guard is None
        line = db.scalar(
            select(SupplierMonthlyStatementLine).where(
                SupplierMonthlyStatementLine.statement_id == stored.id
            )
        )
        assert line is not None
        assert line.active_guard is None
        assert db.scalar(select(func.count(FinancePayable.id))) == 0


def test_valid_group_receipt_scan_failure_never_voids_draft(
    receipt_group_app,
) -> None:
    from app.models.composite_purchase_group import CompositePhysicalGroupReceipt
    from app.models.finance_payable import FinancePayable
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.models.purchase_receipt import PurchaseReceiptFact
    from app.models.supplier_settlement import (
        SupplierMonthlyStatement,
        SupplierMonthlyStatementLine,
    )
    from app.models.user import User
    from app.services.supplier_monthly_settlement import (
        SupplierSettlementError,
        generate_or_refresh_settlements,
    )

    app, factory = receipt_group_app
    _include_supplier_router(app)
    with TestClient(app) as client:
        _login(client)
        _group, _receipt_payload, received = _post_two_source_group_receipt(
            client,
            factory,
            key_prefix="p1-150b-invalid-group-price-fact",
        )
        receipt_item_id = int(received["receipt_item_id"])
        _place_receipt_in_august(factory, receipt_item_id)
        generated = client.post(
            "/api/finance/supplier-settlements/generate",
            json={
                "settlement_month": "2026-08",
                "idempotency_key": "p1-150b-generate-before-price-fact-mismatch",
            },
        )
        assert generated.status_code == 200, generated.text
        statement = generated.json()["items"][0]
        line_id = int(statement["lines"][0]["id"])

        with factory() as db:
            receipt_item = db.get(IncomingReceiptItem, receipt_item_id)
            assert receipt_item is not None
            receipt = db.get(IncomingReceipt, receipt_item.receipt_id)
            group_receipt = db.scalar(
                select(CompositePhysicalGroupReceipt).where(
                    CompositePhysicalGroupReceipt.incoming_receipt_item_id
                    == receipt_item_id
                )
            )
            assert receipt is not None and receipt.status == "posted"
            assert receipt_item.status == "posted"
            assert group_receipt is not None and group_receipt.status == "posted"
            purchase_fact = db.get(
                PurchaseReceiptFact, group_receipt.purchase_receipt_fact_id
            )
            assert purchase_fact is not None
            purchase_fact.unit_price = Decimal(purchase_fact.unit_price) + Decimal(
                "1.00"
            )
            db.commit()

        candidates, issues, _start, _end = _scan_august(factory)
        assert candidates == []
        assert {issue["code"] for issue in issues} == {
            "PAPERBOARD_GROUP_RECEIPT_PRICE_FACT_MISMATCH"
        }

        refresh_preview = client.post(
            "/api/finance/supplier-settlements/generate",
            json={
                "settlement_month": "2026-08",
                "idempotency_key": "p1-150b-preview-invalid-group-price-fact",
            },
        )
        assert refresh_preview.status_code == 200, refresh_preview.text
        preview_codes = {
            issue["code"] for issue in refresh_preview.json()["issues"]
        }
        assert "PAPERBOARD_GROUP_RECEIPT_PRICE_FACT_MISMATCH" in preview_codes
        assert "SUPPLIER_SETTLEMENT_SOURCE_SCAN_FAILED" in preview_codes
        assert "SUPPLIER_SETTLEMENT_REGENERATION_REQUIRED" not in preview_codes
        assert refresh_preview.json()["changed_statement_count"] == 0
        assert refresh_preview.json()["released_line_count"] == 0

        blocked = client.post(
            f"/api/finance/supplier-settlements/{statement['id']}/regenerate",
            json={
                "expected_version": statement["version"],
                "reason": "价格事实异常时不得作废",
                "idempotency_key": "p1-150b-block-invalid-group-regeneration",
            },
        )
        assert blocked.status_code == 409, blocked.text
        assert _error_code(blocked) == "SUPPLIER_SETTLEMENT_SOURCE_SCAN_FAILED"
        assert "PAPERBOARD_GROUP_RECEIPT_PRICE_FACT_MISMATCH" in blocked.text

    with factory() as db:
        user = db.scalar(select(User).order_by(User.id))
        assert user is not None
        with pytest.raises(SupplierSettlementError) as raised:
            generate_or_refresh_settlements(
                db,
                settlement_month="2026-08",
                user=user,
                business_date=date(2026, 9, 3),
                replace_changed_drafts=True,
                supplier_ids={int(statement["supplier_id"])},
            )
        assert raised.value.code == "SUPPLIER_SETTLEMENT_SOURCE_SCAN_FAILED"
        db.rollback()

    with factory() as db:
        stored = db.get(SupplierMonthlyStatement, int(statement["id"]))
        line = db.get(SupplierMonthlyStatementLine, line_id)
        assert stored is not None
        assert stored.status == "draft"
        assert stored.active_guard == 1
        assert stored.version == statement["version"]
        assert stored.source_hash == statement["source_hash"]
        assert line is not None
        assert line.active_guard == 1
        assert line.released_at is None
        assert db.scalar(select(func.count(FinancePayable.id))) == 0


def test_reviewed_group_draft_cannot_confirm_after_receipt_reversal(
    receipt_group_app,
) -> None:
    from app.models.finance_payable import FinancePayable

    app, factory = receipt_group_app
    _include_supplier_router(app)
    with TestClient(app) as client:
        _login(client)
        _group, _receipt_payload, received = _post_two_source_group_receipt(
            client,
            factory,
            key_prefix="p1-150b-reviewed-stale-group",
        )
        receipt_item_id = int(received["receipt_item_id"])
        _place_receipt_in_august(factory, receipt_item_id)
        generated = client.post(
            "/api/finance/supplier-settlements/generate",
            json={
                "settlement_month": "2026-08",
                "idempotency_key": "p1-150b-generate-reviewed-stale-group",
            },
        )
        assert generated.status_code == 200, generated.text
        statement = generated.json()["items"][0]
        reviewed = client.post(
            f"/api/finance/supplier-settlements/{statement['id']}/review",
            json={
                "expected_version": statement["version"],
                "supplier_statement_number": "P1-150B-REVIEWED-STALE",
                "supplier_statement_date": "2026-08-25",
                "supplier_statement_amount": "2.50",
                "idempotency_key": "p1-150b-review-before-reversal",
            },
        )
        assert reviewed.status_code == 200, reviewed.text
        statement = reviewed.json()
        reversed_response = client.put(
            f"/api/incoming/receipt-items/{receipt_item_id}/revert",
            json={
                "reason": "已复核草稿确认前撤销",
                "idempotency_key": "p1-150b-reverse-reviewed-draft",
            },
        )
        assert reversed_response.status_code == 200, reversed_response.text

        blocked = client.post(
            f"/api/finance/supplier-settlements/{statement['id']}/confirm",
            json={
                "expected_version": statement["version"],
                "idempotency_key": "p1-150b-confirm-reviewed-stale-group",
            },
        )
        assert blocked.status_code == 409, blocked.text
        assert _error_code(blocked) == "SUPPLIER_SETTLEMENT_SOURCE_STALE"

    with factory() as db:
        assert db.scalar(select(func.count(FinancePayable.id))) == 0


def test_supplier_converted_group_uses_supplier_purchase_order_in_settlement(
    receipt_group_app,
) -> None:
    app, factory = receipt_group_app
    with TestClient(app) as client:
        _login(client)
        pending = client.get("/api/requisition/pending")
        assert pending.status_code == 200, pending.text
        created = client.post(
            "/api/requisition/batches",
            json=_group_payload_from_pending(pending.json()["items"][:2]),
        )
        assert created.status_code == 201, created.text
        _convert_group_sources_to_supplier_items(factory)

        incoming = client.get("/api/incoming/pending")
        assert incoming.status_code == 200, incoming.text
        group_row = next(
            row
            for row in incoming.json()["items"]
            if str(row["item_id"]).startswith("cg")
        )
        price_fact = _freeze_direct_group_receipt_fact(
            client,
            factory,
            group_row,
            idempotency_key="p1-150b-settlement-supplier-price",
        )
        incoming = client.get("/api/incoming/pending")
        group_row = next(
            row for row in incoming.json()["items"] if row["item_id"] == group_row["item_id"]
        )
        received = client.put(
            f"/api/incoming/receive/{group_row['item_id']}",
            json=_group_receive_payload(
                group_row,
                price_fact,
                quantity=1,
                idempotency_key="p1-150b-settlement-supplier-receipt",
            ),
        )
        assert received.status_code == 200, received.text
        receipt_item_id = int(received.json()["receipt_item_id"])
        _place_receipt_in_august(factory, receipt_item_id)

    candidates, issues, _start, _end = _scan_august(factory)
    assert issues == []
    assert len(candidates) == 1
    assert candidates[0].supplier_name == "P1-150B 供应商"
    assert candidates[0].purchase_document_number == "P1-150B-SUPPLIER-CONVERTED"
    assert candidates[0].source_key == f"paperboard:{receipt_item_id}"
