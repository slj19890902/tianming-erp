import pytest
from app.services.product_readiness import product_readiness
from tests.test_inventory_cost_snapshot import db
from tests.test_bom_entry_cost import seed


def test_inactive_bom_child_is_not_green_and_has_editable_reason(db):
    parent,children,_,_=seed(db)
    children[0].is_active=False;db.flush()
    state=product_readiness(parent)
    assert state['ready'] is False
    issue=next(i for i in state['issues'] if i['field']=='bom_components')
    assert children[0].product_code in issue['reason'] and '已停用' in issue['reason']
    assert issue['repairable'] is True and issue['repair_page']=='/?page=products'


def test_existing_assembled_stock_reference_does_not_require_new_production_mold(db):
    from app.models.order import OrderItem
    from app.services.multilevel_bom_compile import compile_master_order_bom
    from app.services.bom_entry_cost import assembled_entry_cost
    from app.core.inventory_entry_guard import _assembled_specifications
    from app.services.finished_stock_identity import product_basis
    import json
    parent,children,_,_=seed(db)
    children[0].box_category='die_cut';children[0].production_process='模切';children[0].mold_tool_id=None;db.flush()
    with pytest.raises(ValueError,match='模具'):
        compile_master_order_bom(db,OrderItem(id=0,order_id=0,product_id=parent.id,quantity=1,unit_price=0,subtotal=0))
    resolution=assembled_entry_cost(db,parent,for_entry=True)
    assert resolution.estimate is not None and not resolution.missing
    assert _assembled_specifications(resolution.estimate.detail,json.loads(product_basis(parent)))


def test_reference_cost_failure_points_to_child_and_material_field(db):
    parent,children,material,_=seed(db)
    material.quote_price=None;db.flush()
    state=product_readiness(parent)
    assert not state['ready']
    issue=next(i for i in state['issues'] if i['source']=='bom_entry_reference')
    assert issue['product_id'] in {p.id for p in children}
    assert issue['field']=='material_id' and issue['repairable']


def test_legacy_parent_delivery_keeps_own_stock_without_inventing_child_delivery_stock():
    from types import SimpleNamespace
    from app.services.order_parent_stock import parent_inventory_eligible
    product=SimpleNamespace(id=1,is_virtual_composite_parent=False,is_composite=True,composite_fulfillment_mode='parent_delivery')
    assert parent_inventory_eligible(product,profiles={1:None})
    product.composite_fulfillment_mode='component_delivery'
    assert not parent_inventory_eligible(product,profiles={1:None})
    product.composite_fulfillment_mode=None
    assert not parent_inventory_eligible(product,profiles={1:None})
    product.composite_fulfillment_mode='parent_delivery';product.is_virtual_composite_parent=True
    assert not parent_inventory_eligible(product,profiles={1:None})

def test_free_stock_assembly_does_not_inherit_obsolete_replenishment_parent_basis(db):
    import json
    from types import SimpleNamespace
    from app.services.stock_preparation_assembly import _recipe
    from app.services.finished_stock_identity import product_basis
    parent,children,_,_=seed(db)
    old={'parent_product_id':parent.id,'parent_product_version':0,'customer_id':parent.customer_id,'components':[{'product_id':p.id,'product_version':p.version,'quantity_per_set':'1'} for p in children]}
    snapshot=json.dumps({'frozen_bom_plan':old,'bom_parent_basis':'old-replenishment-parent-basis'})
    recipe=_recipe(db,parent.id,[SimpleNamespace(product_snapshot=snapshot)])
    assert recipe['parent_basis']==product_basis(parent) and recipe['version']==parent.version
    assert recipe['frozen_plan_hash'] is None and 'old-replenishment-parent-basis' in snapshot
