from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select,func

from test_n040_material_candidates import n040_app, _login, _login_scoped


def trace(ids, **updates):
    value = dict(source_sha256='a'*64, source_name='采购单.xlsx', source_lineage='purchase-ledger-2020-2026',
                 sheet='2020.1-2026', row=22569, product_id=ids['product'], customer_id=ids['customer'],
                 source_date_label='2026-09-08', report_dimensions_text='58*83.5', crease_text='净',
                 material_text='N040-A-3B/B', supplier_names=['Supplier A'], purchase_sheet_qty=500,
                 related_product_ids=[ids['product']], identity_basis='exact_customer_code')
    value.update(updates)
    return value


def actor(db):
    from app.models.user import User
    return db.scalar(select(User).where(User.username=='n040-admin'))


def test_import_is_idempotent_and_does_not_generate_procurement_or_stock(n040_app):
    from app.models import Base
    from app.services.purchase_workbook_history import record_trace
    app, ids, factory = n040_app
    tables=['sales_orders','sales_order_items','supplier_requisition_orders','supplier_requisition_order_items','incoming_receipts','inventory_lots']
    with factory() as db:
        counts={name:db.scalar(select(func.count()).select_from(Base.metadata.tables[name])) for name in tables}
        first=record_trace(db,trace(ids),actor=actor(db),batch_id='trace-test');db.commit()
        replay=record_trace(db,trace(ids),actor=actor(db),batch_id='trace-test');db.commit()
        assert first['created'] and not replay['created'] and first['id']==replay['id']
        # An unchanged row in a resaved/copied workbook stays the same trace.
        copied=record_trace(db,trace(ids,source_sha256='b'*64,source_name='采购单-副本.xlsx'),actor=actor(db),batch_id='trace-copy');db.commit()
        assert copied==replay
        with pytest.raises(ValueError,match='内容已变化'):
            record_trace(db,trace(ids,purchase_sheet_qty=501),actor=actor(db),batch_id='trace-test')
        db.rollback()
        assert counts=={name:db.scalar(select(func.count()).select_from(Base.metadata.tables[name])) for name in tables}
    with TestClient(app) as client:
        _login(client)
        payload=client.get(f"/api/requisition/products/{ids['product']}/material-context").json()
        external=[r for r in payload['material_history'] if r['source_type']=='purchase_workbook']
        assert len(external)==1 and external[0]['source_purchase_sheet_qty']=='500'
        assert external[0]['document_date'] is None and not external[0]['is_formal_requisition']
        assert payload['requisition_history']==[] and payload['manual_selection_history']==[]
        assert all(c.get('history_count',0)==0 for c in payload['candidates'])


def test_history_scope_price_annotations_and_pagination(n040_app):
    from app.services.purchase_workbook_history import record_trace
    app,ids,factory=n040_app
    with factory() as db:
        for i in range(3):record_trace(db,trace(ids,row=22569+i,material_text='AAA/B 单价3.5元'),actor=actor(db),batch_id='trace-test')
        record_trace(db,trace(ids,product_id=ids['other_product'],customer_id=ids['other_customer'],related_product_ids=[ids['other_product']]),actor=actor(db),batch_id='trace-test');db.commit()
    with TestClient(app) as client:
        _login_scoped(client)
        first=client.get(f"/api/requisition/products/{ids['product']}/purchase-traces?limit=2")
        assert first.status_code==200,first.text
        assert all('3.5' not in row['material_code'] for row in first.json()['items'])
        page=first.json();assert len(page['items'])==2 and page['has_more']
        with factory() as db:
            record_trace(db,trace(ids,row=22500),actor=actor(db),batch_id='late-old-row');db.commit()
        second=client.get(f"/api/requisition/products/{ids['product']}/purchase-traces",params={'before_id':page['next_before_id'],'limit':2}).json()
        assert len(second['items'])==2 and not second['has_more']
        assert [x['source_row'] for x in second['items']]==[22569,22500]
        assert {x['document_id'] for x in page['items']}.isdisjoint({x['document_id'] for x in second['items']})
        assert client.get(f"/api/requisition/products/{ids['other_product']}/purchase-traces").status_code==403


def test_shared_row_quantity_is_reference_total_and_import_rolls_back(n040_app):
    from app.services.purchase_workbook_history import record_trace,history_page
    from app.models.product import Product
    app,ids,factory=n040_app
    related=[ids['product'],ids['same_customer_product']]
    with factory() as db:
        record_trace(db,trace(ids,related_product_ids=related),actor=actor(db),batch_id='trace-test')
        db.rollback()
        assert history_page(db,db.get(Product,ids['product']),can_view_costs=True)['items']==[]
        record_trace(db,trace(ids,related_product_ids=related),actor=actor(db),batch_id='trace-test');db.commit()
        row=history_page(db,db.get(Product,ids['product']),can_view_costs=True)['items'][0]
        assert row['shared_source_row'] and row['source_purchase_sheet_qty']=='500'
        assert 'requisition_qty' not in row and 'quantity' not in row
        with pytest.raises(ValueError,match='混合客户'):
            record_trace(db,trace(ids,row=22570,related_product_ids=[ids['product'],ids['other_product']]),actor=actor(db),batch_id='trace-test')
        db.rollback()
