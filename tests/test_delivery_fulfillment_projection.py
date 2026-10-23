"""Physical periods and frozen accounting remain independent after append."""
from datetime import date, datetime
from decimal import Decimal

from fastapi import Depends, FastAPI
from fastapi import HTTPException
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.deps import get_current_user, get_db
from app.models.delivery import Delivery, DeliveryItem
from app.models.finance import Invoice, Statement
from app.models.order import OrderItem
from app.models.user import User
from app.services import delivery_fulfillment_facts as service
from app.services.delivery_fulfillment_projection import actual_fulfillment_rows, fulfillment_by_delivery
from tests.test_delivery_fulfillment_facts import fulfillment_db, _original_rows, _payload, _submit_payload


def _seed_frozen_source(db):
    """An already consumed original source, not a current physical location claim."""
    from app.models.warehouse_inventory import (WarehouseLocation, InventoryLot,
        FinishedGoodsInventoryDetail, InventoryReservation, InventoryMovement, DeliveryInventoryAllocation)
    db.add(WarehouseLocation(id=1, location_code="TEST-HISTORIC", location_name="隔离测试原历史位", warehouse_type="finished"))
    db.flush()
    db.add(InventoryLot(id=1, lot_number="TEST-CONSUMED", inventory_type="finished", warehouse_location_id=1,
        quantity_available=0, quantity_reserved=0, quantity_consumed=800, unit="boxes", status="closed", source_type="manual",
        stock_date=date(2026, 9, 1), last_movement_at=datetime(2026, 9, 24), estimated_unit_cost_snapshot=Decimal("1.25"),
        cost_snapshot_source="historical_confirmed"))
    db.flush()
    db.add(FinishedGoodsInventoryDetail(inventory_lot_id=1, owner_customer_id=46, product_id=2402,
        inventory_code_snapshot="3D60089", product_name_snapshot="历史已消耗成品"))
    db.add(InventoryReservation(id=1, reservation_number="TEST-RES", inventory_lot_id=1, reservation_type="finished_order",
        order_id=9754, order_item_id=10376, reserved_stock_quantity=800, credited_requirement_quantity=800,
        consumed_stock_quantity=800, consumed_requirement_quantity=800, status="consumed"))
    db.flush()
    db.add(InventoryMovement(id=1, movement_number="TEST-CONSUME", inventory_lot_id=1, reservation_id=1,
        movement_type="consume", quantity=800, unit="boxes", before_available=0, after_available=0,
        before_reserved=800, after_reserved=0, before_consumed=0, after_consumed=800,
        before_damaged=0, after_damaged=0, before_scrapped=0, after_scrapped=0,
        related_order_id=9754, related_order_item_id=10376, related_delivery_id=142))
    db.flush()
    db.add(DeliveryInventoryAllocation(id=1, delivery_item_id=818, reservation_id=1, consume_movement_id=1,
        consumed_stock_quantity=800, credited_requirement_quantity=800))
    for item_id, code in ((10375, "3D30515"), (10376, "3D60089")):
        item = db.get(OrderItem, item_id)
        item.snapshot_product_code = code
        item.sales_unit_snapshot = "只"
        item.price_tax_mode_snapshot = "tax_inclusive"
    db.commit()


def _append(db, key="projection-append-001"):
    actor = db.get(User, 1)
    result = service.append_delivery_fulfillment(db, actor, 142, _submit_payload(db, actor, key))
    db.commit()
    return result


def test_projection_latest_only_preserves_nonempty_stock_cost_and_finance(fulfillment_db):
    with fulfillment_db() as db:
        _seed_frozen_source(db)
        before = _original_rows(db)
        _append(db)
        rows = actual_fulfillment_rows(db, delivery_ids=[142])
        assert [(r["delivery_item_id"], r["actual_date"], r["quantity"]) for r in rows] == [
            (817, date(2026,9,22), 50), (818, date(2026,9,22), 400), (818, date(2026,10,12), 400)]
        assert len({r["segment_identity"] for r in rows}) == 3
        first_ids = {r["segment_identity"] for r in rows}
        _append(db, "projection-revision-002")
        revised = actual_fulfillment_rows(db, delivery_ids=[142])
        assert len(revised) == 3 and sum(r["quantity"] for r in revised) == 850
        assert first_ids.isdisjoint({r["segment_identity"] for r in revised})
        assert all(r["revision"] == 2 for r in revised)
        assert _original_rows(db) == before
        assert db.get(Statement, 57).total_receivable == db.get(Invoice, 55).invoice_amount == Decimal("5030")


def test_projection_scope_completion_and_receipt_identity(fulfillment_db):
    from app.api.orders import _completion_dates_by_item
    from app.api.finance import _pending_statement_groups
    with fulfillment_db() as db:
        _append(db)
        assert actual_fulfillment_rows(db, visible_customer_ids={47}) == []
        assert _completion_dates_by_item(db, [10375, 10376]) == {10375:date(2026,9,22), 10376:date(2026,10,12)}
        for month in (9, 10):
            assert _pending_statement_groups(db, 46, statement_month=f"2026-{month:02d}",
                period_start=date(2026,month,1), period_end=date(2026,month,28)) == []
        projected = fulfillment_by_delivery(db, [142], date_from=date(2026,10,1))[142]
        assert projected["matched_physical_quantity"] == 400
        assert projected["total_physical_quantity"] == 850
        assert projected["original_document_date"] == date(2026,9,24)
        assert projected["signed_receipt_proven_by_segments"] is False
        assert projected["original_document_amount_is_period_amount"] is False


def test_delivery_filter_binds_date_product_keyword_to_same_segment(fulfillment_db):
    from app.api.deliveries import router
    with fulfillment_db() as db:
        _seed_frozen_source(db)
        _append(db)
    app = FastAPI()
    app.include_router(router, prefix="/api/deliveries")
    def session():
        with fulfillment_db() as db: yield db
    def actor(db=Depends(get_db)): return db.get(User, 1)
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = actor
    with TestClient(app) as client:
        for view in ("summary", "full"):
            for filters, expected in [
                ({"date_from":"2026-10-12", "date_to":"2026-10-12", "product_code":"30515"}, None),
                ({"date_from":"2026-10-12", "date_to":"2026-10-12", "keyword":"30515"}, None),
                ({"date_from":"2026-10-12", "product_code":"60089"}, 400),
                ({"date_from":"2026-09-22", "date_to":"2026-09-22"}, 450),
                ({"date_from":"2026-09-22", "date_to":"2026-09-22", "keyword":"30515"}, 50),
                ({"date_from":"2026-09-22", "date_to":"2026-09-22", "keyword":"60089", "product_code":"30515"}, None),
                ({"date_from":"2026-10-12", "keyword":"高泰"}, 400),
            ]:
                response = client.get("/api/deliveries", params=dict(view=view, **filters))
                assert response.status_code == 200, response.text
                body = response.json()
                if expected is None:
                    assert body["total"] == 0, body
                else:
                    assert body["total"] == 1
                    assert body["items"][0]["actual_fulfillment"]["matched_physical_quantity"] == expected


def test_split_cost_stays_unallocated_and_cannot_be_price_supplemented(fulfillment_db):
    from app.services.material_cost_lineage import material_cost_coverage_report
    from app.services.customer_delivery_margin import build_customer_delivery_margin
    with fulfillment_db() as db:
        _seed_frozen_source(db)
        _append(db)
        before = _original_rows(db)
        for month, total, sales in ((9,450,"2670.00"), (10,400,"2360.00")):
            gaps, lines = [], []
            report = material_cost_coverage_report(db, month=f"2026-{month:02d}", _gap_collector=gaps, _line_collector=lines)
            assert report["total_delivery_quantity"] == total
            assert report["fulfillment_split_cost_pending_item_ids"] == [818]
            assert report["management_cost_ready"] is False and report["lineage_ready"] is False
            assert not any(g["item"].id == 818 for g in gaps)
            split = next(line for line in lines if line["delivery_item_id"] == 818)
            assert split["actual_material_cost"] == 0 and split["management_material_cost"] == 0
            assert split["management_cost_complete"] is False
            assert "fulfillment_split_cost_allocation_pending" in split["reason_codes"]
            margin = build_customer_delivery_margin(db, date_from=date(2026,month,1), date_to=date(2026,month,28), customer_id=46)
            assert margin["summary"]["sales_amount"] == sales
            assert margin["summary"]["material_margin"] is None
            assert "fulfillment_split_cost_allocation_pending" in margin["gaps"]["reason_counts"]
        assert _original_rows(db) == before


def test_whole_line_cross_month_keeps_frozen_cost_identity_without_new_candidate(fulfillment_db):
    from app.models.material_cost_supplement import FinanceMaterialCostSupplement as Supplement
    from app.services.material_cost_lineage import material_cost_coverage_report
    from app.services.material_cost_supplement import (
        applicable_amount, canonical, fingerprint, preview as supplement_preview, target_identity,
    )
    from app.services.customer_delivery_margin import build_customer_delivery_margin

    with fulfillment_db() as db:
        _seed_frozen_source(db)
        gaps = []
        material_cost_coverage_report(db, month="2026-09", _gap_collector=gaps)
        supplements = {}
        for item_id in (817, 818):
            gap = next(g for g in gaps if g["item"].id == item_id)
            identity, key = target_identity(gap)
            source = gap.get("source") or {}
            lot = source.get("lot")
            evidence = {"test": "approved original September source", "source_id": identity["source_id"]}
            row = Supplement(delivery_item_id=item_id, inventory_lot_id=lot.id if lot else None,
                month="2026-09", source_kind=identity["source_kind"], source_id=identity["source_id"],
                target_fingerprint=key, target_json=canonical(identity), quantity_limit=gap["quantity"],
                unit_cost=Decimal("1.25"), currency="CNY",
                reference_kind="confirmed_inventory_entry_cost" if lot else "approved_test_reference",
                evidence_json=canonical(evidence), evidence_fingerprint=fingerprint(evidence),
                algorithm_version="test-reference-v1", batch_id=f"original-cost-{item_id}",
                reason="测试原单已批准冻结成本", created_by=1)
            db.add(row)
            supplements[item_id] = row
        db.commit()
        original_rows = _original_rows(db)
        assert material_cost_coverage_report(db, month="2026-09")["supplemental_material_cost"] == Decimal("1062.50")

        payload = _payload()
        payload.segments[0].actual_date = date(2026, 10, 1)
        actor = db.get(User, 1)
        service.append_delivery_fulfillment(db, actor, 142,
            _submit_payload(db, actor, "whole-line-cost-month-001", payload))
        db.commit()

        september = material_cost_coverage_report(db, month="2026-09")
        october_gaps, october_lines = [], []
        october = material_cost_coverage_report(db, month="2026-10",
            _gap_collector=october_gaps, _line_collector=october_lines)
        assert september["supplemental_material_cost"] == Decimal("0.00")
        assert october["supplemental_material_cost"] == Decimal("62.50")
        assert september["fulfillment_split_cost_pending_item_ids"] == [818]
        assert october["fulfillment_split_cost_pending_item_ids"] == [818]
        whole = next(line for line in october_lines if line["delivery_item_id"] == 817)
        split = next(line for line in october_lines if line["delivery_item_id"] == 818)
        assert whole["delivery_date"] == date(2026, 10, 1)
        assert whole["management_cost_complete"] and whole["management_material_cost"] == Decimal("62.50")
        assert not split["management_cost_complete"] and split["management_material_cost"] == 0

        # Gap collection still exposes the absent actual source, but its
        # existing approved identity must match: it cannot be adopted anew.
        assert {gap["item"].id for gap in october_gaps} == {817}
        gap = october_gaps[0]
        assert gap["month"] == "2026-09"
        assert target_identity(gap)[1] == supplements[817].target_fingerprint
        assert applicable_amount(supplements[817], gap) == Decimal("62.50")
        assert not supplement_preview(db, ["2026-09"])["proposals"]
        margin = build_customer_delivery_margin(db, date_from=date(2026, 9, 1),
            date_to=date(2026, 10, 31), customer_id=46)
        assert margin["summary"]["supplemental_material_cost"] == "62.50"
        assert _original_rows(db) == original_rows


def test_stock_reference_reports_two_actual_batches(fulfillment_db):
    from app.services.order_stock_reference import stock_reference
    with fulfillment_db() as db:
        _seed_frozen_source(db)
        _append(db)
        result = stock_reference(db, customer_id=46, product_id=2402, product_code="3D60089", quantity_unit="只", remaining_quantity=500)
        assert result["sample_count"] == 2
        assert [r["quantity"] for r in result["samples"]] == [400,400]
        assert [r["delivery_date"] for r in result["samples"]] == ["2026-10-12","2026-09-22"]


def test_confirmed_unit_evidence_fills_read_model_without_changing_old_snapshots(fulfillment_db):
    from app.services.order_stock_reference import stock_reference
    from app.services.home_stock_advice import build_stock_advice
    from app.services.product_activity import activity
    from app.models.product import Product
    with fulfillment_db() as db:
        db.get(OrderItem, 10376).snapshot_product_code = "3D60089"
        db.commit()
        assert db.get(OrderItem, 10376).sales_unit_snapshot is None
        _append(db)
        reference = stock_reference(db, customer_id=46, product_id=2402, product_code="3D60089", quantity_unit="只", remaining_quantity=500)
        assert reference["sample_count"] == 2 and reference["min_quantity"] == 400
        advice = build_stock_advice(db, warning=dict(customer_id=46,product_id=2402,unit_label="只",policy_id=1),today=date(2026,10,22))
        assert advice["shipment_count_90"] == 2 and advice["shipment_quantity_90"] == 800
        permissions = dict(requisition=False, incoming=False, production=False, orders=False, warehouse=False, deliveries=True)
        events = activity(db, db.get(Product, 2402), permissions=permissions, scope={46})["deliveries"]
        assert [e["quantity"] for e in events] == [400,400]
        assert len({e["fulfillment_segment_identity"] for e in events}) == 2
        assert all(e["unit"] == "只" for e in events)
        assert db.get(OrderItem, 10376).sales_unit_snapshot is None
        assert db.get(DeliveryItem, 818).unit_snapshot is None


def test_old_cancel_and_reference_edits_return_business_lock(fulfillment_db):
    from app.api.deliveries import (_cancel_delivery, update_delivery_customer_po, DeliveryCustomerPoUpdate,
        _delivery_finance_chain_block_reason)
    with fulfillment_db() as db:
        _append(db)
        before = _original_rows(db)
        actor = db.get(User, 1)
        assert "交付事实" in _delivery_finance_chain_block_reason(db, 142)
        with pytest.raises(HTTPException) as error:
            _cancel_delivery(142, db=db, user=actor)
        assert error.value.status_code == 409 and "交付事实" in error.value.detail
        payload = DeliveryCustomerPoUpdate(expected_version=1, idempotency_key="old-po-locked-001",
            items=[dict(delivery_item_id=817, customer_po="new-reference")])
        with pytest.raises(HTTPException) as error:
            update_delivery_customer_po(142, payload, db=db, user=actor)
        assert error.value.status_code == 409 and "交付事实" in error.value.detail
        assert _original_rows(db) == before
