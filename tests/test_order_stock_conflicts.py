import json

from fastapi.testclient import TestClient

from app.models.product import Product
from app.models.warehouse_inventory import InventoryLot
from app.services.order_stock_conflicts import parent_stock_conflicts
from app.services.warehouse_inventory import finished_inventory_candidates_for_product
from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login
from tests.test_multilevel_bom_receipt_flow import seed_graph
from tests.test_yl_bom_deduction_20261013 import legacy_parent_lot


def test_old_single_piece_stock_is_explained_but_not_reserved_as_sets(composite_requisition_app):
    app, factory = composite_requisition_app
    seed_graph(factory, quantity=300)
    lid = legacy_parent_lot(factory)
    with factory() as db:
        lot = db.get(InventoryLot, lid)
        detail = lot.finished_detail
        old = json.loads(detail.physical_basis_json)
        old['unit'] = '只'; old['assembly'] = []
        detail.physical_basis_json = json.dumps(old)
        db.commit()
        conflicts = parent_stock_conflicts(db, db.get(Product, 1))
        assert len(conflicts) == 1
        assert conflicts[0]['quantity'] == 810 and conflicts[0]['unit'] == '只'
        assert conflicts[0]['location'] and '组装关系' in conflicts[0]['reason']
        assert finished_inventory_candidates_for_product(db, customer_id=1, product_id=1) == []
        assert not db.new and not db.dirty
    with TestClient(app) as client:
        _login(client)
        result = client.post('/api/orders/inventory-draft-preview', json={
            'customer_id':1, 'items':[{'client_line_id':'old-stock','product_id':1,'quantity':300}]})
        assert result.status_code == 200, result.text
        row = result.json()['items'][0]
        assert row['finished_candidate_available_quantity'] == 0
        assert row['finished_stock_identity_conflicts'][0]['quantity'] == 810
    with factory() as db:
        lot=db.get(InventoryLot,lid)
        assert (lot.quantity_available,lot.quantity_reserved)==(810,0)
        # Cross-customer facts are not exposed by the explanatory projection.
        from app.models.customer import Customer
        other = Customer(customer_number=2, customer_code='OTHER', name='Other')
        db.add(other); db.flush()
        lot.finished_detail.owner_customer_id = other.id
        db.flush()
        assert parent_stock_conflicts(db,db.get(Product,1)) == []
        db.rollback()
