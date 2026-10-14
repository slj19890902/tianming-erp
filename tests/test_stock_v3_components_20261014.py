from datetime import date
from decimal import Decimal

import pytest

from tests.test_inventory_cost_snapshot import db, _customer, _material
from tests.test_bom_entry_cost import seed
from app.models.product import Product
from app.services.inventory_cost_snapshot import estimate_finished_product_cost
from app.services.inventory_valuation import resolve_product_cost
from app.services.sheet_cutting_settings import SheetCuttingSettings
from app.services.warehouse_inventory import manual_finished_in


@pytest.mark.parametrize('style', ['独立底', '独立天盖', 'TDGD 天地盖底', 'TDGG 天地盖盖'])
def test_standalone_component_ignores_retired_base_dimensions(db, style):
    customer = _customer(db)
    material = _material(db, code='K8K', price='2')
    product = Product(customer_id=customer.id, product_code='000120', customer_material_code='000120', product_name='下盖',
        box_style=style, material_id=material.id, layer_count=3, flute_type='B',
        report_length_mm=890, report_width_mm=890,
        base_report_length_mm=845, base_report_width_mm=845, pieces_per_box=1,
        sheet_cutting_settings={'schema_version': 2, 'whole': SheetCuttingSettings().to_dict()})
    db.add(product); db.flush()
    result = resolve_product_cost(db, product, for_entry=True)
    assert not result.missing
    assert result.estimate.unit_cost == Decimal('1.5842')
    assert [part['component'] for part in result.estimate.detail['components']] == ['whole']
    assert product.base_report_length_mm == 845
    product.sheet_cutting_settings = None
    assert estimate_finished_product_cost(db, product=product).unit_cost == Decimal('1.5842')


def test_parent_stocktake_cost_uses_single_sheet_children_and_freezes_history(db):
    parent, children, material, location = seed(db)
    material.quote_price = Decimal('2')
    for child in children:
        child.box_style = '独立底'
        child.report_length_mm = child.report_width_mm = 890
        child.base_report_length_mm = child.base_report_width_mm = 845
        child.default_cutting_mode = '一开一'
        child.sheet_cutting_settings = {'schema_version': 2, 'whole': SheetCuttingSettings().to_dict()}
    lot = manual_finished_in(db, customer_id=parent.customer_id, product_id=parent.id,
        location_id=location.id, quantity=50, stock_date=date(2026,10,14), source_type='stocktake',
        remarks=None, operator_id=None, idempotency_key='v3-components-parent')
    assert lot.estimated_unit_cost_snapshot == Decimal('1.5842') * 15
    material.quote_price = Decimal('9')
    assert lot.estimated_unit_cost_snapshot == Decimal('1.5842') * 15


def test_component_contract_conflict_identifies_product_for_entry(db):
    customer = _customer(db)
    material = _material(db, code='K8K', price='2')
    product = Product(customer_id=customer.id, product_code='BAD-COMPONENT', customer_material_code='BAD-COMPONENT', product_name='错误独立底',
        box_style='独立底', material_id=material.id, layer_count=3, flute_type='B',
        report_length_mm=890, report_width_mm=890, pieces_per_box=1,
        sheet_cutting_settings={'schema_version': 2, 'cover': SheetCuttingSettings().to_dict(),
                               'base': SheetCuttingSettings().to_dict()})
    db.add(product); db.flush()
    result = resolve_product_cost(db, product, for_entry=True)
    assert result.estimate is None
    assert any('BAD-COMPONENT' in message and '常用箱' in message for message in result.missing)
