from datetime import date, datetime, timedelta
from decimal import Decimal
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from app.api.deps import get_db
from app.models.order import Order, OrderItem
from app.models.production import ProductionCompletion, ProductionCompletionBatch, ProductionTask
from app.models.product import Product
from app.models.warehouse_inventory import (FinishedGoodsInventoryDetail, InventoryLot,
    InventoryLotTransfer, InventoryMovement, SemiFinishedInventoryDetail)
from app.services.warehouse_intake_age import build_intake_age_projection, intake_bucket, intake_identity
from test_p1_29_warehouse_twin_dashboard import twin_dashboard_app, _login


@pytest.fixture()
def age_db(twin_dashboard_app):
    app, ids = twin_dashboard_app
    dependency = app.dependency_overrides[get_db]()
    db = next(dependency)
    try:
        db.query(InventoryMovement).filter(InventoryMovement.movement_type=="manual_in").update({"created_at":datetime(2025,1,1)})
        db.commit()
        yield app, ids, db
    finally:
        dependency.close()


def add_lot(db, template, number, day, *, source="stocktake", product_id=None, owner_id=None,
            basis=None, remaining=10, inbound=True):
    lot = InventoryLot(lot_number=number, inventory_type="finished",
        warehouse_location_id=template.warehouse_location_id,
        quantity_available=remaining, quantity_consumed=10-remaining, unit="boxes",
        status="active" if remaining else "closed", source_type=source,
        stock_date=day-timedelta(days=400), stock_date_accuracy="exact",
        created_at=datetime(2026, 10, 6), last_movement_at=datetime(2026, 10, 6))
    db.add(lot); db.flush()
    original = template.finished_detail
    lot.finished_detail = FinishedGoodsInventoryDetail(
        product_id=product_id or original.product_id,
        owner_customer_id=owner_id or original.owner_customer_id, is_general=False,
        inventory_code_snapshot="80011946", product_name_snapshot="同码库存",
        physical_basis_json=basis)
    if inbound:
        db.add(InventoryMovement(movement_number="AGE-"+number,
            inventory_lot_id=lot.id, movement_type="manual_in", quantity=10, unit="boxes",
            before_available=0, after_available=10, before_reserved=0, after_reserved=0,
            before_consumed=0, after_consumed=0, before_damaged=0, after_damaged=0,
            before_scrapped=0, after_scrapped=0, created_at=datetime.combine(day, datetime.min.time())))
    db.flush()
    return lot


def completion(db, lot, day, status="posted"):
    order = Order(order_number="AGE-ORDER-"+lot.lot_number,
        customer_id=lot.finished_detail.owner_customer_id, order_date=day, status="pending_production")
    db.add(order); db.flush()
    item = OrderItem(order_id=order.id, product_id=lot.finished_detail.product_id,
        quantity=10, unit_price=Decimal("1"), subtotal=10, snapshot_product_name="测试成品", snapshot_product_code="80011946")
    db.add(item); db.flush()
    task = ProductionTask(order_item_id=item.id, planned_quantity=10, status="completed")
    batch = ProductionCompletionBatch(idempotency_key="AGE-"+lot.lot_number,
        request_hash="a"*64, item_count=1, completed_at=datetime.combine(day, datetime.min.time()))
    db.add_all([task, batch]); db.flush()
    fact = ProductionCompletion(batch_id=batch.id, task_id=task.id, order_item_id=item.id,
        expected_version=1, quantity=10, status=status, initial_disposition="stock",
        warehouse_location_id=lot.warehouse_location_id, inventory_lot_id=lot.id,
        direct_delivery_quantity=0, stock_quantity=10, order_reserved_quantity=0,
        surplus_finished_quantity=10, completed_at=datetime.combine(day, datetime.min.time()))
    db.add(fact); db.flush()
    lot.source_type="production_completion"
    lot.source_ref_type="production_completion"
    lot.source_ref_id=fact.id
    lot.stock_date=day
    db.flush()
    return fact


@pytest.mark.parametrize("days,expected", [(0,"0_30"),(30,"0_30"),(31,"31_90"),
    (90,"31_90"),(91,"91_180"),(180,"91_180"),(181,"181_364"),
    (364,"181_364"),(365,"365_plus"),(None,"unknown")])
def test_five_intake_boundaries(days, expected):
    assert intake_bucket(days) == expected


def test_body_identity_uses_real_product_and_cannot_refresh_completed_stock():
    body=InventoryLot(id=1,inventory_type='assembly_body',unit='boxes')
    same_body=InventoryLot(id=2,inventory_type='assembly_body',unit='boxes')
    complete=InventoryLot(id=3,inventory_type='finished',unit='boxes')
    complete.finished_detail=FinishedGoodsInventoryDetail(product_id=42)
    assert intake_identity(body,body_product_id=42)==intake_identity(same_body,body_product_id=42)
    assert intake_identity(body,body_product_id=42)!=intake_identity(complete)
    assert intake_identity(body,body_product_id=42)!=intake_identity(same_body,body_product_id=43)


def test_new_production_refreshes_null_legacy_across_locations_and_exhaustion_then_reversal(age_db):
    _, ids, db = age_db
    template = db.scalar(select(InventoryLot).where(InventoryLot.lot_number=="FG-OWNER"))
    old = add_lot(db, template, "AGE-OLD", date(2026, 1, 1))
    new = add_lot(db, template, "AGE-NEW", date(2026, 9, 28),
        source="production_completion", basis=json.dumps(dict(product_id=old.finished_detail.product_id,
        inventory_stage="complete", schema=1)), remaining=0)
    fact = completion(db, new, date(2026, 9, 28))
    # A location split has a recent technical creation time and no new inbound.
    moved = add_lot(db, template, "AGE-SPLIT", date(2026, 10, 5), source="transfer", inbound=False)
    moved.stock_date=old.stock_date
    moved.warehouse_location_id = db.scalar(select(InventoryLot.warehouse_location_id).where(
        InventoryLot.lot_number=="FG-PENDING"))
    db.flush()
    projected = build_intake_age_projection(db, [old,moved], visible_customer_ids={ids["owner"]}, as_of=date(2026,10,6))
    assert projected[old.id]["intake_date"] == projected[moved.id]["intake_date"] == "2026-09-28"
    assert projected[old.id]["intake_age_days"] == 8
    assert old.stock_date == date(2024,11,27)  # immutable original date
    fact.status="reversed"; db.flush()
    # Existing fixture's initial inbound is also valid; move it before our baseline.
    db.query(InventoryMovement).filter(InventoryMovement.inventory_lot_id==template.id,
        InventoryMovement.movement_type=="manual_in").update({"created_at":datetime(2025,1,1)})
    projected = build_intake_age_projection(db, [old,moved], visible_customer_ids={ids["owner"]}, as_of=date(2026,10,6))
    assert projected[old.id]["intake_date"] == projected[moved.id]["intake_date"] == "2026-01-01"


def test_customer_scope_before_latest_and_same_code_bom_real_ids_and_body_stage(age_db):
    _, ids, db = age_db
    template = db.scalar(select(InventoryLot).where(InventoryLot.lot_number=="FG-OWNER"))
    old = add_lot(db, template, "AGE-VISIBLE", date(2026,9,1))
    hidden = add_lot(db, template, "AGE-HIDDEN", date(2026,10,5), owner_id=ids["hidden_owner"])
    child = Product(customer_id=ids["owner"], product_code="AGE-CHILD", customer_material_code="80011946", product_name="短片")
    db.add(child); db.flush()
    short = add_lot(db, template, "AGE-SHORT", date(2026,10,4), product_id=child.id)
    body = add_lot(db, template, "AGE-BODY", date(2026,10,3),
        basis=json.dumps(dict(inventory_stage="body")))
    scoped = build_intake_age_projection(db, [old,short,body,hidden], visible_customer_ids={ids["owner"]}, as_of=date(2026,10,6))
    assert hidden.id not in scoped
    assert scoped[old.id]["intake_date"] != "2026-10-05"
    assert len({scoped[lot.id]["intake_identity_key"] for lot in (old,short,body)}) == 3
    all_visible = build_intake_age_projection(db,[old],visible_customer_ids=None,as_of=date(2026,10,6))
    assert all_visible[old.id]["intake_date"] == "2026-10-05"


def test_unknown_has_no_fabricated_date_and_queries_are_batched(age_db):
    _, ids, db = age_db
    template = db.scalar(select(InventoryLot).where(InventoryLot.lot_number=="FG-HIDDEN"))
    unknown = add_lot(db, template, "AGE-UNKNOWN", date(2026,10,5), inbound=False)
    unknown.stock_date_accuracy="unknown";db.flush()
    queries=[]
    def capture(_conn,_cursor,sql,*_): queries.append(sql)
    event.listen(db.get_bind(),"before_cursor_execute",capture)
    try:
        data=build_intake_age_projection(db,[unknown]*100,visible_customer_ids={ids["hidden_owner"]},as_of=date(2026,10,6))
    finally:
        event.remove(db.get_bind(),"before_cursor_execute",capture)
    assert data[unknown.id]["intake_date"] is None
    assert data[unknown.id]["intake_age_bucket"] == "unknown"
    assert len(queries) <= 5
    assert not any(sql.lstrip().upper().startswith(("INSERT","UPDATE","DELETE")) for sql in queries)


def test_api_search_page_and_selected_quantities_use_whole_history(age_db):
    app, _, db = age_db
    template=db.scalar(select(InventoryLot).where(InventoryLot.lot_number=="FG-OWNER"))
    new=add_lot(db,template,"AGE-API-EXHAUSTED",date(2026,10,1),remaining=0)
    completion(db,new,date(2026,10,1));db.commit()
    with TestClient(app) as client:
        _login(client,"twin-scoped")
        for endpoint in ("/api/warehouse/twin-dashboard/search?keyword=TM-FG-001&page_size=1",
            "/api/warehouse/twin-operations/locate?keyword=TM-FG-001&search_type=finished&page_size=1",
            f"/api/warehouse/twin-operations/product-quantities/{template.id}"):
            response=client.get(endpoint)
            assert response.status_code==200,response.text
            assert response.json()["items"]
            assert all(item["intake_date"]=="2026-10-01" for item in response.json()["items"])
        overview=client.get('/api/warehouse/twin-dashboard/overview')
        assert overview.status_code==200,overview.text
        items=[]
        for location in overview.json()['locations']:
            items.extend(location.get('loose_items',[]))
            for pallet in location.get('pallets',[]):items.extend(pallet.get('items',[]))
        matched=[item for item in items if item.get('product_id')==template.finished_detail.product_id]
        assert matched
        assert all(item['intake_date']=='2026-10-01' for item in matched)


def test_quantity_and_stock_date_correction_do_not_refresh_production(age_db):
    _, ids, db=age_db
    template=db.scalar(select(InventoryLot).where(InventoryLot.lot_number=="FG-OWNER"))
    new=add_lot(db,template,"AGE-CORRECTION",date(2026,9,15))
    completion(db,new,date(2026,9,15))
    new.stock_date=date(2026,10,6)
    new.quantity_available=15
    new.last_movement_at=datetime(2026,10,6)
    db.flush()
    result=build_intake_age_projection(db,[template,new],visible_customer_ids={ids['owner']},as_of=date(2026,10,6))
    assert {r['intake_date'] for r in result.values()}=={'2026-09-15'}


def test_identity_correction_transfer_inherits_visible_root_not_creation(age_db):
    _, ids, db=age_db
    template=db.scalar(select(InventoryLot).where(InventoryLot.lot_number=="FG-OWNER"))
    original=add_lot(db,template,"AGE-CORRECT-ROOT",date(2026,8,1))
    corrected_product=Product(customer_id=ids['owner'],product_code='AGE-CORRECTED',
        customer_material_code='AGE-CORRECTED',product_name='校正身份')
    db.add(corrected_product);db.flush()
    moved=add_lot(db,template,'AGE-CORRECT-TARGET',date(2026,10,5),source='transfer',
        product_id=corrected_product.id,inbound=False)
    db.add(InventoryLotTransfer(source_lot_id=original.id,target_lot_id=moved.id,
        source_location_id=original.warehouse_location_id,target_location_id=moved.warehouse_location_id,
        quantity=10,available_quantity=10,reserved_quantity=0,source_version_before=1,
        source_version_after=2,idempotency_key='age-correct-transfer',request_hash='b'*64,
        transferred_at=datetime(2026,10,5)))
    db.flush()
    result=build_intake_age_projection(db,[moved],visible_customer_ids={ids['owner']},as_of=date(2026,10,6))
    assert result[moved.id]['intake_date']=='2026-08-01'


def test_true_finished_and_raw_receipts_refresh_only_their_real_stock_then_reverse(age_db):
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    _, ids, db=age_db
    template=db.scalar(select(InventoryLot).where(InventoryLot.lot_number=="FG-OWNER"))
    received=add_lot(db,template,'AGE-RECEIPT-FG',date(2026,10,1))
    fact=completion(db,received,date(2026,10,1))
    receipt=IncomingReceipt(receipt_number='AGE-RECEIPT',received_at=datetime(2026,10,1),idempotency_key='age-receipt')
    db.add(receipt);db.flush()
    line=IncomingReceiptItem(receipt_id=receipt.id,order_id=fact.order_item_id and db.get(OrderItem,fact.order_item_id).order_id,
        order_item_id=fact.order_item_id,planned_quantity=10,received_quantity=10,
        cumulative_received_quantity=10,variance_quantity=0,variance_type='matched',resolution_status='not_required')
    db.add(line);db.flush()
    received.source_type='replenishment';received.source_ref_type='stock_replenishment_receipt';received.source_ref_id=line.id
    paper_template=db.scalar(select(InventoryLot).where(InventoryLot.lot_number=='SI-OWNER'))
    raw=InventoryLot(lot_number='AGE-RAW-RECEIPT',inventory_type='semi_finished',
        warehouse_location_id=paper_template.warehouse_location_id,quantity_available=10,
        unit='sheets',source_type='purchase_reserve',source_ref_type='incoming_receipt_item',source_ref_id=line.id,
        stock_date=date(1999,1,1),last_movement_at=datetime(2026,10,1))
    db.add(raw);db.flush()
    raw.semi_finished_detail=SemiFinishedInventoryDetail(**{c.name:getattr(paper_template.semi_finished_detail,c.name)
        for c in SemiFinishedInventoryDetail.__table__.columns if c.name!='inventory_lot_id'})
    db.add(InventoryMovement(movement_number='AGE-RAW-IN',inventory_lot_id=raw.id,
        movement_type='manual_in',quantity=10,unit='sheets',created_at=datetime(2026,10,1),
        **{f'{prefix}_{balance}':(10 if prefix=='after' and balance=='available' else 0)
           for prefix in ('before','after') for balance in ('available','reserved','consumed','damaged','scrapped')}))
    db.flush()
    rows=[template,paper_template,raw]
    result=build_intake_age_projection(db,rows,visible_customer_ids={ids['owner']},as_of=date(2026,10,6))
    assert result[template.id]['intake_date']=='2026-10-01'
    assert result[paper_template.id]['intake_date']=='2026-10-01'
    # Reversed real receipts stop refreshing either ledger. Raw-material receipt
    # remains independent from finished product despite a shared order source.
    line.status='reversed';db.flush()
    result=build_intake_age_projection(db,rows,visible_customer_ids={ids['owner']},as_of=date(2026,10,6))
    assert result[template.id]['intake_date']=='2025-01-01'
    assert result[paper_template.id]['intake_date'] is None
    assert result[raw.id]['intake_date'] is None


def test_onboarding_uses_explicit_stocktake_day_and_future_events_remain_unknown(age_db):
    from app.models.inventory_onboarding import InventoryOnboardingBatch, InventoryOnboardingLine
    from app.models.user import User
    _, ids, db=age_db
    template=db.scalar(select(InventoryLot).where(InventoryLot.lot_number=='FG-HIDDEN'))
    original=add_lot(db,template,'AGE-ONBOARDING',date(2026,9,20),inbound=False)
    original.stock_date_accuracy='unknown'
    batch=InventoryOnboardingBatch(batch_number='AGE-ONBOARDING',source_file_reference='test.csv',
        source_file_sha256='c'*64,source_original_filename='test.csv',source_content_type='text/csv',
        source_size=1,source_format='csv',created_by=db.scalar(select(User.id).where(User.username=='twin-admin')))
    db.add(batch);db.flush()
    line=InventoryOnboardingLine(batch_id=batch.id,source_sheet_name='sheet',source_row_number=1,
        source_row_hash='d'*64,original_values_json={},stocktake_date=date(2026,9,1))
    db.add(line);db.flush()
    original.source_ref_type='inventory_onboarding_line';original.source_ref_id=line.id
    future=add_lot(db,template,'AGE-FUTURE',date(2030,1,1))
    future.finished_detail.product_id=None
    future.finished_detail.is_general=True
    future.finished_detail.owner_customer_id=None
    future.finished_detail.length_mm=10;future.finished_detail.width_mm=10;future.finished_detail.height_mm=10
    future.finished_detail.physical_basis_json='{}';future.finished_detail.material_code_snapshot='K=A'
    db.flush()
    result=build_intake_age_projection(db,[original,future],visible_customer_ids=None,as_of=date(2026,10,6))
    assert result[original.id]['intake_date']=='2026-09-01'
    assert result[future.id]['intake_date'] is None
