from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.models.customer import Customer
from app.models.material import Material
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.supplier_requisition_order import SupplierRequisitionOrder, SupplierRequisitionOrderItem, PurchasePurposeSourceSnapshot
from app.models.user import User
from app.services.historical_closed_batch_preview import ClosedBatchRequest, preview
from app.services.sheet_cutting_contract import SheetCuttingContract


@pytest.fixture
def closed_db(tmp_path, monkeypatch):
    import app.services.historical_closed_batch_preview as service
    monkeypatch.setattr(service, "beijing_today", lambda: date(2026, 10, 22))
    engine = create_engine(f"sqlite:///{tmp_path / 'history-preview.sqlite3'}")
    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add_all([User(id=1, username="history-admin", real_name="匿名管理员", role="admin", password_hash="test-only", is_active=True),
                    User(id=2, username="history-workshop", real_name="匿名车间", role="workshop", password_hash="test-only", is_active=True),
                    Customer(id=46, name="匿名历史客户"),
                    Material(id=638, code="D928L", supplier_name="历史供应商", quote_price=Decimal("3.17"),
                             price_unit="元/㎡", purchase_currency="CNY", purchase_tax_included=True, purchase_tax_rate=Decimal("0.13"))])
        db.flush()
        db.add(Order(id=9867, order_number="TM20261013008", customer_id=46, customer_po="261013-08",
                     order_date=date(2026, 10, 13), status="pending_production", total_amount=Decimal("3140"),
                     created_at=datetime(2026, 10, 22, 10), remark="旧ERP历史补录：日期按已知凭据推算"))
        db.add(SupplierRequisitionOrder(id=227, order_number="TEST-SRO-227", supplier_name="历史供应商",
                                       status="confirmed", total_quantity=400, requisition_qty=400))
        for pid in (3325, 3215):
            db.add(Product(id=pid, customer_id=46, product_code=f"P{pid}", customer_material_code=f"C{pid}",
                           product_name="匿名纸箱", material_id=638))
        db.flush()
        for oid, sid, pid, price in ((11052, 655, 3325, "9.8"), (11053, 654, 3215, "5.9")):
            db.add(OrderItem(id=oid, order_id=9867, product_id=pid, quantity=200, unit_price=Decimal(price),
                            subtotal=200*Decimal(price), material_status="pending", snapshot_pieces_per_box=1,
                            supply_mode_snapshot="corrugated_production", sales_unit_snapshot="只",
                            snapshot_crease_type="压线", snapshot_crease_left_mm=125,
                            snapshot_crease_middle_mm=250, snapshot_crease_right_mm=125,
                            snapshot_product_name="匿名历史纸箱", snapshot_spec="100×100×100mm"))
            db.flush()
            db.add(SupplierRequisitionOrderItem(id=sid, supplier_order_id=227, order_item_id=oid,
                product_id=pid, material_id=638, material_code_snapshot="D928L", supplier_name_snapshot="历史供应商",
                report_length_mm=1500,report_width_mm=500,flute_type_snapshot="AB",layer_count_snapshot=5,
                cutting_mode="一开一", sheet_cutting_snapshot=SheetCuttingContract(1500,500).to_snapshot(),
                quantity=200, requisition_qty=200, stock_deduction_qty=0, status="active", purpose_contract_status="frozen"))
            db.flush()
            db.add(PurchasePurposeSourceSnapshot(snapshot_key=f"test:{sid}", allocation_group_key=f"test:{sid}",
                supplier_requisition_order_item_id=sid, source_kind="order_item", source_key=f"order_item:{oid}",
                source_order_item_id=oid, customer_id=46, customer_name_snapshot="匿名历史客户", component_type="whole",
                source_finished_qty_snapshot=200, pieces_per_finished_snapshot=1, source_required_piece_qty_snapshot=200,
                source_semi_reserved_piece_qty_snapshot=0, source_effective_piece_qty_snapshot=200,
                yield_per_sheet_snapshot=1, group_effective_piece_qty_snapshot=200,
                group_authoritative_order_sheet_qty_snapshot=200, purchase_sheet_qty=200,
                order_purpose_sheet_qty=200, reserve_purpose_sheet_qty=0, calculation_rule_version="test-v1",
                preview_fingerprint="a"*64, request_hash="b"*64, created_by=1))
        db.commit()
    yield factory
    engine.dispose()


def payload():
    def evidence(row):
        return dict(file_name="test-source.xls", sha256="c"*64, sheet="历史凭据", row=row)
    return dict(order_id=9867, supplier_order_id=227, customer_id=46,
        order_date=dict(value="2026-10-13", is_inferred=True, basis="按采购来源及PO推算"),
        received_date=dict(value="2026-10-19", is_inferred=True, basis="采用已知实际送货日为最晚收料日"),
        delivery_date=dict(value="2026-10-19", is_inferred=False, basis="旧对账及用户确认的实际送货日"),
        original_delivery_number="GT000666", authorization="用户确认本批历史已全部收料生产并送完，无当前存货。",
        lines=[dict(order_item_id=oid, supplier_item_id=sid, received_sheets=200,
                    finished_quantity=200, delivered_quantity=200,
                    purchase_evidence=evidence(row), delivery_evidence=evidence(row-22798))
               for oid,sid,row in ((11052,655,22807),(11053,654,22806))])


def read(factory, value=None, actor=1):
    with factory() as db:
        return preview(db, actor=db.get(User, actor), request=ClosedBatchRequest.model_validate(value or payload()))


def codes(result):
    return [r["code"] for r in result["blockers"]]


def test_missing_price_fails_closed_and_preview_writes_nothing(closed_db):
    engine = closed_db.kw["bind"]
    with engine.connect() as connection:
        before = {t.name: connection.scalar(select(func.count()).select_from(t)) for t in Base.metadata.tables.values()}
    writes = []
    def capture(_connection, _cursor, sql, *_):
        if sql.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
            writes.append(sql)
    event.listen(engine, "before_cursor_execute", capture)
    result = read(closed_db)
    again = read(closed_db)
    event.remove(engine, "before_cursor_execute", capture)
    with engine.connect() as connection:
        after = {t.name: connection.scalar(select(func.count()).select_from(t)) for t in Base.metadata.tables.values()}
    assert codes(result).count("cost_missing") == 2
    assert not result["can_submit"] and not result["business_facts_ready"]
    assert result["reviewed_hash"] == again["reviewed_hash"]
    assert result["net_current_inventory_change"] == 0
    assert not result["current_location_required"]
    assert before == after and not writes
    with closed_db() as db:
        db.get(Material,638).quote_price=Decimal("999")
        db.commit()
    assert read(closed_db)["reviewed_hash"] == result["reviewed_hash"], "current price must not become historical evidence"


def test_current_reference_price_contract_is_rejected(closed_db):
    value=payload()
    value["lines"][0]["historical_price"] = dict(basis="current_reference", unit_price="3.17",
        price_unit="per_square_meter",currency="CNY",tax_included=True,tax_rate="0.13",material_id=638,
        supplier_name="历史供应商",shipping_fee_mode="included",confirmation="这只是当前报价不是历史价格",evidence=value["lines"][0]["purchase_evidence"])
    with pytest.raises(ValidationError):
        read(closed_db,value)


def test_real_historical_contract_does_not_open_unimplemented_posting(closed_db):
    value=payload()
    for row in value["lines"]:
        row["historical_price"] = dict(basis="verified_historical_purchase_contract",unit_price="3.11",
            price_unit="per_square_meter",currency="CNY",tax_included=True,tax_rate="0.13",material_id=638,
            supplier_name="历史供应商",shipping_fee_mode="included",confirmation="测试夹具历史采购价已核准",evidence=row["purchase_evidence"])
    result=read(closed_db,value)
    assert result["business_facts_ready"]
    assert codes(result)==["historical_posting_unavailable"]
    assert not result["can_submit"]


def test_permissions_customer_and_changed_source(closed_db):
    with pytest.raises(PermissionError):read(closed_db,actor=2)
    value=payload();value["customer_id"]=99
    with pytest.raises(ValueError):read(closed_db,value)
    before=read(closed_db)
    with closed_db() as other:
        other.get(SupplierRequisitionOrderItem,655).version += 1
        other.commit()
    assert read(closed_db)["reviewed_hash"] != before["reviewed_hash"]


def test_customer_scope_is_enforced_even_with_all_preview_permissions(closed_db):
    from app.models.access_control import UserPermissionOverride, UserCustomerScope
    with closed_db() as db:
        actor=db.get(User,2);actor.customer_access_mode="selected"
        for p in ("orders.view","incoming.view","deliveries.view","requisition.purchase_price.confirm"):
            db.add(UserPermissionOverride(user_id=2,permission_code=p,is_allowed=True))
        db.commit()
    with pytest.raises(PermissionError,match="无客户访问权限"):
        read(closed_db,actor=2)
    with closed_db() as db:
        db.add(UserCustomerScope(user_id=2,customer_id=46));db.commit()
    assert not read(closed_db,actor=2)["can_submit"]


def test_existing_reversed_receipt_still_disqualifies_batch(closed_db):
    from app.models.incoming_receipt import IncomingReceipt,IncomingReceiptItem
    with closed_db() as db:
        receipt=IncomingReceipt(receipt_number="TEST-OLD-IR",status="reversed",received_at=datetime(2026,10,19),
                                received_by=1,idempotency_key="old-source-in-test")
        db.add(receipt);db.flush()
        db.add(IncomingReceiptItem(receipt_id=receipt.id,order_id=9867,order_item_id=11052,
            supplier_order_id=227,supplier_order_item_id=655,planned_quantity=200,received_quantity=200,
            cumulative_received_quantity=200,variance_quantity=0,variance_type="matched",
            resolution_status="not_required",status="reversed"))
        db.commit()
    result=read(closed_db)
    assert "existing_downstream" in codes(result)
    assert len(result["before"]["downstream"]["receipts"])==1


@pytest.mark.parametrize("change,expected", [
    ("partial","incomplete_batch"), ("quantity","quantity_not_closed"),
    ("future","historical_date_sequence"), ("ordinary_order","not_current_task_backfill"),
    ("already_received","source_state"),
    ("inference_hidden","inferred_order_date_not_marked"),
])
def test_rejects_incomplete_or_used_chains(closed_db,change,expected):
    value=payload()
    if change=="partial":value["lines"].pop()
    if change=="quantity":value["lines"][0]["received_sheets"]=201
    if change=="future":value["delivery_date"]["value"]="2026-10-23"
    if change=="inference_hidden":value["order_date"]["is_inferred"]=False
    if change in ("ordinary_order","already_received"):
        with closed_db() as db:
            if change=="ordinary_order":db.get(Order,9867).remark="正常新下单"
            else:db.get(OrderItem,11052).material_status="received"
            db.commit()
    result=read(closed_db,value)
    assert expected in codes(result) and not result["can_submit"]


def test_candidate_api_has_preview_only_and_denies_write_route(closed_db):
    from app.api.historical_closed_batches import router
    from app.api.deps import get_current_user,get_db
    app=FastAPI();app.include_router(router,prefix="/api/historical-closed-batches")
    def database():
        with closed_db() as db:yield db
    app.dependency_overrides[get_db]=database
    # Keep actor attached while effective permissions are read.
    from fastapi import Depends
    def attached_actor(db=Depends(get_db)):
        return db.get(User,1)
    app.dependency_overrides[get_current_user]=attached_actor
    with TestClient(app) as client:
        response=client.post("/api/historical-closed-batches/preview",json=payload())
        assert response.status_code==200, response.text
        assert not response.json()["can_submit"]
        assert client.post("/api/historical-closed-batches",json=payload()).status_code==404


def test_foreign_or_mixed_supplier_source_never_returns_snapshots(closed_db):
    from app.models.access_control import UserPermissionOverride, UserCustomerScope
    with closed_db() as db:
        actor=db.get(User,2);actor.customer_access_mode="selected"
        for p in ("orders.view","incoming.view","deliveries.view","requisition.purchase_price.confirm"):
            db.add(UserPermissionOverride(user_id=2,permission_code=p,is_allowed=True))
        db.add(UserCustomerScope(user_id=2,customer_id=46))
        db.add(SupplierRequisitionOrder(id=999,order_number="FOREIGN-SECRET",status="confirmed"))
        db.add(SupplierRequisitionOrder(id=998,order_number="FOREIGN-EMPTY",status="confirmed"))
        db.flush()
        db.add(SupplierRequisitionOrderItem(supplier_order_id=999,order_item_id=None,
            quantity=200, requisition_qty=200, customer_name="OUT-OF-SCOPE"))
        db.commit()
    value=payload();value["supplier_order_id"]=999
    with pytest.raises(PermissionError,match="采购来源"):
        read(closed_db,value,actor=2)
    value["supplier_order_id"]=998
    with pytest.raises(PermissionError,match="采购来源"):
        read(closed_db,value,actor=2)
    with closed_db() as db:
        db.add(SupplierRequisitionOrderItem(supplier_order_id=227,order_item_id=None,
            quantity=200, requisition_qty=200, customer_name="OUT-OF-SCOPE"))
        db.commit()
    with pytest.raises(PermissionError,match="采购来源"):
        read(closed_db,actor=2)


def test_permission_lazy_load_never_flushes_pending_objects(closed_db):
    engine=closed_db.kw["bind"];writes=[]
    def capture(_connection,_cursor,sql,*_):
        if sql.lstrip().split()[0].upper() in {"INSERT","UPDATE","DELETE","REPLACE"}:writes.append(sql)
    with closed_db() as db:
        actor=db.get(User,2)
        db.expire(actor,["is_active"])
        db.add(Customer(name="uncommitted-caller-object"))
        event.listen(engine,"before_cursor_execute",capture)
        try:
            with pytest.raises(PermissionError):
                preview(db,actor=actor,request=ClosedBatchRequest.model_validate(payload()))
            assert not writes
        finally:event.remove(engine,"before_cursor_execute",capture)


@pytest.mark.parametrize("bad_spec",["crease_type","crease_segment","crease_sum","cutting_size","cutting_yield"])
def test_incomplete_supplier_frozen_contract_stays_blocked(closed_db,bad_spec):
    with closed_db() as db:
        item=db.get(OrderItem,11052);source=db.get(SupplierRequisitionOrderItem,655)
        if bad_spec=="crease_type":item.snapshot_crease_type=None
        if bad_spec=="crease_segment":item.snapshot_crease_left_mm=None
        if bad_spec=="crease_sum":item.snapshot_crease_middle_mm=249
        if bad_spec=="cutting_size":source.sheet_cutting_snapshot=SheetCuttingContract(1400,500).to_snapshot()
        if bad_spec=="cutting_yield":source.sheet_cutting_snapshot=SheetCuttingContract(750,500,length_parts=2).to_snapshot()
        db.commit()
    result=read(closed_db)
    assert "purchase_delivery_contract_incomplete" in codes(result)
    assert not result["business_facts_ready"]
