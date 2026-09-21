"""P03: security changes must revoke old sessions before scoped order reads."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_n028_customer_scopes import _login, n028_customer_scope_app


def _order_ids(factory) -> dict[int, int]:
    from app.models.order import Order

    with factory() as session:
        return {
            int(order.customer_id): int(order.id)
            for order in session.scalars(select(Order)).all()
        }


def _user_id(factory, username: str) -> int:
    from app.models.user import User

    with factory() as session:
        user_id = session.scalar(select(User.id).where(User.username == username))
    assert user_id is not None
    return int(user_id)


def _order_read_responses(client: TestClient, *, customer_id: int, order_id: int):
    return (
        client.get("/api/orders", params={"customer_id": customer_id}),
        client.get(f"/api/orders/{order_id}"),
        client.get(
            "/api/orders/group-detail",
            params={
                "customer_id": customer_id,
                "anchor_order_id": order_id,
                "scope": "all",
            },
        ),
        client.get(f"/api/orders/customer-heat/{customer_id}/orders"),
    )


def test_access_change_revokes_old_session_and_rechecks_all_order_read_shapes(
    n028_customer_scope_app,
) -> None:
    """Direct IDs, filters, group details and heat summaries follow the new scope."""

    app, ids, factory = n028_customer_scope_app
    order_ids = _order_ids(factory)
    sales_id = _user_id(factory, "n028-sales")
    own_customer = int(ids["customer"])
    other_customer = int(ids["other_customer"])

    with TestClient(app) as admin_client, TestClient(app) as old_sales_client:
        _login(admin_client, "n028-admin", "AdminPass123!")
        _login(old_sales_client, "n028-sales", "SalesPass123!")

        own_list = old_sales_client.get("/api/orders", params={"scope": "all"})
        assert own_list.status_code == 200, own_list.text
        assert {row["customer_id"] for row in own_list.json()["items"]} == {
            own_customer
        }
        denied_before_change = _order_read_responses(
            old_sales_client,
            customer_id=other_customer,
            order_id=order_ids[other_customer],
        )
        assert all(response.status_code == 403 for response in denied_before_change)

        changed = admin_client.put(
            f"/api/auth/users/{sales_id}/access",
            json={
                "mode": "selected",
                "customer_ids": [other_customer],
                "overrides": {},
            },
        )
        assert changed.status_code == 200, changed.text
        # The pre-change cookie cannot make even a general order-list request.
        assert old_sales_client.get("/api/orders").status_code == 401

    with TestClient(app) as new_sales_client:
        _login(new_sales_client, "n028-sales", "SalesPass123!")
        newly_scoped = new_sales_client.get(
            "/api/orders", params={"scope": "all"}
        )
        assert newly_scoped.status_code == 200, newly_scoped.text
        assert {row["customer_id"] for row in newly_scoped.json()["items"]} == {
            other_customer
        }
        permitted_after_change = _order_read_responses(
            new_sales_client,
            customer_id=other_customer,
            order_id=order_ids[other_customer],
        )
        assert all(response.status_code == 200 for response in permitted_after_change)
        denied_after_change = _order_read_responses(
            new_sales_client,
            customer_id=own_customer,
            order_id=order_ids[own_customer],
        )
        assert all(response.status_code == 403 for response in denied_after_change)
