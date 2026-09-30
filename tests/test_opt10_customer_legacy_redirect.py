from __future__ import annotations

from fastapi.testclient import TestClient


def test_legacy_customers_entry_redirects_to_spa_without_restoring_legacy_api() -> None:
    """`/customers` must not serve the obsolete customer-management client."""

    from app.main import create_app

    application = create_app()
    reconfigured = create_app()
    customer_routes = [
        route for route in application.routes if getattr(route, "path", None) == "/customers"
    ]
    assert reconfigured is application
    assert len(customer_routes) == 1
    client = TestClient(application)
    try:
        legacy_entry = client.get("/customers", follow_redirects=False)
        target = client.get("/?page=customers")
        legacy_api = client.get("/api/customer-management")
        master_api = client.get("/api/master/customers")
    finally:
        client.close()

    assert legacy_entry.status_code == 307
    assert legacy_entry.headers["location"] == "/?page=customers"
    assert "no-store" in legacy_entry.headers.get("cache-control", "").lower()
    assert target.status_code == 200
    assert "activePage" in target.text
    assert legacy_api.status_code in {401, 403, 404}
    assert master_api.status_code == 401
