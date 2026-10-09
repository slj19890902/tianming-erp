"""Independent review: successful save retries cannot depend on later state."""
from copy import deepcopy
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from tests.test_phase7_deliveries import delivery_api_app, _create_payload, _login


@pytest.mark.parametrize('later_change', ['customer_po', 'delivery_date'])
def test_ordinary_edit_replay_keeps_original_intent_after_later_change(delivery_api_app, later_change):
    from app.models.delivery import Delivery

    app, factory = delivery_api_app
    with TestClient(app) as client:
        _login(client, 'admin')
        create = _create_payload()
        for line in create['items']:
            line['customer_po'] = 'PO-ORIGINAL'
        created = client.post('/api/deliveries', json=create)
        assert created.status_code == 201, created.text
        original = _create_payload()
        original.pop('customer_id')
        original.update(expected_version=created.json()['version'], idempotency_key='review-first-edit',
                        delivery_date=created.json()['delivery_date'])
        original['items'][0]['delivered_quantity'] = 25
        url = f"/api/deliveries/{created.json()['id']}"
        first = client.put(url, json=deepcopy(original))
        assert first.status_code == 200, first.text
        if later_change == 'customer_po':
            changed = deepcopy(original)
            changed.update(expected_version=first.json()['version'], idempotency_key='review-later-edit')
            for line in changed['items']:
                line['customer_po'] = 'PO-LATER'
            later = client.put(url, json=changed)
            assert later.status_code == 200, later.text
        else:
            # Synthetic fixture models a separately authorized date correction;
            # this test targets retry semantics, not the date-correction writer.
            with factory() as db:
                delivery = db.get(Delivery, created.json()['id'])
                delivery.delivery_date -= timedelta(days=1)
                delivery.version += 1
                db.commit()
        with factory() as db:
            delivery = db.get(Delivery, created.json()['id'])
            current = (delivery.version, delivery.delivery_date)
        replay = client.put(url, json=deepcopy(original))
        assert replay.status_code == 200, replay.text
        assert replay.json() == first.json()
        with factory() as db:
            delivery = db.get(Delivery, created.json()['id'])
            assert (delivery.version, delivery.delivery_date) == current
