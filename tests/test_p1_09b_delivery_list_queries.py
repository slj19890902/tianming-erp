from __future__ import annotations

from sqlalchemy import event
from fastapi.testclient import TestClient


pytest_plugins = ("test_n029_production_integration",)


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login", json={"username": "n029-admin", "password": "123456"}
    )
    assert response.status_code == 200, response.text


def _create_delivery(client: TestClient, customer_id: int, order_item_id: int) -> None:
    response = client.post(
        "/api/deliveries",
        json={
            "customer_id": customer_id,
            "items": [{"order_item_id": order_item_id, "delivered_quantity": 1}],
        },
    )
    assert response.status_code == 201, response.text


def _select_count(engine, callback):
    statements: list[str] = []

    def listener(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", listener)
    try:
        callback()
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    return len(statements)


def test_delivery_list_page_context_avoids_per_delivery_base_queries(n029_delivery_app) -> None:
    app, factory, ids = n029_delivery_app
    with TestClient(app) as client:
        _login(client)
        for item_key in ("legacy_received", "legacy_finished", "legacy_semi"):
            _create_delivery(client, ids["customer"], ids[item_key])

        engine = factory.kw["bind"]
        one_page = _select_count(
            engine,
            lambda: client.get("/api/deliveries?page=1&page_size=1").raise_for_status(),
        )
        many_response = client.get("/api/deliveries?page=1&page_size=3")
        assert many_response.status_code == 200, many_response.text
        many_page = _select_count(
            engine,
            lambda: client.get("/api/deliveries?page=1&page_size=3").raise_for_status(),
        )

    payload = many_response.json()
    assert payload["total"] == 3
    assert len(payload["items"]) == 3
    assert all("items" in row and "pick_task" in row for row in payload["items"])
    # Page-wide base entities are prefetched once; the remaining item-source
    # reads may grow with lines, but three documents must not replay a full
    # delivery serializer three times.
    assert many_page <= one_page + 20
