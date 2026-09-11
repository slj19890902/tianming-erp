from datetime import date
from decimal import Decimal
from types import SimpleNamespace
import pytest
from sqlalchemy import select, func
from app.models.product import Product
from app.models.warehouse_inventory import InventoryLot, WarehouseLocation
from app.services.inventory_valuation import resolve_product_cost, can_view_inventory_cost, cost_payload
from app.services.warehouse_inventory import manual_finished_in, WarehouseInventoryError
from tests.test_inventory_cost_snapshot import db, _customer, _material
from tests.test_phase11_requisition import requisition_app


def product(db, **values):
    customer = _customer(db)
    material = _material(db, code="K7A", price="2")
    material.purchase_currency = "CNY"
    material.purchase_tax_included = True
    material.purchase_tax_rate = Decimal("0.13")
    row = Product(customer_id=customer.id, product_code="VALUATION", customer_material_code="VALUATION",
        product_name="测试箱", material_id=material.id, layer_count=3, flute_type="B", box_category="normal",
        box_style="A1", length_mm=500,width_mm=300,height_mm=200,**values)
    db.add(row);db.flush()
    return row, material


def test_entry_formula_uses_mm_not_legacy_cm_and_does_not_edit_master(db):
    row, material = product(db, default_cardboard_length=163, default_cardboard_width=50)
    before = row.report_length_mm
    result = resolve_product_cost(db, row)
    assert result.missing == []
    assert result.estimate.unit_cost == Decimal("1.6300")
    assert result.estimate.detail["formula_version"] == "tm_a1_20260729_v1"
    assert row.report_length_mm == before


def test_stocktake_cost_is_frozen_and_replay_does_not_reprice(db):
    row, material = product(db)
    location = WarehouseLocation(location_code="COST-NEW",location_name="测试位",warehouse_type="finished")
    db.add(location);db.flush()
    args=dict(customer_id=row.customer_id,product_id=row.id,location_id=location.id,quantity=8,
        stock_date=date(2026,9,11),source_type="stocktake",remarks=None,operator_id=None,idempotency_key="new-price")
    lot=manual_finished_in(db,**args)
    assert lot.estimated_unit_cost_snapshot == Decimal("1.6300")
    material.quote_price=Decimal("10");db.flush()
    replay=manual_finished_in(db,**args)
    assert replay.id == lot.id
    assert replay.estimated_unit_cost_snapshot == Decimal("1.6300")
    lot.quantity_available=5;lot.quantity_reserved=2;lot.quantity_damaged=1
    assert cost_payload(lot)["inventory_value"] == "13.04"


def test_missing_cost_does_not_create_a_priceless_stocktake_lot(db):
    row, material=product(db)
    material.quote_price=None
    location=WarehouseLocation(location_code="COST-MISSING",location_name="测试位",warehouse_type="finished")
    db.add(location);db.flush();db.commit()
    with pytest.raises(WarehouseInventoryError,match="入库成本"):
        manual_finished_in(db,customer_id=row.customer_id,product_id=row.id,location_id=location.id,
            quantity=3,stock_date=date(2026,9,11),source_type="stocktake",remarks=None,operator_id=None,idempotency_key="missing")
    db.rollback()
    assert db.scalar(select(func.count()).select_from(InventoryLot)) == 0


@pytest.mark.parametrize("role,allowed",[("admin",True),("boss",True),("finance",False),("sales",False),("warehouse",False),("workshop",False)])
def test_cost_is_admin_and_boss_only_even_for_finance(role,allowed):
    assert can_view_inventory_cost(SimpleNamespace(role=role,is_active=True)) is allowed
    assert not can_view_inventory_cost(SimpleNamespace(role=role,is_active=False))


def test_foreign_or_placeholder_inputs_are_not_silently_priced(db):
    row, material=product(db,report_length_mm=1,report_width_mm=1)
    assert resolve_product_cost(db,row).estimate is None
    row.report_length_mm=1630;row.report_width_mm=500;material.purchase_currency="USD"
    assert resolve_product_cost(db,row).estimate is None


@pytest.mark.parametrize("role",["admin","boss","finance","workshop"])
def test_cost_api_enforces_role_before_query_and_totals_reserved(db,monkeypatch,role):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import warehouse
    from app.api.deps import get_db
    row,material=product(db)
    location=WarehouseLocation(location_code="API-COST",location_name="三楼测试",warehouse_type="finished")
    db.add(location);db.flush()
    lot=manual_finished_in(db,customer_id=row.customer_id,product_id=row.id,location_id=location.id,
        quantity=8,stock_date=date(2026,9,11),source_type="stocktake",remarks=None,operator_id=None,idempotency_key="api-cost")
    lot.quantity_available=5;lot.quantity_reserved=2;lot.quantity_damaged=1;db.commit()
    app=FastAPI();app.include_router(warehouse.router,prefix="/api/warehouse")
    app.dependency_overrides[get_db]=lambda:db
    app.dependency_overrides[warehouse.can_read]=lambda:SimpleNamespace(role=role,is_active=True)
    monkeypatch.setattr(warehouse,"_visible_customer_ids",lambda *_:None)
    with TestClient(app) as client:
        r=client.get("/api/warehouse/costs",params={"location_id":location.id})
    if role in {"admin","boss"}:
        assert r.status_code==200,r.text
        assert r.json()["inventory_value"]=="13.04"
        assert r.json()["rows"][0]["unit_cost"]=="1.6300"
    else:
        assert r.status_code==403
        assert "1.6300" not in r.text


def test_dispatch_cost_uses_frozen_entry_and_return_quantity_no_repricing(requisition_app):
    from app.models.delivery import Delivery,DeliveryItem
    from app.models.user import User
    from app.models.material_cost_supplement import FinanceMaterialCostSupplement as Supplement
    from app.services.material_cost_supplement import freeze_inventory_entry_cost,applicable_amount
    _,factory=requisition_app
    with factory() as db:
        row,material=product(db)
        user=db.scalar(select(User).where(User.username=="admin"))
        location=WarehouseLocation(location_code="DISPATCH-COST",location_name="成本位",warehouse_type="finished")
        db.add(location);db.flush()
        lot=manual_finished_in(db,customer_id=row.customer_id,product_id=row.id,location_id=location.id,
            quantity=10,stock_date=date(2026,9,11),source_type="stocktake",remarks=None,operator_id=user.id,idempotency_key="dispatch-price")
        delivery=Delivery(delivery_number="COST-20260911",customer_id=row.customer_id,delivery_date=date(2026,9,11),
            source_mode="unordered_finished",status="dispatched",total_quantity=6,created_by=user.id)
        db.add(delivery);db.flush()
        item=DeliveryItem(delivery_id=delivery.id,product_id=row.id,source_type="unordered_finished",delivered_quantity=6,
            product_code_snapshot=row.product_code,product_name_snapshot=row.product_name,unit_snapshot="只",unit_price_snapshot=5,price_source="manual")
        db.add(item);db.flush()
        allocation=SimpleNamespace(id=987,delivery_item_id=item.id)
        material.quote_price=Decimal("99");db.flush()
        args=dict(allocation=allocation,lot=lot,operator_id=user.id,source_kind="unordered_inventory_allocation",quantity=6)
        snapshot=freeze_inventory_entry_cost(db,**args)
        assert snapshot.unit_cost==Decimal("1.6300")
        assert freeze_inventory_entry_cost(db,**args).id==snapshot.id
        from app.services.inventory_cost_rules import save_rule,preview_revalue,revalue
        save_rule(db,row,dict(mode="fixed",unit_cost="13",basis="后来确认的在库参考价"),user=user,
            expected_version=0,expected_product_version=row.version)
        plan=preview_revalue(db,row,[lot.id])
        revalue(db,row,[lot.id],user=user,expected=plan["fingerprint"],batch_id="after-dispatch-revalue")
        db.flush();db.refresh(snapshot)
        assert snapshot.unit_cost==Decimal("1.6300") and lot.estimated_unit_cost_snapshot==13
        gap=dict(item=item,month="2026-09",quantity=4,reason="estimate_only",order_product_id=None,
            source=dict(kind="unordered_inventory_allocation",id=allocation.id,lot=lot))
        assert applicable_amount(snapshot,gap)==Decimal("6.52")
        gap["quantity"]=7
        assert applicable_amount(snapshot,gap) is None
        assert db.scalar(select(func.count()).select_from(Supplement))==1
        with pytest.raises(ValueError,match="不能覆盖"):
            freeze_inventory_entry_cost(db,**{**args,"quantity":7})


def test_backfill_rejects_stale_price_and_rolls_back_with_audit(db,monkeypatch):
    from app.models.user import User
    from app.services import inventory_cost_backfill as service
    row,material=product(db)
    user=User(username="costadmin",real_name="成本验收",password_hash="test-only",role="admin",is_active=True)
    location=WarehouseLocation(location_code="BACKFILL",location_name="测试",warehouse_type="finished")
    db.add_all([user,location]);db.flush()
    lot=manual_finished_in(db,customer_id=row.customer_id,product_id=row.id,location_id=location.id,
        quantity=8,stock_date=date(2026,9,11),source_type="manual",remarks=None,operator_id=user.id,idempotency_key="old-unpriced")
    for field in service.COST_FIELDS:setattr(lot,field,None)
    db.commit()
    db.expire_all()
    initial=service.preview(db)
    material.quote_price=3;db.flush()
    with pytest.raises(ValueError,match="已变化"):
        service.adopt(db,user=user,expected=initial["fingerprint"],batch_id="backfill-test")
    db.rollback()
    old_audit=service.append_audit_event
    def broken(*args,**kwargs):raise RuntimeError("audit unavailable")
    monkeypatch.setattr(service,"append_audit_event",broken)
    with pytest.raises(RuntimeError,match="audit"):
        service.adopt(db,user=user,expected=initial["fingerprint"],batch_id="backfill-test")
    db.rollback();db.refresh(lot)
    assert lot.estimated_unit_cost_snapshot is None and lot.quantity_available==8
    monkeypatch.setattr(service,"append_audit_event",old_audit)
    assert service.adopt(db,user=user,expected=initial["fingerprint"],batch_id="backfill-test")["adopted"]==1
    db.commit()
    assert lot.quantity_available==8 and lot.warehouse_location_id==location.id
    assert lot.estimated_unit_cost_snapshot==Decimal("1.6300")
    assert service.preview(db)["proposals"]==[]


def test_legacy_centimetre_snapshot_is_flagged_not_used_for_dispatch(db):
    import json
    from app.services.inventory_valuation import frozen_cost
    row,material=product(db)
    location=WarehouseLocation(location_code="BAD-UNITS",location_name="测试",warehouse_type="finished")
    db.add(location);db.flush()
    lot=manual_finished_in(db,customer_id=row.customer_id,product_id=row.id,location_id=location.id,
        quantity=8,stock_date=date(2026,9,11),source_type="manual",remarks=None,operator_id=None,idempotency_key="old-cm")
    lot.estimated_unit_cost_snapshot=Decimal("0.0163");lot.cost_snapshot_source="material_quote_area"
    lot.cost_snapshot_detail_json=json.dumps(dict(price_unit="元/㎡",components=[dict(component="whole",length_mm="163",width_mm="50",pieces_per_box=1)]))
    unit,detail=frozen_cost(lot,db)
    assert unit is None and "厘米" in detail["validation_issue"]
    from app.services.inventory_cost_backfill import preview,adopt
    from app.models.user import User
    user=User(username="unitadmin",real_name="单位纠错",password_hash="test-only",role="admin",is_active=True)
    db.add(user);db.commit();db.expire_all()
    plan=preview(db)
    assert plan["proposals"][0]["correction_kind"]=="legacy_estimate_unit_error"
    adopt(db,user=user,expected=plan["fingerprint"],batch_id="unit-fix-test");db.commit()
    assert lot.estimated_unit_cost_snapshot==Decimal("1.6300")
    assert json.loads(lot.cost_snapshot_detail_json)["original_cost"]["estimated_unit_cost_snapshot"]=="0.0163"
    assert lot.quantity_available==8
    assert preview(db)["proposals"]==[]


def test_owner_authorized_recipe_reprices_new_entry_only_for_same_product_version(db):
    import json
    row,material=product(db)
    location=WarehouseLocation(location_code="RECIPE",location_name="测试",warehouse_type="finished")
    db.add(location);db.flush()
    lot=manual_finished_in(db,customer_id=row.customer_id,product_id=row.id,location_id=location.id,
        quantity=8,stock_date=date(2026,9,11),source_type="manual",remarks=None,operator_id=None,idempotency_key="recipe")
    row.box_style="未配置的模切箱";db.flush()
    lot.cost_snapshot_source="owner_current_reference_backfill"
    lot.estimated_unit_cost_snapshot=Decimal("1.6300")
    lot.cost_snapshot_detail_json=json.dumps(dict(authorization="老板确认",basis="current_reference_cost_not_historical_purchase_fact",
        product_version=row.version,currency="CNY",material_id=material.id,flute_type="B",
        components=[dict(component="whole",length_mm=1630,width_mm=500,pieces_per_box=1)]))
    material.quote_price=Decimal("4");db.flush()
    result=resolve_product_cost(db,row)
    assert result.estimate.unit_cost==Decimal("3.2600")
    assert result.estimate.detail["reference_recipe_lot_id"]==lot.id
    assert cost_payload(lot)["unit_cost"]=="1.6300"
    row.version+=1
    assert resolve_product_cost(db,row).estimate is None
