from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.test_phase5_orders import _login, order_api_app


def _add_composite_products(session, *, mode: str):
    from app.models.product import Product
    from app.models.product_bom import ProductBomComponent

    parent = Product(
        customer_id=1,
        product_code=f"{mode}-PARENT",
        customer_material_code=f"{mode}-PARENT",
        product_name=f"{mode}组合父件",
        box_category="normal",
        is_composite=True,
        combination_mode=mode,
    )
    first = Product(
        customer_id=1,
        product_code=f"{mode}-A",
        customer_material_code=f"{mode}-A",
        product_name=f"{mode}组件A",
        box_category="normal",
        is_internal_component=True,
    )
    second = Product(
        customer_id=1,
        product_code=f"{mode}-B",
        customer_material_code=f"{mode}-B",
        product_name=f"{mode}组件B",
        box_category="normal",
        is_internal_component=True,
    )
    session.add_all([parent, first, second])
    session.flush()
    session.add_all(
        [
            ProductBomComponent(
                parent_product_id=parent.id,
                component_product_id=first.id,
                quantity_per_set=1,
                display_order=1,
                internal_component_code=f"{mode}-A",
            ),
            ProductBomComponent(
                parent_product_id=parent.id,
                component_product_id=second.id,
                quantity_per_set=2,
                display_order=2,
                internal_component_code=f"{mode}-B",
            ),
        ]
    )
    session.commit()
    return parent, first, second


def _payload(*, items: list[dict], po: str) -> dict:
    return {
        "customer_id": 1,
        "customer_po": po,
        "order_date": "2026-07-24",
        "items": items,
    }


def _priced_component(*, product_id: int, parent, per_set: int, quantity: int, price: str) -> dict:
    return {
        "product_id": product_id,
        "quantity": quantity,
        "unit_price": price,
        "combination_mode_snapshot": "component_priced",
        "combination_role": "priced_component",
        "combination_group_key": "KIT-20260724-A",
        "combination_parent_product_id": parent.id,
        "combination_parent_name_snapshot": parent.product_name,
        "combination_set_quantity_snapshot": 100,
        "combination_quantity_per_set_snapshot": per_set,
    }


def test_parent_priced_set_creates_bom_snapshots(order_api_app) -> None:
    from app.models.product_bom import SalesOrderItemBomComponent

    app, session_factory = order_api_app
    with session_factory() as session:
        parent, _first, _second = _add_composite_products(session, mode="parent_priced_set")
    with TestClient(app) as client:
        _login(client)
        response = client.post(
            "/api/orders",
            json=_payload(
                po="PO-PARENT-PRICE",
                items=[{"product_id": parent.id, "quantity": 100, "unit_price": "8.50"}],
            ),
        )
    assert response.status_code == 201, response.text
    item = response.json()["items"][0]
    assert item["combination_mode_snapshot"] == "parent_priced_set"
    assert item["combination_role"] == "set_parent"
    with session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(SalesOrderItemBomComponent)
        ) == 2


def test_component_priced_parent_cannot_be_saved_as_one_order_line(order_api_app) -> None:
    app, session_factory = order_api_app
    with session_factory() as session:
        parent, _first, _second = _add_composite_products(session, mode="component_priced")
    with TestClient(app) as client:
        _login(client)
        response = client.post(
            "/api/orders",
            json=_payload(
                po="PO-FORGED-PARENT",
                items=[{"product_id": parent.id, "quantity": 100, "unit_price": "8.50"}],
            ),
        )
    assert response.status_code == 400
    assert "先展开并填写各组件" in response.json()["detail"]


def test_standalone_product_cannot_forge_set_parent_provenance(order_api_app) -> None:
    app, _session_factory = order_api_app
    with TestClient(app) as client:
        _login(client)
        response = client.post(
            "/api/orders",
            json=_payload(
                po="PO-FORGED-SET-PARENT",
                items=[
                    {
                        "product_id": 1,
                        "quantity": 10,
                        "unit_price": "1.00",
                        "combination_mode_snapshot": "parent_priced_set",
                        "combination_role": "set_parent",
                    }
                ],
            ),
        )
    assert response.status_code == 400
    assert "不是有效的组合组件来源" in response.json()["detail"]


def test_component_priced_components_freeze_provenance_and_keep_independent_prices(
    order_api_app,
) -> None:
    from app.models.order import OrderItem

    app, session_factory = order_api_app
    with session_factory() as session:
        parent, first, second = _add_composite_products(session, mode="component_priced")
    with TestClient(app) as client:
        _login(client)
        response = client.post(
            "/api/orders",
            json=_payload(
                po="PO-COMPONENT-PRICE",
                items=[
                    _priced_component(
                        product_id=first.id, parent=parent, per_set=1, quantity=100, price="1.25"
                    ),
                    _priced_component(
                        product_id=second.id, parent=parent, per_set=2, quantity=175, price="0.80"
                    ),
                ],
            ),
        )
    assert response.status_code == 201, response.text
    body = response.json()
    assert Decimal(str(body["total_amount"])) == Decimal("265.00")
    assert [item["quantity"] for item in body["items"]] == [100, 175]
    assert [Decimal(str(item["subtotal"])) for item in body["items"]] == [
        Decimal("125.00"),
        Decimal("140.00"),
    ]
    assert all(item["combination_role"] == "priced_component" for item in body["items"])
    assert body["items"][1]["combination_quantity_per_set_snapshot"] == 2
    with session_factory() as session:
        from app.services.product_lifecycle import has_historical_references

        stored = session.scalars(select(OrderItem).order_by(OrderItem.id)).all()
        assert [(item.product_id, item.quantity, item.combination_parent_product_id) for item in stored] == [
            (first.id, 100, parent.id),
            (second.id, 175, parent.id),
        ]
        assert has_historical_references(session, parent.id) is True


def test_component_priced_group_key_cannot_mix_different_set_counts(
    order_api_app,
) -> None:
    app, session_factory = order_api_app
    with session_factory() as session:
        parent, first, second = _add_composite_products(
            session, mode="component_priced"
        )
    first_line = _priced_component(
        product_id=first.id,
        parent=parent,
        per_set=1,
        quantity=100,
        price="1.00",
    )
    second_line = _priced_component(
        product_id=second.id,
        parent=parent,
        per_set=2,
        quantity=175,
        price="1.00",
    )
    second_line["combination_set_quantity_snapshot"] = 99
    with TestClient(app) as client:
        _login(client)
        response = client.post(
            "/api/orders",
            json=_payload(
                po="PO-MIXED-GROUP-SETS",
                items=[first_line, second_line],
            ),
        )
    assert response.status_code == 400
    assert "同一组合分组" in response.json()["detail"]


def test_component_priced_group_key_is_limited_to_database_length(
    order_api_app,
) -> None:
    app, session_factory = order_api_app
    with session_factory() as session:
        parent, first, _second = _add_composite_products(
            session, mode="component_priced"
        )
    line = _priced_component(
        product_id=first.id,
        parent=parent,
        per_set=1,
        quantity=100,
        price="1.00",
    )
    line["combination_group_key"] = "K" * 81
    with TestClient(app) as client:
        _login(client)
        response = client.post(
            "/api/orders",
            json=_payload(po="PO-GROUP-KEY-TOO-LONG", items=[line]),
        )
    assert response.status_code == 422


@pytest.mark.parametrize("invalid_kind", ["other_customer", "outside_bom", "wrong_ratio"])
def test_component_priced_provenance_rejects_invalid_parent_or_bom(
    order_api_app, invalid_kind: str
) -> None:
    from app.models.customer import Customer
    from app.models.product import Product

    app, session_factory = order_api_app
    with session_factory() as session:
        parent, first, _second = _add_composite_products(session, mode="component_priced")
        other_customer = Customer(
            customer_number=99,
            customer_code="OTHER",
            name="另一客户",
            payment_term_days=30,
            credit_limit=Decimal("1000"),
        )
        outsider = Product(
            customer_id=1,
            product_code="OUTSIDE-BOM",
            customer_material_code="OUTSIDE-BOM",
            product_name="BOM 外组件",
            box_category="normal",
        )
        session.add_all([other_customer, outsider])
        session.flush()
        if invalid_kind == "other_customer":
            parent.customer_id = other_customer.id
        session.commit()

    component_id = outsider.id if invalid_kind == "outside_bom" else first.id
    per_set = 9 if invalid_kind == "wrong_ratio" else 1
    with TestClient(app) as client:
        _login(client)
        response = client.post(
            "/api/orders",
            json=_payload(
                po=f"PO-INVALID-{invalid_kind}",
                items=[
                    _priced_component(
                        product_id=component_id,
                        parent=parent,
                        per_set=per_set,
                        quantity=100,
                        price="1.00",
                    )
                ],
            ),
        )
    assert response.status_code == 400
    assert response.json()["detail"]
