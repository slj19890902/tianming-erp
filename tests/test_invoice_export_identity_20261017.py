from decimal import Decimal
from io import BytesIO
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
from openpyxl import load_workbook
import pytest

from test_fin001_invoice_tasks import fin001_app, _login, _prepare_exported_task
from app.models.delivery import DeliveryItem
from app.models.order import OrderItem
from app.services.invoice_export_identity import drawing_number, export_lines
from app.services.invoice_tax_template import InvoiceTaxTemplateError


def snapshot(drawing):
    return json.dumps({'schema_version': 1, 'customer_drawing_number': drawing})


@pytest.mark.parametrize('drawing', ['0631965-1', '', None])
def test_actual_export_uses_code_and_only_frozen_drawing(fin001_app, drawing):
    app, factory = fin001_app
    with factory() as db:
        db.get(DeliveryItem, 1).customer_document_snapshot_json = snapshot(drawing)
        db.get(OrderItem, 1).customer_document_snapshot_json = snapshot('OLD-ORDER-DRAWING')
        db.commit()
    with TestClient(app) as client:
        _login(client)
        task = _prepare_exported_task(client, idempotency_key='code-drawing-export-20261017')
        before = client.get(f"/api/finance/invoice-tasks/{task['id']}").json()
        exported = client.get(f"/api/finance/invoice-tasks/{task['id']}/tax-template.xlsx")
        assert exported.status_code == 200, exported.text
        workbook = load_workbook(BytesIO(exported.content), read_only=True, data_only=True)
        sheet = workbook['1-明细模板']
        assert sheet['A4'].value == 'FIN-BOX-001'
        assert sheet['C4'].value == (drawing or None)
        assert sheet['E4'].value == 10
        assert Decimal(str(sheet['G4'].value)) == Decimal('113.00')
        assert Decimal(str(sheet['H4'].value)) == Decimal('0.13')
        workbook.close()
        after = client.get(f"/api/finance/invoice-tasks/{task['id']}").json()
        assert after['items'] == before['items']
        for field in ('net_amount', 'tax_amount', 'total_amount'):
            assert after[field] == before[field]


def test_fallback_preserves_leading_zero_and_missing_stays_blank():
    assert drawing_number(None, snapshot('0631965')) == '0631965'
    assert drawing_number(None, None) is None
    with pytest.raises(InvoiceTaxTemplateError, match='快照损坏'):
        drawing_number('{broken', snapshot('do-not-mask-corruption'))


def test_non_product_charge_preserves_all_fields():
    item = SimpleNamespace(source_type='customer_charge', statement_item_id=None,
        project_name='运费', specification='原备注', tax_classification_code='123',
        unit='次', quantity=1, unit_price=10, amount=10, tax_rate=Decimal('.13'))
    line = export_lines(None, [item])[0]
    assert line == {key: getattr(item, key) for key in line}


def test_missing_code_does_not_guess_from_name_or_project_rule():
    item = SimpleNamespace(source_type='delivery', statement_item_id=None,
        sequence_no=1, product_code_snapshot=None, project_name='纸箱',
        specification='300×200', tax_classification_code='123', unit='个',
        quantity=1, unit_price=10, amount=10, tax_rate=Decimal('.13'))
    with pytest.raises(InvoiceTaxTemplateError, match='缺少存货编码'):
        export_lines(None, [item])
