from datetime import date
import json
import sqlite3

import pytest
from sqlalchemy.orm import Session

from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryItem
from app.models.order import Order, OrderItem
from app.models.product import Product
from scripts.admin.repair_customer_drawings_20261007 import (
    BATCH, TARGETS, SNAPSHOT, apply_plan, build_plan, encoded, protected_facts,
)


@pytest.fixture
def repair_db(tmp_path):
    path = tmp_path / 'isolated.sqlite3'
    engine = create_sqlite_engine(path)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Customer(id=137, name='研光隔离测试', customer_number=901))
        db.flush()
        db.add(Order(id=9777, customer_id=137, order_number='ISOLATED', order_date=date.today(),
                     status='pending_delivery', payment_status='unpaid', total_amount=100))
        db.add(Delivery(id=147, customer_id=137, delivery_number='YG-20261007-001',
                        delivery_date=date.today(), status='dispatched', version=1, total_quantity=100))
        db.flush()
        for pid, code, oid, did, before, after in TARGETS:
            db.add(Product(id=pid, customer_id=137, product_code=code, customer_material_code=code,
                           product_name=code, box_category='normal', customer_drawing_number=after))
            db.flush()
            snapshot = encoded({'schema_version': 1, 'product_id': pid, 'customer_material_code': code,
                                'customer_drawing_number': before, 'customer_drawing_display': '',
                                'customer_model': 'ORIGINAL-MODEL'})
            db.add(OrderItem(id=oid, order_id=9777, product_id=pid, quantity=100, unit_price=1,
                             subtotal=100, snapshot_product_name=code, snapshot_product_code=code,
                             customer_document_snapshot_json=snapshot))
            db.flush()
            db.add(DeliveryItem(id=did, delivery_id=147, order_item_id=oid,
                                delivered_quantity=50, customer_document_snapshot_json=snapshot))
        db.commit()
    engine.dispose()
    with sqlite3.connect(path) as db:
        yield db


def test_fixed_scope_repair_preserves_all_other_facts_and_is_idempotent(repair_db):
    db = repair_db
    before = protected_facts(db)
    plan = build_plan(db)
    result = apply_plan(db, plan['sha256'], {'isolated_rehearsal': True})
    assert result['snapshots_changed'] == 4 and result['delivery_version'] == 2
    assert protected_facts(db) == before
    for change in plan['changes']:
        value = db.execute(f'SELECT {SNAPSHOT} FROM {change["table"]} WHERE id=?', (change['id'],)).fetchone()[0]
        assert value == change['after_snapshot']
        assert json.loads(value)['customer_model'] == 'ORIGINAL-MODEL'
    assert apply_plan(db, plan['sha256'], {})['status'] == 'already_applied'
    assert db.execute('SELECT COUNT(*) FROM operation_logs WHERE batch_id=?', (BATCH,)).fetchone()[0] == 1
    with pytest.raises(ValueError, match='another plan'):
        apply_plan(db, 'different', {})


@pytest.mark.parametrize('sql', [
    "UPDATE sales_deliveries SET version=2 WHERE id=147",
    "UPDATE sales_deliveries SET customer_id=138 WHERE id=147",
    "UPDATE products SET customer_drawing_number='NEW' WHERE id=3428",
    'UPDATE sales_order_items SET quantity=999 WHERE id=10487',
])
def test_stale_plan_or_scope_change_cannot_write(repair_db, sql):
    db = repair_db
    plan = build_plan(db)
    db.execute(sql); db.commit()
    before = protected_facts(db)
    snapshots = list(db.execute(f'SELECT id,{SNAPSHOT} FROM sales_order_items ORDER BY id'))
    with pytest.raises(ValueError):
        apply_plan(db, plan['sha256'], {})
    assert protected_facts(db) == before
    assert list(db.execute(f'SELECT id,{SNAPSHOT} FROM sales_order_items ORDER BY id')) == snapshots
    assert not db.execute('SELECT 1 FROM operation_logs').fetchall()


def test_late_failure_rolls_back_all_four_snapshots_and_version(repair_db):
    db = repair_db
    plan = build_plan(db)
    db.execute("CREATE TRIGGER deny_repair BEFORE INSERT ON operation_logs BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END")
    db.commit()
    with pytest.raises(sqlite3.IntegrityError, match='audit unavailable'):
        apply_plan(db, plan['sha256'], {})
    assert build_plan(db) == plan
    assert not db.in_transaction


def test_unapproved_side_effect_rolls_back_repair_and_audit(repair_db):
    db = repair_db
    plan = build_plan(db)
    db.execute("CREATE TRIGGER unexpected_change AFTER INSERT ON operation_logs BEGIN UPDATE products SET product_name='changed' WHERE id=3428; END")
    db.commit()
    before = protected_facts(db)
    with pytest.raises(ValueError, match='Unapproved business facts'):
        apply_plan(db, plan['sha256'], {})
    assert build_plan(db) == plan and protected_facts(db) == before
    assert not db.execute('SELECT 1 FROM operation_logs').fetchall()
