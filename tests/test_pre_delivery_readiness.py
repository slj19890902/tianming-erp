from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.requisition import Requisition, RequisitionItem
from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
from app.services.pre_delivery_readiness import order_readiness, allocate_readiness, _paper_capacity


@pytest.fixture
def context(tmp_path):
    engine = create_sqlite_engine(tmp_path / "isolated-readiness.sqlite3")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        customer = Customer(customer_number=1, customer_code="YG", name="测试客户", credit_limit=0)
        db.add(customer); db.flush()
        product = Product(customer_id=customer.id, product_code="TEST", customer_material_code="TEST", product_name="测试箱", box_category="normal")
        db.add(product); db.flush()
        order = Order(order_number="TEST-1", customer_id=customer.id, order_date=date.today(),
                      status="pending_delivery", payment_status="unpaid", total_amount=0)
        db.add(order); db.flush()
        item = OrderItem(order_id=order.id, product_id=product.id, quantity=600, delivered_quantity=0,
                         unit_price=0, subtotal=0, material_status="pending", requisition_status="已报料",
                         snapshot_product_name="测试箱", snapshot_product_code="TEST")
        db.add(item); db.flush()
        yield db, item


def purchase(db, item, quantity=200, name="测试箱", pieces=1):
    n = db.query(Requisition).count() + 1
    header = Requisition(requisition_number=f"R{n}", requisition_date=date.today())
    db.add(header); db.flush()
    row = RequisitionItem(requisition_id=header.id, order_item_id=item.id, requisition_qty=quantity,
                          cardboard_len=100, cardboard_width=100, product_name_snapshot=name,
                          pieces_per_box=pieces)
    db.add(row); db.flush()
    return row


def receipt(db, item, source, quantity, *, action="await_supplier", status="posted"):
    n = db.query(IncomingReceipt).count() + 1
    header = IncomingReceipt(receipt_number=f"IN{n}", received_at=datetime.now(), idempotency_key=f"in{n}")
    db.add(header); db.flush()
    row = IncomingReceiptItem(receipt_id=header.id, order_id=item.order_id, order_item_id=item.id,
        requisition_item_id=source.id, planned_quantity=source.requisition_qty, received_quantity=quantity,
        cumulative_received_quantity=quantity, variance_quantity=quantity-source.requisition_qty,
        variance_type="short", resolution_status="resolved" if action=="accept_short" else "pending",
        resolution_action=action, status=status)
    db.add(row); db.flush()
    return row


def test_status_alone_is_not_inbound(context):
    db, item = context
    value = order_readiness(db, item)
    assert value["effective_inbound"] == value["pending_processing"] == 0
    assert value["review_reasons"]


def test_partial_purchase_receipt_and_short_close(context):
    db, item = context
    source = purchase(db, item, 200)
    assert order_readiness(db, item)["effective_inbound"] == 200
    received = receipt(db, item, source, 80)
    value = order_readiness(db, item)
    assert (value["pending_processing"], value["effective_inbound"]) == (80, 120)
    diagnostic = allocate_readiness(600, [(item.id, 600)], {item.id: value}, {})
    assert diagnostic["new_purchase_shortage"] == 400
    received.resolution_status, received.resolution_action = "resolved", "accept_short"
    db.flush()
    value = order_readiness(db, item)
    assert (value["pending_processing"], value["effective_inbound"]) == (80, 0)
    assert allocate_readiness(600, [(item.id, 600)], {item.id: value}, {})["new_purchase_shortage"] == 520


def test_receipt_reversal_and_void_source(context):
    db, item = context
    source = purchase(db, item)
    receipt(db, item, source, 80, status="reversed")
    value = order_readiness(db, item)
    assert (value["pending_processing"], value["effective_inbound"]) == (0, 200)
    source.status = "已作废"; db.flush()
    assert order_readiness(db, item)["effective_inbound"] == 0


def test_frozen_conversion_and_component_bottleneck(context):
    db, item = context
    source = purchase(db, item, 100, pieces=2)
    receipt(db, item, source, 1)
    # One sheet remains a fractional box; cumulative floors preserve it.
    value = order_readiness(db, item)
    assert (value["pending_processing"], value["effective_inbound"]) == (0, 50)
    source.status="已作废"; db.flush()
    purchase(db, item, 100, name="测试箱-盖")
    assert _paper_capacity(db, item)[:2] == (0, 0)
    purchase(db, item, 70, name="测试箱-底")
    assert _paper_capacity(db, item)[:2] == (0, 70)


def test_reserve_purpose_is_not_order_inbound(context):
    from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot
    from app.models.user import User
    db, item = context
    user = User(username="tester", password_hash="isolated", role="admin", real_name="测试员", display_name="测试员")
    db.add(user); db.flush()
    source = purchase(db, item, 200)
    source.purpose_contract_status="frozen"
    snapshot = PurchasePurposeSourceSnapshot(snapshot_key="test", allocation_group_key="test",
        material_requisition_item_id=source.id, source_kind="requisition_item", source_key="r1",
        source_order_item_id=item.id, source_requisition_item_id=source.id,
        customer_id=db.get(Order,item.order_id).customer_id, customer_name_snapshot="测试客户",
        component_type="whole", source_finished_qty_snapshot=600, pieces_per_finished_snapshot=2,
        source_required_piece_qty_snapshot=1200, source_semi_reserved_piece_qty_snapshot=0,
        source_effective_piece_qty_snapshot=240, yield_per_sheet_snapshot=2,
        group_effective_piece_qty_snapshot=240, group_authoritative_order_sheet_qty_snapshot=120,
        purchase_sheet_qty=200, order_purpose_sheet_qty=120, reserve_purpose_sheet_qty=80,
        calculation_rule_version="test", preview_fingerprint="a"*64, request_hash="b"*64, created_by=user.id)
    db.add(snapshot); db.flush()
    assert order_readiness(db,item)["effective_inbound"] == 120


def test_allocation_does_not_reuse_order_or_unselected_candidate():
    facts={1:dict(finished_available=100,pending_processing=40,effective_inbound=60,review_reasons=[]),
           2:dict(finished_available=500,pending_processing=0,effective_inbound=0,review_reasons=[])}
    used={}
    first=allocate_readiness(150,[(1,150)],facts,used)
    assert (first["finished_available"], first["effective_inbound"]) == (100,10)
    second=allocate_readiness(100,[(1,100)],facts,used)
    assert (second["finished_available"],second["effective_inbound"],second["new_purchase_shortage"]) == (0,50,50)
    chosen=allocate_readiness(200,[(1,100),(2,100)],facts,{})
    assert chosen["finished_available"]==200
    foreign=allocate_readiness(200,[(3,200)],facts,{})
    assert foreign["unresolved_quantity"]==200 and foreign["pending_review"]


def test_initial_diagnostic_covers_file_demand_not_finished_suggestion(context, monkeypatch):
    from types import SimpleNamespace
    from app.services import pre_delivery_readiness as service
    db, item = context
    monkeypatch.setattr(service,"order_readiness",lambda *_:dict(finished_available=100,
        pending_processing=80,effective_inbound=120,pending_quantity=600,review_reasons=[]))
    payload={"draft":None,"items":[dict(item_id=999,product_id=item.product_id,order_item_id=item.id,
        final_delivery_qty=100,image_qty=600,source_payload={})]}
    batch=SimpleNamespace(customer_id=db.get(Order,item.order_id).customer_id)
    service.refresh_excel_readiness(db,batch,payload)
    diagnostic=payload["items"][0]["source_payload"]["shortage_diagnostic"]
    assert (diagnostic["finished_available"],diagnostic["effective_inbound"],diagnostic["new_purchase_shortage"]) == (100,120,300)
    service.refresh_excel_readiness(db,batch,payload,{999:{"order_item_id":item.id,"final_delivery_qty":50}})
    assert payload["items"][0]["source_payload"]["shortage_diagnostic"]["finished_available"] == 50
