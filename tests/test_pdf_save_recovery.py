import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from test_semi_finished_order_reservation import b1_app, login, order_item, add_finished_lot, finished_plan
from app.models.order import Order
from app.models.warehouse_inventory import InventoryReservation


def test_frontend_pdf_loss_and_recovery():
    result = subprocess.run(['node', 'tests/pdf_save_recovery.cjs'], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('po', ['PDF-RETRY-PO', None])
def test_pdf_committed_response_lost_replays_one_order_and_reservation(b1_app, po):
    from app.models.material import Material
    from app.models.product import Product
    from app.models.supplier import Supplier
    from tests.test_phase16_pdf_order_import import _signed_pdf_preview_token
    app, factory = b1_app
    with factory() as db:
        supplier = db.scalar(select(Supplier).where(Supplier.is_active.is_(True)))
        # b1_app already supplies the priced material used by its stock fixture.
        material = db.scalar(select(Material).where(
            Material.code == 'A416D', Material.supplier_name == supplier.standard_name))
        assert material is not None and material.is_active
        db.get(Product, 1).material_id = material.id; db.commit()
    lot, version = add_finished_lot(factory, product_id=1, quantity=10, key='pdf-retry-stock')
    payload = dict(customer_id=1, customer_po=po, idempotency_key='pdf-retry-operation', import_draft=True,
                   import_integrity_status='passed', import_integrity_errors=[],
                   pdf_import_confirmation=dict(preview_safety_token=_signed_pdf_preview_token(app), confirmed=True),
                   items=[order_item(1, 10, {'finished':[finished_plan(lot,version,10)],'semi':[]}, line='pdf-row-1')])
    with TestClient(app) as client:
        login(client, 'sales')
        url = '/api/orders/create-attempts/pdf-retry-operation'
        assert client.get(url).json() == {'status':'not_found'}
        first = client.post('/api/orders', json=payload); assert first.status_code == 201, first.text
        replay = client.post('/api/orders', json=payload); assert replay.status_code == 201, replay.text
        assert replay.json()['id'] == first.json()['id']
        assert client.get(url).json()['order']['id'] == first.json()['id']
        changed = deepcopy(payload); changed['items'][0]['quantity'] = 11
        assert client.post('/api/orders', json=changed).status_code == 409
        stale = deepcopy(payload); stale['idempotency_key'] = 'another-operation'
        source_replay = client.post('/api/orders', json=stale)
        assert source_replay.status_code == 201
        assert source_replay.json()['id'] == first.json()['id']
        assert source_replay.json()['source_replay'] is True
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(Order)) == 1
            assert db.scalar(select(func.count()).select_from(InventoryReservation)) == 1
        # Other account cannot discover this user's result with the same key.
        client.cookies.clear(); login(client, 'admin')
        assert client.get(url).json() == {'status':'not_found'}
        # Revoked customer access also protects recovery of an existing result.
        from app.models.user import User
        with factory() as db:
            user = db.scalar(select(User).where(User.username == 'sales'))
            user.customer_access_mode = 'selected'; db.commit()
        client.cookies.clear(); login(client, 'sales')
        assert client.get(url).status_code == 403
        client.cookies.clear(); assert client.get(url).status_code == 401


def test_pdf_recovery_control_is_not_natively_disabled_with_locked_draft():
    # HTML fieldset disables descendant buttons regardless of their own Vue guard.
    # Keep result lookup available while the source fields stay immutable.
    from html.parser import HTMLParser

    class Controls(HTMLParser):
        def __init__(self):
            super().__init__()
            self.fieldsets = []
            self.recovery = []
            self.source_fields = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "fieldset":
                self.fieldsets.append(attrs.get(":disabled", ""))
            if attrs.get("@click") == "retryFailedImportDraft(draft)":
                self.recovery.append(tuple(self.fieldsets))
            if any(attrs.get(key) in {"draft.customer_po", "draft.delivery_date", "draft.matched_customer_id"}
                   for key in ("v-model", "v-model.trim")):
                self.source_fields.append(tuple(self.fieldsets))

        def handle_endtag(self, tag):
            if tag == "fieldset":
                assert self.fieldsets, "unbalanced fieldset"
                self.fieldsets.pop()

    controls = Controls()
    controls.feed((Path(__file__).resolve().parents[1] / "static/index.html").read_text("utf-8"))
    assert controls.recovery == [()], "result lookup must not inherit the locked draft's native disabled state"
    assert len(controls.source_fields) == 3
    assert all("isImportDraftLocked(draft)" in parents for parents in controls.source_fields)
    assert controls.fieldsets == []
