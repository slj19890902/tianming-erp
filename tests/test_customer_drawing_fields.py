import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest


@pytest.mark.parametrize('preset', ['yke', 'kew', 'yl'])
@pytest.mark.parametrize('override', ['', '   ', 'OTHER-DRAWING'])
def test_retired_overrides_never_hide_or_replace_frozen_drawing(preset, override):
    from app.services.customer_delivery_print import enrich_customer_print
    from app.services.customer_delivery_templates import preset_layout
    identity = {'schema_version': 1, 'customer_material_code': '82020083',
                'customer_drawing_number': '0632264', 'customer_category': 'A',
                'customer_model': 'KCX', 'customer_drawing_display': override,
                'customer_product_name': 'RETIRED-NAME'}
    record = SimpleNamespace(id=1, customer_document_snapshot_json=json.dumps(identity))
    db = Mock(); db.scalar.return_value = None; db.scalars.return_value = [record]
    data = {'id': 1, 'delivery_number': 'TEST', 'delivery_date': '2026-10-07',
            'vehicle_number': '', 'sender': {}, 'customer': {},
            'print_template': {'layout': preset_layout(preset)},
            'items': [{'delivery_item_id': 1, 'product_code': '82020083',
                       'product_name': '冻结产品名称', 'quantity': 50}]}
    with patch('app.api.orders._can_view_order_sales_amount', return_value=False):
        result = enrich_customer_print(db, SimpleNamespace(id=1), data, SimpleNamespace())
    row = result['customer_document_rows'][0]
    assert row['customer_drawing_number'] == '0632264'
    assert row['customer_product_name'] == '冻结产品名称'
    assert row['customer_category'] == 'A' and row['customer_model'] == 'KCX'
    assert row['quantity'] == 50 and 'unit_price' not in row
    assert json.loads(record.customer_document_snapshot_json) == identity


def test_new_snapshot_and_stale_client_write_exclude_retired_fields():
    from app.api.products import ProductPayload, _product_write_data
    from app.services.customer_document_fields import document_snapshot
    product = SimpleNamespace(id=1, customer_id=137, customer_material_code='82020083',
                              customer_drawing_number='0632264', customer_category='A',
                              customer_model='KCX', customer_drawing_display='',
                              customer_product_name='RETIRED', remark='')
    snapshot = document_snapshot(product)
    assert snapshot['customer_drawing_number'] == '0632264'
    assert 'customer_drawing_display' not in snapshot
    assert 'customer_product_name' not in snapshot
    payload = ProductPayload(customer_id=137, product_code='82020083',
                             customer_material_code='82020083', product_name='产品名称',
                             box_category='normal', customer_drawing_number='0632264',
                             customer_drawing_display='HIDE', customer_product_name='STALE')
    with patch('app.api.products.has_permission', return_value=True):
        fields = _product_write_data(payload, SimpleNamespace())
    assert fields['customer_drawing_number'] == '0632264'
    assert 'customer_drawing_display' not in fields and 'customer_product_name' not in fields


def test_common_box_editor_exposes_only_three_customer_identity_inputs():
    html = (Path(__file__).resolve().parents[1] / 'static/index.html').read_text(encoding='utf-8')
    editor = html.split('客户送货资料 · 图号／类别／使用型番', 1)[1].split('</details>', 1)[0]
    save = html.split('_productFormSaveFields() {', 1)[1].split('material_id: f.material_id', 1)[0]
    for field in ['customer_drawing_number', 'customer_category', 'customer_model']:
        assert field in editor and field in save
    for field in ['customer_product_name', 'customer_drawing_display']:
        assert field not in editor and field not in save


@pytest.mark.parametrize('drawing', [None, '', '   '])
def test_missing_real_drawing_reports_exact_row_and_code(drawing):
    from app.services.customer_delivery_print import enrich_customer_print
    from app.services.customer_delivery_templates import preset_layout
    record = SimpleNamespace(id=1, customer_document_snapshot_json=json.dumps({
        'schema_version': 1, 'customer_material_code': '80040986', 'customer_drawing_number': drawing}))
    db = Mock(); db.scalar.return_value = None; db.scalars.return_value = [record]
    data = {'id': 1, 'delivery_number': 'TEST', 'delivery_date': '2026-10-07',
            'vehicle_number': '', 'sender': {}, 'customer': {},
            'print_template': {'layout': preset_layout('yke')},
            'items': [{'delivery_item_id': 1, 'product_code': '80040986', 'quantity': 50}]}
    with patch('app.api.orders._can_view_order_sales_amount', return_value=False):
        result = enrich_customer_print(db, SimpleNamespace(id=1), data, SimpleNamespace())
    assert result['document_warnings'] == ['第1行（80040986）客户图号未填写，请核对原单据']
