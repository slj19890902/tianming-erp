"""Existing safe API supports saved-draft append and withdrawal before new stock."""
from fastapi.testclient import TestClient
from tests.test_delivery_physical_quantities import seed_physical
from tests.test_p1_15b_unordered_finished_delivery import unordered_finished_delivery_app, _add_finished, _login
from tests.test_delivery_dispatch_commands import command
from app.models.warehouse_inventory import InventoryLot


def test_saved_append_then_dispatch_withdraw_append_and_redispatch(unordered_finished_delivery_app):
    app, factory = unordered_finished_delivery_app
    cid, pid, lot1, _ = seed_physical(factory, 60, external=False)
    lot2, _ = _add_finished(factory, customer_id=cid, product_id=pid, quantity=30, key='append-after-save')
    def payload(version, allocations):
        return dict(source_mode='unordered_finished', expected_version=version,
                    lines=[dict(source_type='finished_stock', product_id=pid,
                                delivered_quantity=sum(q for _, q in allocations), unit_price='3.60',
                                allocations=[dict(inventory_lot_id=lot, quantity=q) for lot, q in allocations])])
    def available():
        with factory() as db:
            return tuple(db.get(InventoryLot, lot).quantity_available for lot in (lot1, lot2))
    with TestClient(app) as c:
        _login(c)
        body=payload(1, [(lot1, 10)]);body['customer_id']=cid
        created=c.post('/api/deliveries',json=body);assert created.status_code==201,created.text
        did=created.json()['id']
        saved=c.put(f'/api/deliveries/{did}',json=payload(1,[(lot1,10),(lot2,5)]))
        assert saved.status_code==200,saved.text
        assert available()==(60,30)
        sent=c.put(f'/api/deliveries/{did}/dispatch',json=command(c,did,'append-dispatch-first'))
        assert sent.status_code==200,sent.text
        assert available()==(50,25)
        blocked=c.put(f'/api/deliveries/{did}',json=payload(c.get(f'/api/deliveries/{did}').json()['version'],[(lot1,10),(lot2,10)]))
        assert blocked.status_code==409,blocked.text
        assert available()==(50,25)
        withdraw_body=dict(expected_version=c.get(f'/api/deliveries/{did}').json()['version'],idempotency_key='append-withdraw-first');withdrawn=c.put(f'/api/deliveries/{did}/withdraw',json=withdraw_body);assert withdrawn.status_code==200,withdrawn.text
        assert available()==(60,30)
        assert c.put(f'/api/deliveries/{did}/withdraw',json=withdraw_body).json()==withdrawn.json()
        assert c.put(f'/api/deliveries/{did}/withdraw',json={**withdraw_body,'expected_version':withdraw_body['expected_version']+1}).status_code==409
        assert available()==(60,30)
        edited=c.put(f'/api/deliveries/{did}',json=payload(withdrawn.json()['version'],[(lot1,10),(lot2,10)]))
        assert edited.status_code==200,edited.text
        assert edited.json()['id']==did and edited.json()['total_quantity']==20
        assert available()==(60,30)
        cmd=command(c,did,'append-dispatch-second')
        sent=c.put(f'/api/deliveries/{did}/dispatch',json=cmd);assert sent.status_code==200,sent.text
        assert available()==(50,20)
        assert c.put(f'/api/deliveries/{did}/dispatch',json=cmd).json()==sent.json()
        assert available()==(50,20)


def test_order_draft_can_add_inventory_then_withdraw_mixed_document(unordered_finished_delivery_app):
    from tests.test_p1_15b_unordered_finished_delivery import _seed, _mixed_delivery_fixture, _mixed_payload
    from app.models.order import OrderItem
    from sqlalchemy import select
    from app.models.delivery import DeliveryItem
    app,factory=unordered_finished_delivery_app
    fixture=_seed(app,factory)
    with TestClient(app) as c:
        _login(c)
        _,oid,order_lot=_mixed_delivery_fixture(c,factory,fixture)
        original=dict(customer_id=fixture.customer_a_id,source_mode='order',items=[dict(order_item_id=oid,delivered_quantity=10)])
        created=c.post('/api/deliveries',json=original);assert created.status_code==201,created.text
        did=created.json()['id']
        mixed=_mixed_payload(fixture,order_item_id=oid,order_quantity=10,unordered_quantity=3)
        mixed.pop('customer_id');mixed['expected_version']=created.json()['version'];mixed.pop('delivery_date')
        edited=c.put(f'/api/deliveries/{did}',json=mixed);assert edited.status_code==200,edited.text
        sent=c.put(f'/api/deliveries/{did}/dispatch',json=command(c,did,'mixed-withdraw-first'));assert sent.status_code==200,sent.text
        with factory() as db:
            before_ids=list(db.scalars(select(DeliveryItem.id).where(DeliveryItem.delivery_id==did,DeliveryItem.is_current.is_(True))))
            assert db.get(OrderItem,oid).delivered_quantity==10
        body=dict(expected_version=sent.json()['dispatch_receipt']['completed_version'],idempotency_key='mixed-withdraw-once')
        withdrew=c.put(f'/api/deliveries/{did}/withdraw',json=body);assert withdrew.status_code==200,withdrew.text
        assert withdrew.json()['status']=='pending'
        with factory() as db:
            assert db.get(OrderItem,oid).delivered_quantity==0
            assert db.get(InventoryLot,order_lot).quantity_available==0  # restored to order reservation
            assert db.get(InventoryLot,order_lot).quantity_reserved==10
            assert all(not db.get(DeliveryItem,i).is_current for i in before_ids)
        again=c.put(f'/api/deliveries/{did}/dispatch',json=command(c,did,'mixed-withdraw-resend'))
        assert again.status_code==200,again.text
        with factory() as db:assert db.get(OrderItem,oid).delivered_quantity==10


def test_withdraw_version_receipt_and_atomic_rollback(unordered_finished_delivery_app,monkeypatch):
    from tests.test_p1_15b_unordered_finished_delivery import _seed, _unordered_payload
    from tests.test_delivery_dispatch_commands import state
    from app.api import deliveries
    from app.models.finance import ReturnReceipt
    from datetime import date
    app,factory=unordered_finished_delivery_app
    fixture=_seed(app,factory)
    with TestClient(app,raise_server_exceptions=False) as c:
        _login(c)
        created=c.post('/api/deliveries',json=_unordered_payload(fixture));assert created.status_code==201,created.text
        did=created.json()['id'];sent=c.put(f'/api/deliveries/{did}/dispatch',json=command(c,did,'withdraw-gates-dispatch'));assert sent.status_code==200,sent.text
        body=dict(expected_version=sent.json()['dispatch_receipt']['completed_version'],idempotency_key='withdraw-gates-command')
        before=state(factory)
        assert c.put(f'/api/deliveries/{did}/withdraw',json={**body,'expected_version':1}).status_code==409
        assert state(factory)==before
        with monkeypatch.context() as patch:
            patch.setattr(deliveries,'_delivery_finance_chain_block_reason',lambda *args: '已进入对账，不能撤回发货')
            assert c.put(f'/api/deliveries/{did}/withdraw',json=body).status_code==409
        assert state(factory)==before
        for target in ('_update_delivery','_record_delivery_idempotency'):
            with monkeypatch.context() as patch:
                def fail(*args,**kwargs):raise RuntimeError('failure after exact inventory reversal')
                patch.setattr(deliveries,target,fail)
                assert c.put(f'/api/deliveries/{did}/withdraw',json=body).status_code==500
            assert state(factory)==before
        with monkeypatch.context() as patch:
            from fastapi import HTTPException
            def deny(*args,**kwargs):raise HTTPException(status_code=403,detail='customer outside permitted scope')
            patch.setattr(deliveries,'require_customer_access',deny)
            assert c.put(f'/api/deliveries/{did}/withdraw',json=body).status_code==403
        assert state(factory)==before
        with factory() as db:
            db.add(ReturnReceipt(delivery_id=did,status='confirmed',actual_received_date=date(2026,10,17),created_by=1));db.commit()
        assert c.put(f'/api/deliveries/{did}/withdraw',json=body).status_code==409
        assert state(factory)==before


def test_legacy_cancel_keeps_original_archive_contract(unordered_finished_delivery_app):
    from tests.test_p1_15b_unordered_finished_delivery import _seed, _unordered_payload
    app,factory=unordered_finished_delivery_app
    fixture=_seed(app,factory)
    with TestClient(app) as c:
        _login(c)
        created=c.post('/api/deliveries',json=_unordered_payload(fixture));assert created.status_code==201,created.text
        did=created.json()['id']
        sent=c.put(f'/api/deliveries/{did}/dispatch',json=command(c,did,'legacy-cancel-still-archives'));assert sent.status_code==200,sent.text
        cancelled=c.put(f'/api/deliveries/{did}/cancel');assert cancelled.status_code==200,cancelled.text
        assert cancelled.json()['status']=='voided'
