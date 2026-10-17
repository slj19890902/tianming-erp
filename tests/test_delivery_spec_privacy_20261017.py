from types import SimpleNamespace as NS
from copy import deepcopy

import pytest

from app.services.product_specification import delivery_specification
from app.services.delivery_current_presentation import compact_layout
from app.services.delivery_print_templates import default_layout


def product(**changes):
    return NS(**{**dict(length_mm=None, width_mm=None, height_mm=None,
        supply_mode='corrugated_production', external_packaging_specification_summary=None,
        sheet_cutting_settings=None, mold_tool_id=4,
        mold_tool=NS(id=4, is_active=True, deleted_at=None, archive_status='active', rack_location='R04'),
        product_name='纸箱 999×888', report_length_mm=900, report_width_mm=800), **changes})


def test_die_cut_delivery_uses_finished_size_never_tool_location_or_up_count(monkeypatch):
    monkeypatch.setattr('app.services.sheet_cutting_settings.component_settings',
                        lambda value: NS(is_die_cut=True, mold_count=4))
    assert delivery_specification(product(length_mm=430, width_mm=718)) == '430×718mm'
    assert delivery_specification(product()) == ''


@pytest.mark.parametrize('summary', [None, '', '待完善', '外购包材规格待完善', '规格未登记', '-'])
def test_missing_external_product_spec_is_blank(summary):
    assert delivery_specification(product(supply_mode='external_purchase',
        external_packaging_specification_summary=summary)) == ''


def test_registered_external_product_spec_is_preserved():
    assert delivery_specification(product(supply_mode='external_purchase',
        external_packaging_specification_summary='护角 40×40×3mm')) == '护角 40×40×3mm'


def test_generic_code_and_unit_have_room_without_changing_table_width_or_snapshot():
    original = {'layout': default_layout(), 'layout_hash': 'a'*64}
    frozen = deepcopy(original)
    layout = compact_layout(original)['layout']
    widths = {c['key']:c['width'] for c in layout['columns']}
    # 13pt digits: two half-em characters require about 4.6mm on a 188mm table.
    assert (widths['product_code'] - original['layout']['column_widths']['product_code']) * 188 / 100 >= 4.59
    # Two 11pt Chinese characters plus both horizontal cell paddings.
    assert widths['unit'] * 188 / 100 >= 2 * 11 * 25.4 / 72 + 1.2
    assert sum(widths.values()) == pytest.approx(100)
    assert original == frozen


def test_current_print_clears_unresolved_old_spec_without_rewriting_frozen_data(monkeypatch):
    from unittest.mock import Mock
    from app.services import delivery_current_presentation as service
    monkeypatch.setattr(service, 'current_descriptions', lambda *args: {})
    db = Mock()
    db.scalars.return_value = []
    result = {'print_template': {'layout':default_layout()}, 'sender':{},
              'items':[{'delivery_item_id':1, 'specification':'R04 · 4模',
                        'actual_goods_lines':[{'specification':'待完善', 'quantity':7}]}],
              'actual_goods_items':[{'delivery_item_id':1, 'specification':'R04 · 4模', 'quantity':7}]}
    out = service.prepare_current_print(db, NS(id=1, customer_id=2), result)
    assert out['items'][0]['specification'] == ''
    assert out['items'][0]['actual_goods_lines'][0] == {'specification':'', 'quantity':7}
    assert out['actual_goods_items'][0]['specification'] == ''
    assert out['actual_goods_items'][0]['quantity'] == 7


def test_bom_component_current_size_replaces_production_description_in_all_projections():
    from unittest.mock import Mock
    from app.services.delivery_current_presentation import present_component_descriptions
    p = product(id=8, length_mm=120, width_mm=80, customer_drawing_number='DRAW-8')
    db = Mock(); db.scalars.return_value = [p]
    child = {'component_product_id':8, 'component_snapshot_id':11, 'specification':'R04 · 4模', 'quantity':3}
    missing = {'line_type':'component', 'component_product_id':9, 'specification':'待完善', 'quantity':2}
    items = [{'component_lines':[deepcopy(child)], 'actual_goods_lines':[deepcopy(child), missing]}]
    top = [deepcopy(child)]
    present_component_descriptions(db, NS(customer_id=2), items, top)
    assert items[0]['component_lines'][0]['specification'] == '120×80mm'
    assert items[0]['actual_goods_lines'][0]['specification'] == '120×80mm'
    assert top[0]['specification'] == '120×80mm'
    assert missing['specification'] == ''
    assert top[0]['quantity'] == 3
