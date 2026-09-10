from decimal import Decimal
from types import SimpleNamespace as NS
import pytest

from app.services.material_cost_supplement import applicable_amount, target_identity
from tests.test_phase11_requisition import requisition_app


def test_approved_reference_caps_quantity_and_rejects_changed_identity():
    gap = {"item": NS(id=1, delivery_id=2, product_id=3, order_item_id=4, source_type="order"),
           "month": "2026-08", "quantity": 5,
           "source": {"kind": "inventory_allocation", "id": 6, "lot": NS(id=7, source_ref_type=None, source_ref_id=None)},
           "reason": "estimate_only"}
    row = NS(target_fingerprint=target_identity(gap)[1], quantity_limit=10, unit_cost=Decimal("1.234567"))
    assert applicable_amount(row, gap) == Decimal("6.172835")
    gap["quantity"] = 11
    assert applicable_amount(row, gap) is None
    gap["quantity"] = 5
    gap["source"]["lot"].id = 8
    assert applicable_amount(row, gap) is None


def test_eligible_actual_and_foreign_currency_never_use_reference():
    gap = {"reason": "foreign_currency_rate_missing"}
    assert applicable_amount(NS(), gap) is None
    gap["reason"] = "actual_cost_not_frozen"
    assert applicable_amount(NS(), gap) is None


@pytest.mark.parametrize("role,active", [("finance", True), ("boss", True), ("workshop", True), ("admin", False)])
def test_only_active_admin_can_adopt(role, active):
    from app.services.material_cost_supplement import adopt
    with pytest.raises(PermissionError):
        adopt(None, months=["2026-08"], user=NS(role=role,is_active=active), expected_preview="x", batch_id="b", reason="approved")


def test_external_two_pieces_tax_currency_and_identity():
    from app.services.material_cost_supplement import _reference
    from app.models.external_packaging_purchase import ExternalPackagingReceiptItem
    purchase = NS(id=14, customer_product_id_snapshot=3, order_quantity_basis_snapshot=1000,
                  purchase_quantity_basis_snapshot=2000, unit_price=Decimal("1.97"), currency="CNY",
                  tax_mode="tax_inclusive", tax_rate=Decimal("0.13"), price_version_id=2,purchase_unit="片")
    receipt = NS(id=7,purchase_item_id=14,received_quantity=1200)
    db = NS(get=lambda cls, id: receipt if cls is ExternalPackagingReceiptItem else purchase)
    lot = NS(id=639,inventory_type="finished",finished_detail=NS(product_id=3),estimated_unit_cost_snapshot=None,
             source_ref_type="external_packaging_receipt_item",source_ref_id=7)
    gap = dict(item=NS(product_id=3,order_item_id=None),source=dict(kind="unordered_inventory_allocation",lot=lot),reason="missing_purchase_lineage",quantity=300)
    ref, error = _reference(db,gap)
    assert error is None and ref["unit_cost"] == Decimal("3.94")
    purchase.tax_mode = "tax_exclusive"
    assert _reference(db,gap)[0]["unit_cost"] == Decimal("4.4522")
    purchase.currency = "USD"
    assert _reference(db,gap)[0] is None
    purchase.currency = "CNY"
    purchase.customer_product_id_snapshot = 4
    assert _reference(db,gap)[0] is None


def test_adopt_replay_conflict_and_audit_failure_roll_back(requisition_app, monkeypatch):
    from datetime import date
    from sqlalchemy import select, func
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.user import User
    from app.models.audit import OperationLog
    from app.models.material_cost_supplement import FinanceMaterialCostSupplement as Supplement
    from app.services import material_cost_supplement as service
    _, factory = requisition_app
    with factory() as db:
        user = db.scalar(select(User).where(User.username=="admin"))
        delivery = Delivery(delivery_number="COST-TEST-001",customer_id=1,delivery_date=date(2026,8,1),source_mode="order",status="dispatched",total_quantity=5,created_by=user.id)
        db.add(delivery); db.flush()
        item = DeliveryItem(delivery_id=delivery.id,order_item_id=1,delivered_quantity=5,source_type="order")
        db.add(item); db.commit()
        actor_id, item_id = user.id, item.id
    target=dict(delivery_item_id=item_id,lot_id=None,month="2026-08",source_kind="untraced_delivery",source_id=item_id)
    plan=dict(preview_fingerprint="p"*64, months=["2026-08"],proposed_amount="6.25",missing=[],proposals=[dict(target=target,target_fingerprint="a"*64,quantity_limit=5,unit_cost="1.250000",reference_kind="order_current_material_reference",evidence={"source":1})])
    monkeypatch.setattr(service,"preview",lambda db, months:plan)
    with factory() as db:
        with pytest.raises(ValueError,match="已变化"):
            service.adopt(db,months=["2026-08"],user=db.get(User,actor_id),expected_preview="wrong",batch_id="test",reason="approved")
        db.rollback()
    original_audit = service.append_audit_event
    def fail_audit(*args,**kwargs):
        args[0].flush()
        raise RuntimeError("audit unavailable")
    monkeypatch.setattr(service,"append_audit_event",fail_audit)
    with factory() as db:
        with pytest.raises(RuntimeError,match="audit unavailable"):
            with db.begin():
                service.adopt(db,months=["2026-08"],user=db.get(User,actor_id),expected_preview="p"*64,batch_id="test",reason="approved")
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Supplement))==0
    monkeypatch.setattr(service,"append_audit_event",original_audit)
    with factory() as db:
        result=service.adopt(db,months=["2026-08"],user=db.get(User,actor_id),expected_preview="p"*64,batch_id="test",reason="approved")
        db.commit()
        assert result["count"]==1
        replay=service.adopt(db,months=["2026-08"],user=db.get(User,actor_id),expected_preview="p"*64,batch_id="test",reason="approved")
        assert replay["replayed"]
        with pytest.raises(ValueError,match="不同预览"):
            service.adopt(db,months=["2026-08"],user=db.get(User,actor_id),expected_preview="p"*64,batch_id="test",reason="changed")
        assert db.scalar(select(func.count()).select_from(Supplement))==1
        assert db.scalar(select(func.count()).select_from(OperationLog).where(OperationLog.resource=="material_cost_supplement"))==1


def test_no_reference_double_count_after_actual_cost_filled():
    from app.services.material_cost_supplement import summarize
    class NoQuery:
        def scalars(self,*args):
            raise AssertionError("No gaps: old supplement must not enter report")
    result=summarize(NoQuery(),[],2)
    assert result["supplemental_material_cost"]==0
    assert result["management_cost_ready"] and result["management_covered_lines"]==2
