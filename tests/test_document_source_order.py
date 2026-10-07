from datetime import date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryItem
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.user import User
from app.models.tianhua_pre_delivery import (
    PreDeliverySourceAllocation, TianhuaPreDeliveryDraft,
    TianhuaPreDeliveryDraftItem, TianhuaPreDeliveryImportBatch,
    TianhuaPreDeliveryImportItem,
)


@pytest.fixture
def document_db(tmp_path):
    engine = create_sqlite_engine(tmp_path / 'source-order.sqlite3')
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        customer = Customer(name='顺序测试', customer_number=901)
        user = User(username='source-order-admin', password_hash='unused',
                    real_name='测试', role='admin', must_change_password=False)
        db.add_all([customer, user]); db.flush()
        order = Order(order_number='SOURCE-ORDER', customer_id=customer.id,
                      order_date=date.today(), status='pending_delivery',
                      payment_status='unpaid', total_amount=60)
        db.add(order); db.flush()
        # IDs, product codes and receipt dates deliberately disagree with source order.
        for sequence, code in [(3, 'AAA'), (1, 'ZZZ'), (2, 'MMM')]:
            product = Product(customer_id=customer.id, product_code=code, customer_material_code=code,
                              product_name=code, box_category='normal')
            db.add(product); db.flush()
            db.add(OrderItem(order_id=order.id, product_id=product.id,
                             item_sequence=sequence, quantity=20, delivered_quantity=0,
                             unit_price=1, subtotal=20, material_status='received',
                             material_received_at=datetime(2026, 9, 1 + sequence),
                             snapshot_product_code=code, snapshot_product_name=code))
        db.flush()
        items = list(db.scalars(select(OrderItem).order_by(OrderItem.item_sequence)))
        delivery = Delivery(delivery_number='SOURCE-DELIVERY', customer_id=customer.id,
                            delivery_date=date.today(), total_quantity=6, status='pending')
        db.add(delivery); db.flush()
        for item in reversed(items):
            db.add(DeliveryItem(delivery_id=delivery.id, order_item_id=item.id,
                                delivered_quantity=2))
        db.commit()
        yield db, user, order, items, delivery
    engine.dispose()


def test_order_detail_and_paginated_delivery_candidates_keep_original_sequence(document_db):
    from app.api.orders import _order_response
    from app.api.deliveries import _pending_query
    db, user, order, items, _ = document_db
    expected = [item.id for item in items]
    assert [i['id'] for i in _order_response(order, user, db=db)['items']] == expected
    query = _pending_query(db=db, customer_id=order.customer_id)
    assert [r.order_item_id for r in db.execute(query.limit(2))] == expected[:2]
    assert [r.order_item_id for r in db.execute(query.offset(2).limit(2))] == expected[2:]
    assert list(db.execute(_pending_query(db=db, customer_ids=set()))) == []


def _assert_delivery_order(db, user, delivery, expected):
    from app.api.deliveries import _delivery_item_rows, _delivery_response, get_delivery_print_data
    assert [r['order_item_id'] for r in _delivery_item_rows(db, [delivery.id])] == expected
    detail = _delivery_response(db, delivery.id)
    assert [r['order_item_id'] for r in detail['items']] == expected
    printed = get_delivery_print_data(delivery.id, db=db, user=user)
    assert [r['order_item_id'] for r in printed['items']] == expected
    assert [r['quantity'] for r in printed['items']] == [2] * len(expected)
    assert not db.dirty and not db.new and not db.deleted


def test_reversed_delivery_selection_prints_in_order_sequence(document_db):
    db, user, _, items, delivery = document_db
    _assert_delivery_order(db, user, delivery, [item.id for item in items])


def test_pre_delivery_rows_override_order_sequence_including_split_and_recreated_lines(document_db):
    db, user, order, items, delivery = document_db
    batch = TianhuaPreDeliveryImportBatch(batch_number='BATCH', filename='rows.xlsx',
                customer_id=order.customer_id, customer_name='顺序测试', total_rows=2)
    db.add(batch); db.flush()
    draft = TianhuaPreDeliveryDraft(batch_id=batch.id, draft_number='DRAFT',
                customer_id=order.customer_id, delivery_id=delivery.id)
    db.add(draft); db.flush()
    # Original pre-delivery is item 2, then a split row for item 3 and item 1.
    for row_no, targets in [(1, [items[1]]), (2, [items[2], items[0]])]:
        first = targets[0]
        source = TianhuaPreDeliveryImportItem(batch_id=batch.id, row_no=row_no,
                    stock_code=first.snapshot_product_code, status='ok')
        db.add(source); db.flush()
        db.add(TianhuaPreDeliveryDraftItem(draft_id=draft.id, import_item_id=source.id,
                    row_no=row_no, stock_code=first.snapshot_product_code, product_id=first.product_id,
                    order_item_id=first.id, order_id=order.id, order_number=order.order_number,
                    delivery_qty=2 * len(targets)))
        if len(targets) > 1:
            for item in targets:
                db.add(PreDeliverySourceAllocation(import_item_id=source.id,
                            order_item_id=item.id, allocated_qty=2))
    db.commit()
    expected = [items[1].id, items[2].id, items[0].id]
    _assert_delivery_order(db, user, delivery, expected)
    first_line = db.scalar(select(DeliveryItem).where(DeliveryItem.order_item_id == items[1].id))
    first_line.is_current = False
    db.flush()
    db.add(DeliveryItem(delivery_id=delivery.id, order_item_id=items[1].id,
                        delivered_quantity=2, revision_number=2))
    db.commit()
    _assert_delivery_order(db, user, delivery, expected)


def test_legacy_items_without_sequence_and_duplicate_codes_do_not_merge(document_db):
    db, user, _, items, delivery = document_db
    for item in items:
        item.item_sequence = None
        item.snapshot_product_code = 'SAME-CODE'
    db.commit()
    _assert_delivery_order(db, user, delivery, sorted(item.id for item in items))


def test_delivery_editor_uses_source_sequence_and_preserves_pre_delivery_order(tmp_path):
    import json
    import shutil
    import subprocess
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / 'static/index.html').read_text(encoding='utf-8')
    body = html.split('deliveryUnifiedLines() {', 1)[1].split('\n          },', 1)[0]
    script = '''const assert = require('node:assert/strict');
const sort = new Function(BODY);
const lines = [
 {key:'three',order_id:9,order_item_id:10,item_sequence:3},
 {key:'stock',source_type:'unordered_finished'},
 {key:'one',order_id:9,order_item_id:20,item_sequence:1},
 {key:'two',order_id:9,order_item_id:30,item_sequence:2},
 {key:'blank'}];
const run = rows => sort.call({deliveryForm:{lines:rows}}).map(row=>row.key);
assert.deepEqual(run(lines), ['one','two','three','stock','blank']);
assert.deepEqual(lines.map(row=>row.key), ['three','stock','one','two','blank']);
const pre = lines.filter(row=>row.order_item_id).map((row,i)=>({...row,pre_delivery_position:[i+1,0]}));
assert.deepEqual(run(pre), ['three','one','two']);
assert.deepEqual(run([lines[0], lines[3], lines[2]]), ['one','two','three']);
'''.replace('BODY', json.dumps(body))
    result = subprocess.run([shutil.which('node'), '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
