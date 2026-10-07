from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

from app.api.orders import PdfImportConfirmation
from app.models.order import Order, OrderItem
from app.models.order_import_source import OrderImportSourceLine
from tests.test_phase16_pdf_order_import import _signed_pdf_preview_token
from tests.test_semi_finished_order_reservation import b1_app, login, order_item
from tests.test_t02_order_import_source_identity import _payload, _ready_product


def test_28_line_signed_import_saves_and_replays_without_duplicates(b1_app):
    app, factory = b1_app
    _ready_product(factory)
    payload = _payload(app, 'c' * 64, 'long-preview-token')
    payload['items'] = [order_item(1, 10, line=f'pdf-line-{i}') for i in range(1, 29)]
    token = _signed_pdf_preview_token(app, source_hash='c' * 64,
        source_name='28-line-order.xlsx', items=[dict(line_no=i, source_sheet='订货单',
            source_row=i + 4, product_code='B1-P1', product_name='纸箱',
            quantity=10, unit_price='1.00', raw_lines=[f'源行{i}']) for i in range(1, 29)])
    assert len(token) > 4000
    payload['pdf_import_confirmation']['preview_safety_token'] = token
    with TestClient(app) as client:
        login(client, 'sales')
        bad = deepcopy(payload)
        header, body, signature = token.split('.')
        bad['pdf_import_confirmation']['preview_safety_token'] = '.'.join(
            [header, body, ('A' if signature[0] != 'A' else 'B') + signature[1:]])
        rejected = client.post('/api/orders', json=bad)
        assert rejected.status_code == 409, rejected.text
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(Order)) == 0
        saved = client.post('/api/orders', json=payload)
        assert saved.status_code == 201, saved.text
        replay = client.post('/api/orders', json=payload)
        assert replay.status_code in (200, 201), replay.text
        assert replay.json()['id'] == saved.json()['id']
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 1
        assert db.scalar(select(func.count()).select_from(OrderItem)) == 28
        assert db.scalar(select(func.count()).select_from(OrderImportSourceLine)) == 28


def test_preview_token_capacity_remains_bounded():
    with pytest.raises(ValidationError) as error:
        PdfImportConfirmation(preview_safety_token='x' * (1024 * 1024 + 1), confirmed=True)
    assert error.value.errors()[0]['type'] == 'string_too_long'
