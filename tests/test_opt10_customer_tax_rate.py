from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.test_opt10_customer_status_lifecycle import (
    _customer,
    _customer_api,
    _customer_payload,
)
from tests.test_phase11_requisition import _login, requisition_app


def test_out_of_range_tax_rate_was_written_then_silently_fell_back(
    requisition_app,
) -> None:
    """The old API accepted 200%, then new contracts silently used 13%."""

    from fastapi.testclient import TestClient
    from app.models.customer import Customer
    from app.services.customer_price_tax import resolve_customer_price_tax_terms

    app, session_factory = _customer_api(requisition_app)
    customer = _customer(session_factory)
    assert customer is not None
    payload = _customer_payload(customer, status="active")
    payload["default_tax_rate"] = "2"
    invalid_create = _customer_payload(customer, status="active")
    invalid_create.update(
        customer_number=204,
        customer_code="OPT10-04-INVALID",
        name="OPT10-04 无效税率",
        default_tax_rate="2",
    )
    invalid_create.pop("expected_version")

    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/master/customers", json=invalid_create)
        preview = client.post(
            f"/api/master/customers/{customer.id}/update-preview", json=payload
        )
        if preview.status_code == 200:
            payload["confirmation_token"] = preview.json()["confirmation_token"]
        response = client.put(f"/api/master/customers/{customer.id}", json=payload)

    with session_factory() as db:
        persisted = db.get(Customer, customer.id)
        terms = resolve_customer_price_tax_terms(db, customer.id)
        assert persisted is not None
        observed_rate = persisted.default_tax_rate
        observed_terms_rate = terms.tax_rate
        invalid_customer = db.scalar(
            select(Customer).where(Customer.customer_code == "OPT10-04-INVALID")
        )
    assert (
        created.status_code == 422
        and preview.status_code == 422
        and response.status_code == 422
    ), (
        "out-of-range default_tax_rate was accepted "
        "(create=%s, preview=%s, update=%s, stored=%s, downstream_fallback=%s)"
        % (
            created.status_code,
            preview.status_code,
            response.status_code,
            observed_rate,
            observed_terms_rate,
        )
    )
    assert observed_rate == Decimal("0.13")
    assert observed_terms_rate == Decimal("0.13")
    assert invalid_customer is None


@pytest.mark.parametrize("tax_rate", ("0", "0.13", "1"))
def test_create_update_and_preview_accept_tax_rate_bounds(requisition_app, tax_rate: str) -> None:
    from fastapi.testclient import TestClient

    app, session_factory = _customer_api(requisition_app)
    base = _customer(session_factory)
    assert base is not None
    create_payload = _customer_payload(base, status="active")
    create_payload.update(
        customer_number=100 + int(Decimal(tax_rate) * 100),
        customer_code=f"OPT10-04-{tax_rate}",
        name=f"OPT10-04 税率 {tax_rate}",
        default_tax_rate=tax_rate,
    )
    create_payload.pop("expected_version")

    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/master/customers", json=create_payload)
        assert created.status_code == 201, created.text
        payload = _customer_payload(SimpleNamespace(**created.json()), status="active")
        payload["default_tax_rate"] = tax_rate
        preview = client.post(
            f"/api/master/customers/{created.json()['id']}/update-preview", json=payload
        )
        updated = client.put(
            f"/api/master/customers/{created.json()['id']}", json=payload
        )

    assert preview.status_code == 200, preview.text
    assert updated.status_code == 200, updated.text
    assert Decimal(str(updated.json()["default_tax_rate"])) == Decimal(tax_rate)


def test_historical_out_of_range_tax_rate_remains_readable_and_keeps_fallback(
    requisition_app,
) -> None:
    from fastapi.testclient import TestClient
    from app.models.customer import Customer
    from app.services.customer_price_tax import resolve_customer_price_tax_terms

    app, session_factory = _customer_api(requisition_app)
    customer = _customer(session_factory)
    assert customer is not None
    with session_factory() as db:
        persisted = db.get(Customer, customer.id)
        assert persisted is not None
        persisted.default_tax_rate = Decimal("2")
        db.commit()

    with TestClient(app) as client:
        _login(client, "admin")
        response = client.get(f"/api/master/customers/{customer.id}")
        listed = client.get("/api/master/customers", params={"include_inactive": True})

    with session_factory() as db:
        terms = resolve_customer_price_tax_terms(db, customer.id)
    assert response.status_code == 200, response.text
    assert Decimal(str(response.json()["default_tax_rate"])) == Decimal("2")
    assert Decimal(str(next(row for row in listed.json()["items"] if row["id"] == customer.id)["default_tax_rate"])) == Decimal("2")
    assert terms.tax_rate == Decimal("0.13")
