"""P0-45C: incomplete supplier periods cannot create or confirm partial payables."""

from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session


@pytest.fixture()
def settlement_db(tmp_path):
    from app.models import Base
    from app.models.material import Material
    from app.models.supplier import Supplier, SupplierAlias
    from app.models.user import User

    # Metadata-only temporary fixtures represent pre-gate historical receipts.
    # No application database/backup startup and no formal receipt guard bypass.
    engine = create_engine(f"sqlite:///{(tmp_path / 'historical-fixture.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(username="p045-isolated", password_hash="unused", role="admin", real_name="隔离测试")
        supplier = Supplier(standard_name="隔离供应商甲", normalized_name="隔离供应商甲", settlement_day=20)
        other = Supplier(standard_name="隔离供应商乙", normalized_name="隔离供应商乙", settlement_day=20)
        material = Material(code="P045-TEST", supplier_name="隔离供应商甲", is_active=True, version=1)
        db.add_all([user, supplier, other, material])
        db.flush()
        db.add(SupplierAlias(supplier_id=supplier.id, alias_name="甲别名", normalized_alias="甲别名"))
        db.flush()
        yield db, user, supplier, other, material
    engine.dispose()


def _receipt(db, user, supplier, material, key, *, priced=True,
             received_at=datetime(2026, 8, 5, 3), supplier_name=None):
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.models.stock_replenishment import StockReplenishmentOrder, StockReplenishmentOrderItem
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact

    source_order = StockReplenishmentOrder(
        order_number=f"SR-{key}", supplier_name=supplier_name or supplier.standard_name,
        source_type="manual_history", status="stocked", created_by=user.id,
    )
    db.add(source_order)
    db.flush()
    source = StockReplenishmentOrderItem(
        replenishment_order_id=source_order.id, target_inventory_type="semi_finished",
        procurement_route_snapshot="paperboard", material_id=material.id,
        product_name_snapshot="隔离测试片料", material_code_snapshot=material.code,
        report_length_mm=1000, report_width_mm=500, quantity=10, stocked_quantity=10,
    )
    receipt = IncomingReceipt(
        receipt_number=f"IR-{key}", status="posted", received_at=received_at,
        received_by=user.id, idempotency_key=f"receipt-{key}",
    )
    db.add_all([source, receipt])
    db.flush()
    item = IncomingReceiptItem(
        receipt_id=receipt.id, stock_replenishment_item_id=source.id,
        planned_quantity=10, received_quantity=10, cumulative_received_quantity=10,
        variance_quantity=0, variance_type="matched", resolution_status="not_required", status="posted",
    )
    db.add(item)
    db.flush()
    if priced:
        fact = SupplierReceiptSettlementPriceFact(
            incoming_receipt_item_id=item.id, supplier_id=supplier.id,
            supplier_name_snapshot=supplier.standard_name, material_id=material.id,
            material_code_snapshot=material.code, source_material_version=1,
            source_kind="stock_replenishment_item", purchase_document_number_snapshot=source_order.order_number,
            receipt_number_snapshot=receipt.receipt_number, receipt_date_snapshot=received_at.date(),
            received_quantity_snapshot=10, quantity_unit="张", report_length_mm=1000, report_width_mm=500,
            unit_price=Decimal("2.50"), price_unit="per_sheet", currency="CNY", tax_included=True,
            tax_rate=Decimal("0.13"), shipping_fee_mode="included", fact_origin="receipt_frozen",
            match_strategy="stable_material_id", source_hash="a" * 64, created_by=user.id,
        )
        db.add(fact)
        db.flush()
    return item


def _generate(db, user, **kwargs):
    from app.services.supplier_monthly_settlement import generate_or_refresh_settlements

    return generate_or_refresh_settlements(
        db, settlement_month="2026-08", business_date=date(2026, 9, 1), user=user, **kwargs,
    )


def test_mixed_priced_and_missing_receipts_block_partial_supplier_draft(settlement_db):
    from app.models.supplier_settlement import SupplierMonthlyStatement
    from app.services.supplier_monthly_settlement import scan_settlement_candidates

    db, user, supplier, _, material = settlement_db
    _receipt(db, user, supplier, material, "priced")
    missing = _receipt(db, user, supplier, material, "missing", priced=False)
    candidates, issues, _, _ = scan_settlement_candidates(db, settlement_month="2026-08")
    assert len(candidates) == len(issues) == 1
    assert candidates[0].erp_amount == Decimal("25.00")
    assert issues[0]["source_key"] == f"paperboard:{missing.id}"

    result = _generate(db, user)
    assert result["added_line_count"] == 0
    assert result["changed_statement_count"] == 0
    assert db.scalar(select(func.count(SupplierMonthlyStatement.id))) == 0
    assert result["completeness"]["status"] == "blocked"
    assert result["completeness"]["blocked_supplier_count"] == 1
    blocked = result["blocked_periods"][0]
    assert blocked["supplier_id"] == supplier.id
    assert blocked["issue_count"] == 1
    assert blocked["source_keys"] == [f"paperboard:{missing.id}"]


def test_missing_supplier_alias_blocks_only_its_supplier_and_automatic_generation(settlement_db):
    from app.services.supplier_monthly_settlement import generate_due_supplier_settlements

    db, user, supplier, other, material = settlement_db
    _receipt(db, user, supplier, material, "priced-a")
    _receipt(db, user, supplier, material, "missing-a", priced=False, supplier_name="甲别名")
    _receipt(db, user, other, material, "priced-b")
    result = generate_due_supplier_settlements(db, user=user, business_date=date(2026, 9, 1))
    assert result["changed_statement_count"] == 1
    assert result["completeness"]["status"] == "blocked"
    assert result["blocked_periods"][0]["supplier_id"] == supplier.id
    assert result["results"][0]["items"][0]["supplier_id"] == other.id
    assert result["results"][0]["items"][0]["erp_amount"] == Decimal("25.00")
    assert result["issues"][0]["receipt_number"] == "IR-missing-a"


def test_supplier_actual_cutoff_and_other_month_gaps_do_not_block(settlement_db):
    db, user, supplier, other, material = settlement_db
    supplier.settlement_day = 10
    _receipt(db, user, supplier, material, "a-in-period")
    _receipt(db, user, supplier, material, "a-after-cutoff", priced=False,
             received_at=datetime(2026, 8, 15, 3))
    _receipt(db, user, other, material, "b-in-period")
    _receipt(db, user, other, material, "b-next-month", priced=False,
             received_at=datetime(2026, 9, 1, 3))
    result = _generate(db, user)
    assert result["changed_statement_count"] == 2
    assert result["completeness"]["status"] == "complete"
    assert result["issues"] == []


def _draft(db, user, supplier, material):
    from app.models.supplier_settlement import SupplierMonthlyStatement

    _receipt(db, user, supplier, material, "original-priced")
    result = _generate(db, user)
    row = db.get(SupplierMonthlyStatement, result["items"][0]["id"])
    row.supplier_statement_number = "ISOLATED-REVIEW"
    row.supplier_statement_date = date(2026, 8, 25)
    row.supplier_statement_amount = row.adjusted_amount
    db.flush()
    return row


def test_refresh_preserves_old_draft_and_overview_marks_it_blocked(settlement_db):
    from app.models.supplier_settlement import SupplierMonthlyStatement, SupplierMonthlyStatementLine
    from app.services.supplier_monthly_settlement import list_statement_responses, supplier_settlement_overview

    db, user, supplier, _, material = settlement_db
    row = _draft(db, user, supplier, material)
    original = (row.id, row.version, row.source_hash, row.erp_amount, row.active_guard)
    _receipt(db, user, supplier, material, "late-missing", priced=False)
    result = _generate(db, user)
    assert result["changed_statement_count"] == 0
    assert result["released_line_count"] == 0
    assert result["items"][0]["completeness"]["status"] == "blocked"
    assert (row.id, row.version, row.source_hash, row.erp_amount, row.active_guard) == original
    assert db.scalar(select(func.count(SupplierMonthlyStatement.id))) == 1
    assert db.scalar(select(func.count(SupplierMonthlyStatementLine.id))) == 1
    _, _, periods = supplier_settlement_overview(db, settlement_month="2026-08")
    items = list_statement_responses(db, settlement_month="2026-08", supplier_periods=periods)
    assert items[0]["completeness"]["issue_count"] == 1


@pytest.mark.parametrize("operation", ["regenerate", "confirm"])
def test_old_draft_cannot_be_replaced_or_confirmed_with_missing_receipt(settlement_db, operation):
    from app.models.finance_payable import FinancePayable
    from app.models.supplier_settlement import SupplierMonthlyStatementLine
    from app.services.supplier_monthly_settlement import SupplierSettlementError, confirm_statement, regenerate_statement

    db, user, supplier, _, material = settlement_db
    row = _draft(db, user, supplier, material)
    version = row.version
    missing = _receipt(db, user, supplier, material, "late-missing", priced=False)
    with pytest.raises(SupplierSettlementError) as error:
        if operation == "regenerate":
            regenerate_statement(db, statement_id=row.id, expected_version=version, reason="隔离核对", user=user)
        else:
            confirm_statement(db, statement_id=row.id, expected_version=version, user=user)
    assert error.value.code == "SUPPLIER_SETTLEMENT_INCOMPLETE"
    assert error.value.status_code == 409
    assert error.value.details["issues"][0]["source_key"] == f"paperboard:{missing.id}"
    assert error.value.details["completeness"]["supplier_id"] == supplier.id
    assert row.version == version and row.status == "draft" and row.active_guard == 1
    assert db.scalar(select(func.count(FinancePayable.id))) == 0
    assert db.scalar(select(SupplierMonthlyStatementLine.active_guard)) == 1


def test_newly_priced_receipt_requires_regeneration_before_confirming_old_draft(settlement_db):
    from app.models.finance_payable import FinancePayable
    from app.services.supplier_monthly_settlement import SupplierSettlementError, confirm_statement

    db, user, supplier, _, material = settlement_db
    row = _draft(db, user, supplier, material)
    _receipt(db, user, supplier, material, "late-priced")
    with pytest.raises(SupplierSettlementError) as error:
        confirm_statement(db, statement_id=row.id, expected_version=row.version, user=user)
    assert error.value.code == "SUPPLIER_SETTLEMENT_REGENERATION_REQUIRED"
    assert db.scalar(select(func.count(FinancePayable.id))) == 0


def test_frozen_supplier_id_scopes_invalid_snapshot_even_after_display_name_changes(settlement_db):
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact

    db, user, supplier, other, material = settlement_db
    item = _receipt(db, user, supplier, material, "invalid-snapshot")
    fact = db.scalar(select(SupplierReceiptSettlementPriceFact).where(
        SupplierReceiptSettlementPriceFact.incoming_receipt_item_id == item.id))
    fact.supplier_name_snapshot = "已经变更的历史显示名"
    fact.received_quantity_snapshot = 99
    _receipt(db, user, other, material, "other-valid")
    result = _generate(db, user)
    assert result["blocked_periods"][0]["supplier_id"] == supplier.id
    assert result["completeness"]["blocked_supplier_count"] == 1
    assert result["items"][0]["supplier_id"] == other.id


def test_reversed_and_authorized_finance_test_remain_compatible(settlement_db):
    from app.models.stock_replenishment import StockReplenishmentOrderItem
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact

    db, user, supplier, _, material = settlement_db
    reversed_item = _receipt(db, user, supplier, material, "reversed", priced=False)
    reversed_item.status = "reversed"
    classified = _receipt(db, user, supplier, material, "authorized-test")
    source = db.get(StockReplenishmentOrderItem, classified.stock_replenishment_item_id)
    source.procurement_route_snapshot = "external_packaging"
    fact = db.scalar(select(SupplierReceiptSettlementPriceFact).where(
        SupplierReceiptSettlementPriceFact.incoming_receipt_item_id == classified.id))
    fact.fact_origin = "historical_master_adoption"
    fact.match_strategy = "owner_authorized_finance_test_classification"
    fact.finance_only_test_classification = True
    fact.adoption_reason = "2026-09-02 老板确认采用当前主数据"
    fact.adoption_evidence_reference = "isolated-existing-authorized-fact"
    fact.received_quantity_snapshot = 2
    result = _generate(db, user)
    assert result["completeness"]["status"] == "complete"
    assert result["added_line_count"] == 1
    assert result["items"][0]["erp_amount"] == Decimal("5.00")
    assert classified.received_quantity == 10


def test_external_route_on_generic_receipt_does_not_prove_independent_payable_exists(settlement_db):
    from app.models.stock_replenishment import StockReplenishmentOrderItem

    db, user, supplier, _, material = settlement_db
    _receipt(db, user, supplier, material, "normal-priced")
    external = _receipt(db, user, supplier, material, "external-on-generic-route", priced=False)
    db.get(StockReplenishmentOrderItem, external.stock_replenishment_item_id).procurement_route_snapshot = "external_packaging"
    result = _generate(db, user)
    assert result["completeness"]["status"] == "blocked"
    assert result["added_line_count"] == 0
    assert result["issues"][0]["source_key"] == f"paperboard:{external.id}"
    assert result["issues"][0]["code"] == "FINISHED_REPLENISHMENT_PAYABLE_SOURCE_MISSING"


def test_confirmed_fact_is_unchanged_and_can_invoice_after_later_gap(settlement_db):
    from app.services.supplier_monthly_settlement import add_invoice, confirm_statement

    db, user, supplier, _, material = settlement_db
    row = _draft(db, user, supplier, material)
    row.source_hash = None  # Legacy drafts need matching line facts, not a new stored hash.
    row = confirm_statement(db, statement_id=row.id, expected_version=row.version, user=user)
    confirmed = (row.confirmed_amount, row.finance_payable_id, row.confirmed_at, row.version)
    _receipt(db, user, supplier, material, "later-unpriced", priced=False)
    result = _generate(db, user)
    assert result["completeness"]["status"] == "blocked"
    assert "completeness" not in result["items"][0]
    assert (row.confirmed_amount, row.finance_payable_id, row.confirmed_at, row.version) == confirmed
    row, invoice = add_invoice(
        db, statement_id=row.id, expected_version=row.version, invoice_number="ISOLATED-INVOICE",
        invoice_date=date(2026, 8, 26), received_date=date(2026, 8, 27),
        invoice_total_amount=Decimal("25.00"), allocated_amount=Decimal("25.00"),
        tax_amount=Decimal("2.88"), note="隔离验证已确认历史事实", user=user,
    )
    assert invoice.allocated_amount == Decimal("25.00")
    assert row.status == "invoiced_pending_payment"
