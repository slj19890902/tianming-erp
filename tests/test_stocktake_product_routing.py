from tests.test_inventory_cost_snapshot import db
from tests.test_bom_entry_cost import seed
from app.models.multilevel_bom import ProductBomProfile
from app.services.stocktake_product_routing import route_candidates
from tests.test_p1_47d_inventory_adjustment import stocktake_app


def candidate(parent):
    return dict(product_id=parent.id, customer_id=parent.customer_id,
                product_name=parent.product_name, product_code=parent.product_code)


def test_legacy_component_delivery_exposes_own_product_and_real_children_without_new_profile(db):
    parent, children, _, _ = seed(db)
    db.delete(db.get(ProductBomProfile, parent.id)); db.flush()
    parent.composite_fulfillment_mode = 'component_delivery'
    rows = route_candidates(db, [candidate(parent), candidate(children[0])])
    assert {r['product_id'] for r in rows} == {parent.id, *(p.id for p in children)}
    assert len(rows) == 4
    assert '本品' in rows[0]['entry_hint']
    assert db.get(ProductBomProfile, parent.id) is None
    from app.services.order_parent_stock import parent_inventory_eligible
    assert not parent_inventory_eligible(parent, db=db)


def test_virtual_and_separate_parents_route_to_children_and_do_not_enable_parent_stock(db):
    parent, children, _, _ = seed(db)
    profile = db.get(ProductBomProfile, parent.id); profile.source = 'separate'
    parent.is_virtual_composite_parent = True
    rows = route_candidates(db, [candidate(parent)])
    assert {r['product_id'] for r in rows} == {p.id for p in children}
    assert all('子件' in r['entry_hint'] for r in rows)


def test_inactive_and_cross_customer_nodes_are_not_selectable_or_leaked(db):
    parent, children, _, _ = seed(db)
    profile = db.get(ProductBomProfile, parent.id); profile.source = 'separate'
    children[0].is_active = False
    # A detached fake customer identity is enough for the read projection;
    # don't commit a graph which would violate its actual FK/contract.
    children[1].customer_id = parent.customer_id + 1000
    children[1].product_code = 'PRIVATE-CROSS-CUSTOMER-CODE'
    with db.no_autoflush:
        rows = route_candidates(db, [candidate(parent)])
    assert {r['product_id'] for r in rows} == {parent.id, children[2].id}
    blocked = next(r for r in rows if r['product_id'] == parent.id)
    assert children[0].product_code in blocked['entry_block_reason']
    assert children[1].product_code not in blocked['entry_block_reason']


def test_assembled_parent_keeps_exact_candidate(db):
    parent, _, _, _ = seed(db)
    row = candidate(parent)
    assert route_candidates(db, [row]) == [row]


def test_stocktake_api_routes_only_on_requested_operation(stocktake_app):
    from fastapi.testclient import TestClient
    from app.models.product import Product
    from app.models.product_bom import ProductBomComponent
    from tests.test_mobile_initial_stocktake import _login
    app, factory, ids, _ = stocktake_app
    with factory() as session:
        parent = Product(customer_id=ids['customer'], product_code='ROUTE-LEGACY-PARENT',
            customer_material_code='ROUTE-LEGACY-PARENT',
            product_name='只交付子件的虚拟组合', box_style='其他', unit='套',
            is_composite=True, is_virtual_composite_parent=True,
            composite_fulfillment_mode='component_delivery')
        session.add(parent); session.flush(); parent_id = parent.id
        session.add(ProductBomComponent(parent_product_id=parent.id, component_product_id=ids['product'],
            quantity_per_set=2, display_order=1, internal_component_code='ROUTE-CHILD', is_required=True))
        session.commit()
    with TestClient(app) as client:
        _login(client, 'p147d-admin')
        path = '/api/warehouse/floor3/product-candidates'
        plain = client.get(path, params={'q': 'ROUTE-LEGACY-PARENT'})
        assert plain.status_code == 200, plain.text
        assert {r['product_id'] for r in plain.json()['items']} == {parent_id}
        routed = client.get(path, params={'q': 'ROUTE-LEGACY-PARENT', 'stocktake': 'true'})
        assert routed.status_code == 200, routed.text
        assert {r['product_id'] for r in routed.json()['items']} == {ids['product']}
        assert '子件' in routed.json()['items'][0]['entry_hint']
