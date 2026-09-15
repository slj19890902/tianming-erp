from decimal import Decimal
from types import SimpleNamespace as NS
import json
import pytest


def test_order_delivery_freezes_unit_and_sales_basis():
    from app.services.delivery_snapshots import build_order_delivery_snapshot
    product=NS(product_code='P',product_name='箱',unit='只',length_mm=1,width_mm=1,height_mm=1)
    db=NS(get=lambda *a:product)
    item=NS(id=9,product_id=1,snapshot_product_code='P',snapshot_product_name='箱',snapshot_spec='1×1×1',
        sales_unit_snapshot='套',unit_price=Decimal('5'),price_tax_mode_snapshot='tax_exclusive',tax_rate_snapshot=Decimal('.13'))
    row=build_order_delivery_snapshot(db,item)
    assert row['unit_snapshot']=='套'
    assert json.loads(row['sales_contract_json'])['unit_price']=='5'
    from app.services.customer_delivery_margin import _sales_projection
    delivery=NS(delivered_quantity=2,unit_snapshot='套',sales_contract_json=row['sales_contract_json'])
    item.unit_price=Decimal('99')
    assert _sales_projection(delivery,item)['amount']==Decimal('11.30')


def test_unordered_sales_contract_is_complete_and_strict():
    from app.services.delivery_snapshots import sales_contract
    from app.services.customer_delivery_margin import _sales_projection
    frozen=sales_contract(unit='张',price=Decimal('2'),tax_mode='tax_inclusive',tax_rate=Decimal('.13'),source={'kind':'test'})
    row=NS(delivered_quantity=3,unit_snapshot='张',unit_price_snapshot=Decimal('99'),sales_contract_json=frozen)
    assert _sales_projection(row,None)['amount']==Decimal('6')
    with pytest.raises(ValueError):
        sales_contract(unit='',price=Decimal('2'),tax_mode='tax_inclusive',tax_rate=None,source={})
    with pytest.raises(ValueError):
        sales_contract(unit='只',price=Decimal('2'),tax_mode='tax_exclusive',tax_rate=None,source={})


def test_invalid_contract_does_not_fall_back_to_current_order():
    from app.services.customer_delivery_margin import _sales_projection
    row=NS(delivered_quantity=3,sales_contract_json='{"unit":"只","currency":"USD"}',unit_snapshot='只')
    order=NS(unit_price=Decimal('99'),price_tax_mode_snapshot='tax_inclusive',tax_rate_snapshot=Decimal('.13'))
    assert _sales_projection(row,order)['amount'] is None
    assert _sales_projection(row,order)['reasons']=={'invalid_sales_contract'}


def test_derived_cost_cycles_and_malformed_detail_fail_closed():
    from app.services.derived_inventory_cost import derived_cost
    lot=NS(id=8,cost_snapshot_detail_json='not-json',cost_snapshot_source='stock_preparation')
    assert derived_cost(None,lot,{8})[0] is None
    assert derived_cost(None,lot,set())[0] is None


def test_statement_terms_do_not_reprice_an_existing_delivery():
    from app.services.delivery_snapshots import statement_sales_terms,sales_contract
    item=NS(sales_contract_json=sales_contract(unit='张',price='3',tax_mode='tax_exclusive',tax_rate='.13',source={}))
    assert statement_sales_terms(item,Decimal('99'),'tax_inclusive',Decimal('.09')) == (Decimal('3'),'tax_exclusive',Decimal('.13'))


def test_bom_does_not_accept_a_bare_number_as_confirmed_input_cost():
    from app.services.bom_subkit_costs import source_cost
    from app.services.bom_subkits import SubkitError
    lot=NS(id=99,lot_number='legacy',inventory_type='finished',source_ref_type=None,
           estimated_unit_cost_snapshot=Decimal('3'),cost_snapshot_source=None,cost_snapshot_detail_json=None,
           finished_detail=None)
    with pytest.raises(SubkitError,match='成本'):
        source_cost(None,lot,2)
