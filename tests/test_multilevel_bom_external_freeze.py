import json
from decimal import Decimal
import pytest
from sqlalchemy import select, func

from app.models.product import Product
from app.models.multilevel_bom import OrderBomGraph, OrderBomExternalComponent
from app.models.product_bom import SalesOrderItemBomComponent
from app.models.order_external_packaging import SalesOrderItemExternalComponent, SalesOrderItemExternalComponentCandidate
from app.services.multilevel_bom_external_freeze import freeze_order_procurement
from app.services.external_packaging_purchase import _graph_purchase_quantities
from app.services.multilevel_bom_plan import BomPlanError
from tests.test_multilevel_bom_orders import context
from tests.test_multilevel_bom_master import save


def configure(db, actor, *, basis=1, two=False):
    for pid in ([3,4] if two else [3]):
        p = db.get(Product, pid)
        p.supply_mode = 'external_purchase'
        p.external_packaging_category_code = 'other_packaging'
        p.external_packaging_specification_json = '{"size":"test"}'
        p.external_packaging_specification_summary = '隔离规格'
        p.external_packaging_purchase_unit = '片'
        p.external_packaging_default_order_quantity_basis = basis
        p.external_packaging_default_purchase_quantity_basis = 1
        p.external_packaging_candidate_snapshot_json = json.dumps([dict(external_product_id=pid+100,
            external_product_version=1, supplier_id=7, supplier_name='候选供应商', product_name='包材',
            supplier_product_code='TEST', purchase_unit='片', is_default=True, customer_scope_id=136)])
    save(db, actor, 1, 'assembled', [(pid,2,'assembly') for pid in ([3,4] if two else [3])])
    db.commit()


def test_all_nodes_generate_stable_distinct_procurement_sources(context):
    db, actor, item, _ = context
    configure(db, actor, two=True)
    freeze_order_procurement(db, order_item_id=item.id, actor=actor)
    db.commit()
    links = list(db.scalars(select(OrderBomExternalComponent).order_by(OrderBomExternalComponent.product_id)))
    assert [l.product_id for l in links] == [3,4]
    rows = list(db.scalars(select(SalesOrderItemExternalComponent).order_by(SalesOrderItemExternalComponent.display_order)))
    assert [r.display_order for r in rows] == [1,2]
    assert all('每父件 2' in r.conversion_basis and '库存' in r.conversion_basis for r in rows)
    assert _graph_purchase_quantities(db, rows, {item.id:item}) == {r.id:Decimal(200) for r in rows}
    p = db.get(Product, 3)
    p.version += 1
    p.external_packaging_candidate_snapshot_json = '[]'
    db.commit()
    freeze_order_procurement(db, order_item_id=item.id, actor=actor)
    assert db.scalar(select(func.count()).select_from(SalesOrderItemExternalComponentCandidate)) == 2


def test_repeating_ratio_does_not_overbuy_at_exact_boundary(context):
    db, actor, item, _ = context
    configure(db, actor, basis=3)
    item.quantity = 3
    db.commit()
    freeze_order_procurement(db, order_item_id=item.id, actor=actor)
    db.commit()
    row = db.scalar(select(SalesOrderItemExternalComponent))
    assert row.quantity_per_finished_unit == Decimal('0.666667')
    assert _graph_purchase_quantities(db, [row], {item.id:item}) == {row.id:Decimal(2)}


@pytest.mark.parametrize('field,value', [('purchase_unit','箱'), ('customer_scope_id',999), ('supplier_id',0), ('supplier_id',True), ('is_default','true')])
def test_bad_candidate_rolls_back_graph_material_and_procurement(context, field, value):
    db, actor, item, _ = context
    configure(db, actor, two=True)
    p = db.get(Product, 4)
    candidates = json.loads(p.external_packaging_candidate_snapshot_json)
    candidates[0][field] = value
    p.external_packaging_candidate_snapshot_json = json.dumps(candidates)
    db.commit()
    with pytest.raises(BomPlanError):
        freeze_order_procurement(db, order_item_id=item.id, actor=actor)
    for model in (OrderBomGraph, SalesOrderItemBomComponent, SalesOrderItemExternalComponent,
                  SalesOrderItemExternalComponentCandidate, OrderBomExternalComponent):
        assert db.scalar(select(func.count()).select_from(model)) == 0
