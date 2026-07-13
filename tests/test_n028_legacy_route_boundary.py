from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


LEGACY_API_ROUTES = {
    ("/api/health", "health_check", "main"),
    ("/api/meta", "app_meta", "main"),
    ("/api/login", "login", "main"),
    ("/api/archives/search", "search_archives", "main"),
    ("/api/customers", "list_customers", "main"),
    ("/api/customer-management", "list_customer_management", "main"),
    (
        "/api/customer-management/{customer_id}",
        "get_customer_management",
        "main",
    ),
    (
        "/api/customer-management/{customer_id}/products",
        "list_customer_management_products",
        "main",
    ),
    ("/api/customer-management", "create_customer_management", "main"),
    (
        "/api/customer-management/{customer_id}",
        "update_customer_management",
        "main",
    ),
    (
        "/api/customer-management/{customer_id}/status",
        "update_customer_management_status",
        "main",
    ),
    ("/api/customers/{customer_id}/styles", "list_customer_styles", "main"),
    ("/api/legacy/summary", "legacy_data_summary", "main"),
    ("/api/orders", "list_orders", "main"),
    ("/api/orders", "create_order", "main"),
}


def test_create_app_replaces_the_complete_legacy_api_boundary() -> None:
    application = create_app()
    registered_routes = {
        (route.path, route.name, route.endpoint.__module__)
        for route in application.routes
        if getattr(route, "path", "") == "/api"
        or getattr(route, "path", "").startswith("/api/")
    }
    api_routes = [
        route
        for route in application.routes
        if getattr(route, "path", "") == "/api"
        or getattr(route, "path", "").startswith("/api/")
    ]

    assert not LEGACY_API_ROUTES & registered_routes
    assert any(route.path == "/dashboard" for route in application.routes)
    assert any(route.path == "/warehouse.html" for route in application.routes)
    for path, method in (
        ("/api/auth/login", "POST"),
        ("/api/master/customers", "GET"),
        ("/api/master/customers", "POST"),
        ("/api/orders", "GET"),
        ("/api/orders", "POST"),
        ("/api/health", "GET"),
    ):
        assert sum(
            route.path == path and method in getattr(route, "methods", set())
            for route in api_routes
        ) == 1


def test_legacy_customer_management_route_returns_404_without_database_access(
    monkeypatch,
) -> None:
    import main as legacy

    def database_access_is_not_allowed(*_args, **_kwargs):
        raise AssertionError("legacy customer-management route accessed the database")

    monkeypatch.setattr(
        legacy,
        "customer_management_rows",
        database_access_is_not_allowed,
    )

    with TestClient(create_app()) as client:
        response = client.get("/api/customer-management")

    assert response.status_code == 404
