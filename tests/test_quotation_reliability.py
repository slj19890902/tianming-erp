"""Quotation retries, stale windows and audit failure use disposable databases."""
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from test_opt10_quotation_scope_query import quotation_scope_app as shared_quote_app, _login


@pytest.fixture()
def quotation_scope_app(tmp_path_factory):
    yield from shared_quote_app.__wrapped__(tmp_path_factory)


def payload(key="quote-create-20261009"):
    return {"idempotency_key": key, "quotation_date": "2026-10-09", "remarks": "original", "items": [
        {"product_name": "synthetic quotation", "box_type": "其他", "quantity": 10, "final_unit_price": 2}
    ]}


@pytest.fixture()
def setup_quote(quotation_scope_app):
    app, factory, engine, ids = quotation_scope_app
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client, "opt10-scoped")
        url = f"/api/quotations?customer_id={ids['visible_customer']}"
        yield client, factory, ids, url


def test_create_retry_same_result_and_changed_content_rejected(setup_quote):
    c, factory, ids, url = setup_quote
    a = c.post(url, json=payload())
    b = c.post(url, json=payload())
    assert a.status_code == b.status_code == 201, (a.text, b.text)
    assert a.json() == b.json()
    changed = payload(); changed["remarks"] = "different"
    assert c.post(url, json=changed).status_code == 409
    from app.models.quotation import QuotationOrder
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(QuotationOrder).where(QuotationOrder.quotation_no.like("QT-20261009-%"))) == 1


def test_missing_key_and_missing_version_are_rejected(setup_quote):
    c, _, _, url = setup_quote
    missing = payload(); missing.pop("idempotency_key")
    assert c.post(url, json=missing).status_code == 422
    row = c.post(url, json=payload()).json()
    body = payload("quote-update-no-version")
    assert c.put(f"/api/quotations/{row['id']}", json=body).status_code == 409


def test_stale_save_cannot_overwrite_and_replay_survives_new_version(setup_quote):
    c, _, _, url = setup_quote
    row = c.post(url, json=payload()).json()
    path = f"/api/quotations/{row['id']}"
    a = payload("quote-update-a"); a.update(expected_version=1, remarks="saved by A")
    first = c.put(path, json=a)
    assert first.status_code == 200, first.text
    assert first.json()["version"] == 2
    b = payload("quote-update-b"); b.update(expected_version=1, remarks="stale B")
    assert c.put(path, json=b).status_code == 409
    assert c.get(path).json()["remarks"] == "saved by A"
    assert c.put(path, json=a).json() == first.json()


def test_actions_require_current_version_and_replay(setup_quote):
    c, _, _, url = setup_quote
    row = c.post(url, json=payload()).json()
    path = f"/api/quotations/{row['id']}"
    generated = c.post(path + "/generate", json={"idempotency_key": "quote-generate", "expected_version": 1})
    assert generated.status_code == 200, generated.text
    assert generated.json()["version"] == 2
    stale = c.post(path + "/accept", json={"idempotency_key": "quote-accept-stale", "expected_version": 1})
    assert stale.status_code == 409
    command = {"idempotency_key": "quote-accept-current", "expected_version": 2}
    accepted = c.post(path + "/accept", json=command)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["version"] == 3
    assert c.post(path + "/accept", json=command).json() == accepted.json()
    assert c.post(path + "/void", json={"idempotency_key": "quote-void-stale", "expected_version": 2}).status_code == 409
    assert c.post(path + "/void", json={"idempotency_key": "quote-void-current", "expected_version": 3}).json()["version"] == 4


def test_audit_failure_rolls_back_number_items_and_command(setup_quote, monkeypatch):
    c, factory, _, url = setup_quote
    from app.services import quotation_mutations
    original = quotation_mutations.append_audit_event
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic audit failure")
    monkeypatch.setattr(quotation_mutations, "append_audit_event", fail)
    assert c.post(url, json=payload()).status_code == 500
    from app.models.quotation import QuotationMutation
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(QuotationMutation)) == 0
    monkeypatch.setattr(quotation_mutations, "append_audit_event", original)
    row = c.post(url, json=payload()).json()
    assert row["quotation_no"] == "QT-20261009-001"


def test_replay_rechecks_customer_scope_and_hides_cost(setup_quote):
    c, factory, ids, url = setup_quote
    created = c.post(url, json=payload())
    assert created.status_code == 201
    assert "estimated_unit_cost" not in created.json()["items"][0]
    from app.models.access_control import UserCustomerScope
    from app.models.user import User
    with factory() as db:
        user = db.scalar(select(User).where(User.username == "opt10-scoped"))
        for scope in db.scalars(select(UserCustomerScope).where(UserCustomerScope.user_id == user.id)).all():
            db.delete(scope)
        db.commit()
    assert c.post(url, json=payload()).status_code == 403


def test_simultaneous_retry_and_two_editor_save(setup_quote):
    from concurrent.futures import ThreadPoolExecutor
    c, factory, _, url = setup_quote
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:c.post(url,json=payload()), range(2)))
    assert all(r.status_code in (201,409) for r in results)
    row=c.post(url,json=payload()).json()
    assert len({r.json()['id'] for r in results if r.status_code==201}|{row['id']})==1
    a=payload('two-window-a');a.update(expected_version=1,remarks='A')
    b=payload('two-window-b');b.update(expected_version=1,remarks='B')
    with ThreadPoolExecutor(max_workers=2) as pool:
        updates=list(pool.map(lambda body:c.put(f"/api/quotations/{row['id']}",json=body),[a,b]))
    assert sorted(r.status_code for r in updates)==[200,409]
    saved=c.get(f"/api/quotations/{row['id']}").json()
    assert saved['version']==2 and saved['remarks']==next(r.json()['remarks'] for r in updates if r.status_code==200)
