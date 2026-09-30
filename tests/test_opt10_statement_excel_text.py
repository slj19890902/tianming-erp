from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import select

from tests.test_p1_130_statement_invoice_finance import p1_130_app, _login


@pytest.mark.parametrize('suffix', ['export', 'customer-export.xlsx'])
def test_statement_export_preserves_business_text_and_numeric_totals(p1_130_app, suffix):
    from app.models.delivery import DeliveryItem
    from app.models.order import Order, OrderItem
    app, factory = p1_130_app
    with factory() as db:
        for row in db.scalars(select(Order)):
            row.customer_po = '=1+1'
        for row in db.scalars(select(OrderItem)):
            row.snapshot_product_name = '=HYPERLINK("https://invalid.example/","text")'
            row.snapshot_product_code = '000007'
        for row in db.scalars(select(DeliveryItem)):
            row.customer_po_snapshot = '=1+1'
            row.product_name_snapshot = '=HYPERLINK("https://invalid.example/","text")'
            row.product_code_snapshot = '000007'
        db.commit()
    with TestClient(app) as client:
        _login(client)
        response = client.get(f'/api/finance/statements/1/{suffix}')
        assert response.status_code == 200, response.text
    book = load_workbook(BytesIO(response.content), data_only=False)
    texts = [cell for sheet in book for row in sheet for cell in row if isinstance(cell.value, str)]
    formula_texts = [cell for cell in texts if cell.value.startswith('=')]
    assert formula_texts, 'exercise actual business text in the exported workbook'
    assert all(cell.data_type == 's' for cell in formula_texts), 'business strings must not become Excel formulas'
    assert any(cell.value == '000007' and cell.data_type == 's' for cell in texts)
    values = [cell for sheet in book for row in sheet for cell in row]
    assert any(cell.value == 200 and cell.data_type == 'n' for cell in values), 'monetary totals stay numeric'
