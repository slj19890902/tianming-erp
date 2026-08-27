from __future__ import annotations

from collections.abc import Generator
from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from app.api.auth import router as auth_router
from app.api.deps import get_db
from app.api.orders import router as orders_router
from app.core.database import create_sqlite_engine
from app.core.security import hash_password
from app.models import Base
from app.models.access_control import UserCustomerScope, UserPermissionOverride
from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.user import User


PASSWORD = "123456"
AS_OF = date(2026, 8, 14)


@pytest.fixture()
def customer_heat_app(tmp_path, monkeypatch):
    monkeypatch.setattr("app.api.orders.beijing_today", lambda: AS_OF)
    engine = create_sqlite_engine(tmp_path / "p1-61b-customer-heat.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="heat-admin",
            password_hash=hash_password(PASSWORD),
            role="admin",
            real_name="热力管理员",
            must_change_password=False,
            customer_access_mode="all",
        )
        scoped = User(
            username="heat-scoped",
            password_hash=hash_password(PASSWORD),
            role="sales",
            real_name="热力受限业务员",
            must_change_password=False,
            customer_access_mode="selected",
        )
        workshop = User(
            username="heat-workshop",
            password_hash=hash_password(PASSWORD),
            role="workshop",
            real_name="热力车间员工",
            must_change_password=False,
            customer_access_mode="all",
        )
        denied = User(
            username="heat-denied",
            password_hash=hash_password(PASSWORD),
            role="workshop",
            real_name="无订单查看权限员工",
            must_change_password=False,
            customer_access_mode="all",
        )
        customers = {
            "hot": Customer(
                customer_number=10,
                customer_code="HEAT-HOT",
                name="高频近期客户",
            ),
            "warm": Customer(
                customer_number=20,
                customer_code="HEAT-WARM",
                name="普通成熟客户",
                chinese_short_name="暖客",
            ),
            "new": Customer(
                customer_number=30,
                customer_code="HEAT-NEW",
                name="新开户客户",
            ),
            "dormant": Customer(
                customer_number=40,
                customer_code="HEAT-DORMANT",
                name="长期未下单客户",
            ),
            "none": Customer(
                customer_number=50,
                customer_code="HEAT-NONE",
                name="无有效订单客户",
            ),
            "incomplete": Customer(
                customer_number=60,
                customer_code="HEAT-INCOMPLETE",
                name="金额不完整客户",
            ),
            "outside": Customer(
                customer_number=70,
                customer_code="HEAT-OUTSIDE",
                name="范围外高额客户",
                chinese_short_name="越权客",
            ),
        }
        db.add_all([admin, scoped, workshop, denied, *customers.values()])
        db.flush()
        db.add_all(
            [
                UserCustomerScope(
                    user_id=scoped.id,
                    customer_id=customers[key].id,
                    assigned_by=admin.id,
                )
                for key in ("hot", "new")
            ]
            + [
                UserPermissionOverride(
                    user_id=scoped.id,
                    permission_code="orders.view",
                    is_allowed=True,
                    granted_by=admin.id,
                ),
                UserPermissionOverride(
                    user_id=workshop.id,
                    permission_code="orders.view",
                    is_allowed=True,
                    granted_by=admin.id,
                ),
                UserPermissionOverride(
                    user_id=denied.id,
                    permission_code="orders.view",
                    is_allowed=False,
                    granted_by=admin.id,
                ),
            ]
        )

        products: dict[str, Product] = {}
        for key, customer in customers.items():
            product = Product(
                customer_id=customer.id,
                product_code=f"P161B-{key.upper()}",
                customer_material_code=f"MAT-{key.upper()}",
                product_name=f"P1-61B {key} 测试纸箱",
                box_style="普通箱",
            )
            db.add(product)
            products[key] = product
        db.flush()

        sequence = 0

        def add_order(
            key: str,
            order_date: date,
            *,
            status: str = "closed",
            unit_prices: tuple[str, ...] = ("5",),
        ) -> int:
            nonlocal sequence
            sequence += 1
            order = Order(
                order_number=f"TM-P161B-{sequence:03d}",
                customer_id=customers[key].id,
                customer_po=f"PO-P161B-{sequence:03d}",
                order_date=order_date,
                delivery_date=order_date,
                status=status,
                payment_status="unpaid",
                total_amount=sum(
                    (Decimal(value) * 10 for value in unit_prices), Decimal("0")
                ),
            )
            db.add(order)
            db.flush()
            for item_sequence, value in enumerate(unit_prices, start=1):
                price = Decimal(value)
                db.add(
                    OrderItem(
                        order_id=order.id,
                        product_id=products[key].id,
                        item_sequence=item_sequence,
                        item_order_number=f"{order.order_number}-{item_sequence:03d}",
                        quantity=10,
                        delivered_quantity=0,
                        unit_price=price,
                        subtotal=price * 10,
                        material_status="pending",
                        snapshot_product_code=products[key].product_code,
                        snapshot_product_name=products[key].product_name,
                        snapshot_spec="400×300×200",
                        snapshot_material="A=A",
                    )
                )
            db.flush()
            return int(order.id)

        hot_order_ids = [
            add_order("hot", date(2026, 6, 1), unit_prices=("5", "5")),
            add_order("hot", date(2026, 8, 10)),
            add_order("hot", date(2026, 8, 12)),
            add_order("hot", date(2026, 8, 13)),
            add_order("hot", date(2026, 8, 14), status="pending_confirmation"),
        ]
        add_order("hot", date(2026, 8, 14), status="cancelled", unit_prices=("999",))
        add_order("hot", date(2026, 8, 15), status="closed", unit_prices=("999",))
        for day in (date(2026, 4, 1), date(2026, 8, 1), date(2026, 8, 10)):
            add_order("warm", day, unit_prices=("2",))
        add_order("new", date(2026, 8, 14), status="pending_confirmation")
        add_order("dormant", date(2025, 8, 1), unit_prices=("9",))
        for day, price in (
            (date(2026, 5, 1), "3"),
            (date(2026, 8, 1), "3"),
            (date(2026, 8, 5), "0"),
        ):
            add_order("incomplete", day, unit_prices=(price,))
        for day in (
            date(2026, 6, 1),
            date(2026, 8, 11),
            date(2026, 8, 12),
            date(2026, 8, 13),
            date(2026, 8, 14),
        ):
            add_order("outside", day, unit_prices=("10000",))
        db.commit()
        ids = {
            **{key: int(customer.id) for key, customer in customers.items()},
            "hot_orders": hot_order_ids,
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(orders_router, prefix="/api/orders")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, ids, engine
    finally:
        engine.dispose()


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text


def _heat(client: TestClient, **params):
    return client.get(
        "/api/orders/customer-heat",
        params={"page_size": 100, **params},
    )


def test_customer_heat_is_scoped_explainable_and_master_order_deduplicated(
    customer_heat_app,
) -> None:
    app, ids, _engine = customer_heat_app
    with TestClient(app) as client:
        _login(client, "heat-scoped")
        response = _heat(client)
        scoped_search = _heat(client, keyword="HEAT-OUTSIDE")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert body["amount_visible"] is True
    assert body["threshold_version"] == "p1-61a-20260814-candidate-v1"
    assert [row["customer_id"] for row in body["items"]] == [ids["hot"], ids["new"]]
    hot, new = body["items"]
    assert hot["segment"] == "established"
    assert hot["heat_level"] == 5
    assert hot["recency_days"] == 0
    assert hot["valid_order_days_90"] == 5
    assert hot["valid_master_orders_365"] == 5
    assert hot["annual_amount"] == "300.00"
    assert hot["ongoing_order_count"] == 1
    assert hot["earliest_blocking_stage"] == "pending_confirmation"
    assert new["segment"] == "new"
    assert new["heat_level"] is None
    assert new["heat_label"] == "新客户"
    encoded = response.text
    assert "范围外高额客户" not in encoded
    assert "HEAT-OUTSIDE" not in encoded
    assert scoped_search.status_code == 200
    assert scoped_search.json()["total"] == 0


def test_customer_heat_hides_amount_before_return_and_sorting(customer_heat_app) -> None:
    app, ids, _engine = customer_heat_app
    with TestClient(app) as client:
        _login(client, "heat-workshop")
        response = _heat(client)
        forbidden_sort = _heat(client, sort_by="annual_amount")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["amount_visible"] is False
    assert body["total"] == 7
    for row in body["items"]:
        assert "annual_amount" not in row
        assert "known_annual_amount" not in row
        assert "annual_amount_completeness_rate" not in row
    assert forbidden_sort.status_code == 403
    assert ids["outside"] in {row["customer_id"] for row in body["items"]}


def test_customer_heat_marks_dormant_new_and_incomplete_amount(customer_heat_app) -> None:
    app, ids, _engine = customer_heat_app
    with TestClient(app) as client:
        _login(client, "heat-admin")
        response = _heat(client)
    assert response.status_code == 200, response.text
    rows = {row["customer_id"]: row for row in response.json()["items"]}
    assert rows[ids["dormant"]]["segment"] == "dormant"
    assert rows[ids["dormant"]]["heat_level"] == 1
    assert rows[ids["none"]]["segment"] == "dormant"
    assert rows[ids["none"]]["last_valid_order_date"] is None
    assert rows[ids["incomplete"]]["annual_amount"] is None
    assert rows[ids["incomplete"]]["annual_amount_complete"] is False
    assert Decimal(rows[ids["incomplete"]]["known_annual_amount"]) == Decimal("60.00")


def test_customer_heat_permission_pagination_and_on_demand_order_summary(
    customer_heat_app,
) -> None:
    app, ids, engine = customer_heat_app
    with TestClient(app) as client:
        _login(client, "heat-denied")
        assert _heat(client).status_code == 403

    with TestClient(app) as client:
        _login(client, "heat-admin")
        first = _heat(client, page=1, page_size=2)
        second = _heat(client, page=2, page_size=2)
        expanded_selects: list[str] = []

        def capture_expanded_selects(
            _connection, _cursor, statement, _parameters, _context, _executemany
        ) -> None:
            if statement.lstrip().upper().startswith("SELECT"):
                expanded_selects.append(statement)

        event.listen(engine, "before_cursor_execute", capture_expanded_selects)
        try:
            expanded = client.get(
                f"/api/orders/customer-heat/{ids['hot']}/orders",
                params={"scope": "active", "page": 1, "page_size": 2},
            )
        finally:
            event.remove(engine, "before_cursor_execute", capture_expanded_selects)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["total"] == second.json()["total"] == 7
    first_ids = {row["customer_id"] for row in first.json()["items"]}
    second_ids = {row["customer_id"] for row in second.json()["items"]}
    assert first_ids.isdisjoint(second_ids)
    assert expanded.status_code == 200, expanded.text
    expanded_body = expanded.json()
    assert expanded_body["customer_id"] == ids["hot"]
    assert expanded_body["total"] == 1
    assert len(expanded_body["items"]) == 1
    assert all("items" not in order for order in expanded_body["items"])
    assert len(expanded_selects) <= 18

    with TestClient(app) as client:
        _login(client, "heat-scoped")
        denied = client.get(
            f"/api/orders/customer-heat/{ids['outside']}/orders",
            params={"page": 1, "page_size": 10},
        )
    assert denied.status_code == 403


def test_customer_heat_amount_tiebreak_is_permission_gated(customer_heat_app) -> None:
    app, ids, _engine = customer_heat_app
    with TestClient(app) as client:
        _login(client, "heat-admin")
        admin_default = _heat(client)
        admin_amount = _heat(client, sort_by="annual_amount")
    assert admin_default.status_code == 200, admin_default.text
    assert admin_amount.status_code == 200, admin_amount.text
    assert admin_default.json()["items"][0]["customer_id"] == ids["outside"]
    assert admin_amount.json()["items"][0]["customer_id"] == ids["outside"]

    with TestClient(app) as client:
        _login(client, "heat-workshop")
        workshop_default = _heat(client)
    assert workshop_default.status_code == 200, workshop_default.text
    assert workshop_default.json()["items"][0]["customer_id"] == ids["hot"]


def test_customer_heat_keyword_and_query_families(customer_heat_app) -> None:
    app, ids, engine = customer_heat_app
    with TestClient(app) as client:
        _login(client, "heat-admin")
        keyword = _heat(client, keyword="HEAT-WARM")
        assert keyword.status_code == 200, keyword.text
        assert [row["customer_id"] for row in keyword.json()["items"]] == [ids["warm"]]
        trimmed_abbreviation = _heat(client, keyword=" heat-warm ")
        assert trimmed_abbreviation.status_code == 200, trimmed_abbreviation.text
        assert [row["customer_id"] for row in trimmed_abbreviation.json()["items"]] == [
            ids["warm"]
        ]
        short_name = _heat(client, keyword="暖客")
        assert short_name.status_code == 200, short_name.text
        assert [row["customer_id"] for row in short_name.json()["items"]] == [
            ids["warm"]
        ]

        def select_count_for(page_size: int) -> int:
            statements: list[str] = []

            def before_cursor_execute(
                _connection, _cursor, statement, _parameters, _context, _executemany
            ) -> None:
                if statement.lstrip().upper().startswith("SELECT"):
                    statements.append(statement)

            event.listen(engine, "before_cursor_execute", before_cursor_execute)
            try:
                response = _heat(client, page_size=page_size)
                assert response.status_code == 200, response.text
            finally:
                event.remove(engine, "before_cursor_execute", before_cursor_execute)
            return len(statements)

        active_customer_queries = select_count_for(2)
        all_customer_queries = select_count_for(7)

    assert active_customer_queries <= 40
    assert all_customer_queries <= active_customer_queries + 2

    with TestClient(app) as client:
        _login(client, "heat-scoped")
        hidden_short_name = _heat(client, keyword="越权客")
    assert hidden_short_name.status_code == 200
    assert hidden_short_name.json()["total"] == 0
    assert "范围外高额客户" not in hidden_short_name.text


def test_customer_heat_routes_are_explicit_and_internal_badge_flag_is_not_public(
    customer_heat_app,
) -> None:
    app, _ids, _engine = customer_heat_app
    with TestClient(app) as client:
        schema = client.get("/openapi.json")
    assert schema.status_code == 200
    paths = schema.json()["paths"]
    assert "/api/orders/customer-heat" in paths
    assert "/api/orders/customer-heat/{customer_id}/orders" in paths
    order_parameters = paths["/api/orders"]["get"].get("parameters", [])
    assert "include_unfinished_total" not in {
        parameter["name"] for parameter in order_parameters
    }


def test_customer_heat_filters_groups_by_stage_customer_and_dates(
    customer_heat_app,
) -> None:
    app, ids, _engine = customer_heat_app
    with TestClient(app) as client:
        _login(client, "heat-admin")

        stage = _heat(client, stage="pending_confirmation")
        assert stage.status_code == 200, stage.text
        assert {row["customer_id"] for row in stage.json()["items"]} == {
            ids["hot"],
            ids["new"],
        }

        selected = _heat(
            client,
            customer_id=ids["new"],
            stage="pending_confirmation",
        )
        assert selected.status_code == 200, selected.text
        assert [row["customer_id"] for row in selected.json()["items"]] == [
            ids["new"]
        ]

        recent = _heat(client, order_date_from="2026-08-14")
        assert recent.status_code == 200, recent.text
        assert {row["customer_id"] for row in recent.json()["items"]} == {
            ids["hot"],
            ids["new"],
            ids["outside"],
        }

        _login(client, "heat-scoped")
        scoped = _heat(client, stage="pending_confirmation")
        assert scoped.status_code == 200, scoped.text
        assert {row["customer_id"] for row in scoped.json()["items"]} == {
            ids["hot"],
            ids["new"],
        }
        denied = _heat(client, customer_id=ids["outside"])
        assert denied.status_code == 403
