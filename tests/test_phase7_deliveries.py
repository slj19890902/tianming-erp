from __future__ import annotations

import sqlite3
from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def delivery_api_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deliveries import order_actions_router, router as deliveries_router
    from app.api.deps import get_db
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "deliveries.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        users = [
            User(
                username=role,
                password_hash=hash_password("RolePass123!"),
                role=role,
                real_name=role,
                display_name=role,
                must_change_password=False,
            )
            for role in ("admin", "finance", "sales", "workshop")
        ]
        customer = Customer(
            customer_number=1,
            customer_code="SME",
            name="苏州思迈尔包装有限公司",
            contact_person="张经理",
            phone="0512-66778899",
            address="苏州市吴中区东太湖路88号",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        other_customer = Customer(
            customer_number=2,
            customer_code="HC",
            name="昆山华诚电子有限公司",
            payment_term_days=30,
            credit_limit=Decimal("50000"),
        )
        session.add_all([*users, customer, other_customer])
        session.flush()
        products = [
            Product(
                customer_id=customer.id,
                product_code="SME-001",
                customer_material_code="KH-001",
                product_name="五层加强纸箱",
                legacy_material_text="K=A-BC",
                length_mm=Decimal("520"),
                width_mm=Decimal("350"),
                height_mm=Decimal("300"),
                box_category="normal",
            ),
            Product(
                customer_id=customer.id,
                product_code="SME-002",
                customer_material_code="KH-002",
                product_name="三层瓦楞外箱",
                legacy_material_text="A=B",
                length_mm=Decimal("380"),
                width_mm=Decimal("260"),
                height_mm=Decimal("220"),
                box_category="normal",
            ),
            Product(
                customer_id=other_customer.id,
                product_code="HC-001",
                customer_material_code="HC-001",
                product_name="电子件外箱",
                legacy_material_text="K=K",
                box_category="normal",
            ),
        ]
        session.add_all(products)
        session.flush()
        orders = [
            Order(
                order_number="PO-20260613-001",
                customer_id=customer.id,
                customer_po="CPO-001",
                order_date=date(2026, 6, 13),
                delivery_date=date(2026, 6, 15),
                status="pending_delivery",
                payment_status="unpaid",
                total_amount=Decimal("360"),
            ),
            Order(
                order_number="PO-20260613-002",
                customer_id=customer.id,
                customer_po="CPO-002",
                order_date=date(2026, 6, 13),
                delivery_date=date(2026, 6, 16),
                status="pending_delivery",
                payment_status="unpaid",
                total_amount=Decimal("245"),
            ),
            Order(
                order_number="PO-20260613-003",
                customer_id=other_customer.id,
                customer_po="HC-PO-001",
                order_date=date(2026, 6, 13),
                delivery_date=date(2026, 6, 17),
                status="pending_delivery",
                payment_status="unpaid",
                total_amount=Decimal("100"),
            ),
        ]
        session.add_all(orders)
        session.flush()
        session.add_all(
            [
                OrderItem(
                    order_id=orders[0].id,
                    product_id=products[0].id,
                    quantity=100,
                    unit_price=Decimal("3.60"),
                    subtotal=Decimal("360"),
                    material_status="received",
                    delivered_quantity=20,
                    snapshot_product_name="五层加强纸箱",
                    snapshot_spec="520×350×300mm",
                    snapshot_material="K=A-BC",
                ),
                OrderItem(
                    order_id=orders[1].id,
                    product_id=products[1].id,
                    quantity=100,
                    unit_price=Decimal("2.45"),
                    subtotal=Decimal("245"),
                    material_status="received",
                    delivered_quantity=0,
                    snapshot_product_name="三层瓦楞外箱",
                    snapshot_spec="380×260×220mm",
                    snapshot_material="A=B",
                ),
                OrderItem(
                    order_id=orders[1].id,
                    product_id=products[1].id,
                    quantity=50,
                    unit_price=Decimal("2.45"),
                    subtotal=Decimal("122.50"),
                    material_status="pending",
                    delivered_quantity=0,
                    snapshot_product_name="未到料产品",
                    snapshot_spec="380×260×220mm",
                    snapshot_material="A=B",
                ),
                OrderItem(
                    order_id=orders[1].id,
                    product_id=products[1].id,
                    quantity=30,
                    unit_price=Decimal("2.45"),
                    subtotal=Decimal("73.50"),
                    material_status="received",
                    delivered_quantity=0,
                    is_force_closed=True,
                    snapshot_product_name="已结案产品",
                    snapshot_spec="380×260×220mm",
                    snapshot_material="A=B",
                ),
                OrderItem(
                    order_id=orders[2].id,
                    product_id=products[2].id,
                    quantity=100,
                    unit_price=Decimal("1"),
                    subtotal=Decimal("100"),
                    material_status="received",
                    delivered_quantity=0,
                    snapshot_product_name="电子件外箱",
                    snapshot_spec="400×300×200mm",
                    snapshot_material="K=K",
                ),
            ]
        )
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(deliveries_router, prefix="/api/deliveries")
    app.include_router(order_actions_router, prefix="/api/orders")

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    return app, session_factory


def _login(client: TestClient, role: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": role, "password": "RolePass123!"},
    )
    assert response.status_code == 200


def _create_payload() -> dict:
    return {
        "customer_id": 1,
        "delivery_date": "2026-06-13",
        "vehicle_number": "苏E·12345",
        "items": [
            {"order_item_id": 1, "delivered_quantity": 30, "remarks": "第一批"},
            {"order_item_id": 2, "delivered_quantity": 40, "remarks": "急单"},
        ],
    }


def test_pending_items_use_strict_filter_and_remaining_quantity(
    delivery_api_app,
) -> None:
    app, _ = delivery_api_app
    with TestClient(app) as client:
        _login(client, "workshop")
        response = client.get("/api/deliveries/pending_items")

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["item_id"] for item in items] == [5, 2, 1]
    assert items[-1]["remaining_quantity"] == 80


def test_pending_items_batches_plain_received_rows_without_row_by_row_sql(
    delivery_api_app,
) -> None:
    """The ordinary material-received path stays query-bounded as rows grow."""

    from app.models.order import OrderItem

    app, session_factory = delivery_api_app
    with session_factory() as session:
        seed = session.get(OrderItem, 1)
        assert seed is not None
        session.add_all(
            OrderItem(
                order_id=seed.order_id,
                product_id=seed.product_id,
                quantity=100,
                delivered_quantity=0,
                unit_price=seed.unit_price,
                subtotal=seed.subtotal,
                material_status="received",
                snapshot_product_name=f"批量待送-{index}",
                snapshot_spec=seed.snapshot_spec,
                snapshot_material=seed.snapshot_material,
            )
            for index in range(20)
        )
        session.commit()

    statements: list[str] = []

    def record_sql(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lstrip().lower())

    engine = session_factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        with TestClient(app) as client:
            _login(client, "workshop")
            statements.clear()
            response = client.get("/api/deliveries/pending_items")
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)

    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) >= 22
    selects = [statement for statement in statements if statement.startswith("select")]
    assert len(selects) <= 20
    assert not any(statement.startswith(("insert", "update", "delete")) for statement in statements)


def test_create_combined_delivery_then_partial_dispatch_once(
    delivery_api_app,
) -> None:
    from app.models.delivery import Delivery
    from app.models.order import OrderItem

    app, session_factory = delivery_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/deliveries", json=_create_payload())
        delivery_id = created.json()["id"]
        dispatched = client.put(f"/api/deliveries/{delivery_id}/dispatch")
        repeated = client.put(f"/api/deliveries/{delivery_id}/dispatch")

    assert created.status_code == 201, created.text
    assert created.json()["delivery_number"] == "SME-20260613-001"
    assert created.json()["total_quantity"] == 70
    assert dispatched.status_code == 200
    assert dispatched.json()["status"] == "dispatched"
    assert repeated.status_code == 409
    with session_factory() as session:
        assert session.get(OrderItem, 1).delivered_quantity == 50
        assert session.get(OrderItem, 2).delivered_quantity == 40
        assert session.get(Delivery, delivery_id).status == "dispatched"


def test_new_delivery_stays_first_even_when_older_delivery_was_operated_later(
    delivery_api_app,
) -> None:
    from app.models.delivery import Delivery

    app, session_factory = delivery_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        older = client.post(
            "/api/deliveries",
            json={
                "customer_id": 1,
                "delivery_date": "2026-06-13",
                "items": [{"order_item_id": 1, "delivered_quantity": 1}],
            },
        )
        newer = client.post(
            "/api/deliveries",
            json={
                "customer_id": 1,
                "delivery_date": "2026-06-14",
                "items": [{"order_item_id": 2, "delivered_quantity": 1}],
            },
        )
        assert older.status_code == 201, older.text
        assert newer.status_code == 201, newer.text
        with session_factory() as session:
            session.get(Delivery, older.json()["id"]).printed_at = datetime(
                2099, 1, 1, 0, 0, 0
            )
            session.commit()
        listed = client.get("/api/deliveries")

    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()["items"][:2]] == [
        newer.json()["id"],
        older.json()["id"],
    ]


def test_over_delivery_warns_without_requiring_reason_and_can_be_dispatched(
    delivery_api_app,
) -> None:
    from app.models.order import OrderItem

    app, session_factory = delivery_api_app
    payload = {
        "customer_id": 1,
        "delivery_date": "2026-06-14",
        "items": [
            {"order_item_id": 1, "delivered_quantity": 90}
        ],
    }
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/deliveries", json=payload)
        dispatched = client.put(f"/api/deliveries/{created.json()['id']}/dispatch")

    assert created.status_code == 201, created.text
    assert created.json()["warnings"][0]["code"] == "OVER_DELIVERY"
    assert dispatched.status_code == 200, dispatched.text
    with session_factory() as session:
        assert session.get(OrderItem, 1).delivered_quantity == 110


def test_telescoping_lid_delivery_capacity_uses_min_received_components(
    delivery_api_app,
) -> None:
    from app.models.requisition import Requisition, RequisitionItem

    app, session_factory = delivery_api_app
    with session_factory() as session:
        requisition = Requisition(
            requisition_number="BL-20260613-001",
            requisition_date=date(2026, 6, 13),
            supplier_name="苏州纸板供应商",
            status="已报料",
        )
        session.add(requisition)
        session.flush()
        session.add_all(
            [
                RequisitionItem(
                    requisition_id=requisition.id,
                    order_item_id=2,
                    requisition_qty=100,
                    cardboard_len=Decimal("400"),
                    cardboard_width=Decimal("300"),
                    product_code_snapshot="SME-002",
                    product_name_snapshot="天地盖测试箱-盖",
                    specification_snapshot="380×260×220mm",
                    material_snapshot="A=B",
                    special_process="一开一",
                    status="已入库",
                ),
                RequisitionItem(
                    requisition_id=requisition.id,
                    order_item_id=2,
                    requisition_qty=99,
                    cardboard_len=Decimal("375"),
                    cardboard_width=Decimal("275"),
                    product_code_snapshot="SME-002",
                    product_name_snapshot="天地盖测试箱-底",
                    specification_snapshot="380×260×220mm",
                    material_snapshot="A=B",
                    special_process="一开一",
                    status="已入库",
                ),
            ]
        )
        session.commit()

    with TestClient(app) as client:
        _login(client, "admin")
        listed = client.get(
            "/api/deliveries/pending-items/search",
            params={"customer_id": 1, "list_all": "1"},
        )
        rejected = client.post(
            "/api/deliveries",
            json={
                "customer_id": 1,
                "delivery_date": "2026-06-13",
                "items": [
                    {
                        "order_item_id": 2,
                        "delivered_quantity": 100,
                        "remarks": "客户催单，仍申请超送",
                    }
                ],
            },
        )
        accepted = client.post(
            "/api/deliveries",
            json={
                "customer_id": 1,
                "delivery_date": "2026-06-13",
                "items": [{"order_item_id": 2, "delivered_quantity": 99}],
            },
        )

    assert listed.status_code == 200
    item2 = next(
        item for item in listed.json()["items"] if item["order_item_id"] == 2
    )
    assert item2["remaining_quantity"] == 99
    assert rejected.status_code == 400
    assert "物理可送数量为 99" in rejected.text
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["total_quantity"] == 99


def test_telescoping_lid_missing_component_rejects_even_with_reason(
    delivery_api_app,
) -> None:
    from app.models.requisition import Requisition, RequisitionItem

    app, session_factory = delivery_api_app
    with session_factory() as session:
        requisition = Requisition(
            requisition_number="BL-20260613-MISSING",
            requisition_date=date(2026, 6, 13),
            supplier_name="苏州纸板供应商",
            status="已报料",
        )
        session.add(requisition)
        session.flush()
        session.add(
            RequisitionItem(
                requisition_id=requisition.id,
                order_item_id=2,
                requisition_qty=100,
                cardboard_len=Decimal("400"),
                cardboard_width=Decimal("300"),
                product_code_snapshot="SME-002",
                product_name_snapshot="天地盖测试箱-盖",
                specification_snapshot="380×260×220mm",
                material_snapshot="A=B",
                special_process="一开一",
                status="已入库",
            )
        )
        session.commit()

    with TestClient(app) as client:
        _login(client, "admin")
        rejected = client.post(
            "/api/deliveries",
            json={
                "customer_id": 1,
                "delivery_date": "2026-06-13",
                "items": [
                    {
                        "order_item_id": 2,
                        "delivered_quantity": 1,
                        "remarks": "客户催单，仍申请超送",
                    }
                ],
            },
        )

    assert rejected.status_code == 400, rejected.text
    assert "物理可送数量为 0" in rejected.text


def test_telescoping_lid_delivery_search_uses_components_when_parent_status_stale(
    delivery_api_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.requisition import Requisition, RequisitionItem

    app, session_factory = delivery_api_app
    with session_factory() as session:
        item = session.get(OrderItem, 2)
        item.material_status = "pending"
        item.requisition_status = "已报料"
        requisition = Requisition(
            requisition_number="BL-20260613-002",
            requisition_date=date(2026, 6, 13),
            supplier_name="苏州纸板供应商",
            status="已报料",
        )
        session.add(requisition)
        session.flush()
        session.add_all(
            [
                RequisitionItem(
                    requisition_id=requisition.id,
                    order_item_id=2,
                    requisition_qty=100,
                    cardboard_len=Decimal("400"),
                    cardboard_width=Decimal("300"),
                    product_code_snapshot="SME-002",
                    product_name_snapshot="天地盖测试箱-盖",
                    specification_snapshot="380×260×220mm",
                    material_snapshot="A=B",
                    special_process="一开一",
                    status="已入库",
                ),
                RequisitionItem(
                    requisition_id=requisition.id,
                    order_item_id=2,
                    requisition_qty=99,
                    cardboard_len=Decimal("375"),
                    cardboard_width=Decimal("275"),
                    product_code_snapshot="SME-002",
                    product_name_snapshot="天地盖测试箱-底",
                    specification_snapshot="380×260×220mm",
                    material_snapshot="A=B",
                    special_process="一开一",
                    status="已入库",
                ),
            ]
        )
        session.commit()

    with TestClient(app) as client:
        _login(client, "admin")
        listed = client.get(
            "/api/deliveries/pending-items/search",
            params={"customer_id": 1, "list_all": "1"},
        )

    assert listed.status_code == 200
    item2 = next(
        item for item in listed.json()["items"] if item["order_item_id"] == 2
    )
    assert item2["customer_name"] == "苏州思迈尔包装有限公司"
    assert item2["remaining_quantity"] == 99


def test_delivery_can_be_marked_printed_and_returns_print_status(
    delivery_api_app,
) -> None:
    app, _ = delivery_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/deliveries", json=_create_payload())
        delivery_id = created.json()["id"]
        client.put(f"/api/deliveries/{delivery_id}/dispatch")
        printed = client.put(f"/api/deliveries/{delivery_id}/printed")
        detail = client.get(f"/api/deliveries/{delivery_id}")

    assert printed.status_code == 200
    assert printed.json()["is_printed"] is True
    assert detail.json()["printed_at"] is not None


def test_dispatch_rolls_back_all_lines_when_one_line_becomes_invalid(
    delivery_api_app,
) -> None:
    from app.models.delivery import Delivery
    from app.models.order import OrderItem

    app, session_factory = delivery_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/deliveries", json=_create_payload())
        delivery_id = created.json()["id"]
        with session_factory() as session:
            second = session.get(OrderItem, 2)
            second.is_force_closed = True
            session.commit()
        dispatched = client.put(f"/api/deliveries/{delivery_id}/dispatch")

    assert dispatched.status_code == 409
    with session_factory() as session:
        assert session.get(OrderItem, 1).delivered_quantity == 20
        assert session.get(OrderItem, 2).delivered_quantity == 0
        assert session.get(Delivery, delivery_id).status == "pending"


def test_force_close_requires_reason_hides_item_and_is_audited(
    delivery_api_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.order import OrderItem

    app, session_factory = delivery_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        invalid = client.put(
            "/api/orders/items/1/force_close",
            json={"reason": " "},
        )
        closed = client.put(
            "/api/orders/items/1/force_close",
            json={"reason": "客户确认尾数不再补做"},
        )
        pending = client.get("/api/deliveries/pending_items")

    assert invalid.status_code == 422
    assert closed.status_code == 200
    assert 1 not in [item["item_id"] for item in pending.json()["items"]]
    with session_factory() as session:
        assert session.get(OrderItem, 1).is_force_closed is True
        details = session.scalar(
            select(OperationLog.details).where(
                OperationLog.action == "FORCE_CLOSE_ORDER_ITEM",
                OperationLog.entity_id == 1,
            )
        )
    assert "客户确认尾数不再补做" in details


def test_finance_is_read_only_for_delivery_operations(delivery_api_app) -> None:
    app, _ = delivery_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        assert client.get("/api/deliveries").status_code == 200
        create = client.post("/api/deliveries", json=_create_payload())
        dispatch = client.put("/api/deliveries/1/dispatch")
        force_close = client.put(
            "/api/orders/items/1/force_close",
            json={"reason": "测试"},
        )

    assert create.status_code == 403
    assert dispatch.status_code == 403
    assert force_close.status_code == 403


def test_workshop_is_read_only(delivery_api_app) -> None:
    app, _ = delivery_api_app
    with TestClient(app) as client:
        _login(client, "workshop")
        assert client.get("/api/deliveries/pending_items").status_code == 200
        create = client.post("/api/deliveries", json=_create_payload())
        close = client.put(
            "/api/orders/items/1/force_close",
            json={"reason": "测试"},
        )

    assert create.status_code == 403
    assert close.status_code == 403


def _all_keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            key
            for child in value.values()
            for key in _all_keys(child)
        }
    if isinstance(value, list):
        return {key for child in value for key in _all_keys(child)}
    return set()


def test_print_response_contains_no_financial_fields(delivery_api_app) -> None:
    app, _ = delivery_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/deliveries", json=_create_payload())
        response = client.get(
            f"/api/deliveries/{created.json()['id']}/print"
        )

    assert response.status_code == 200
    keys = {key.lower() for key in _all_keys(response.json())}
    forbidden = {"unit_price", "subtotal", "cost", "cost_unit_price", "amount"}
    assert keys.isdisjoint(forbidden)
    assert response.json()["items"][0]["unit"] == "PCS"


def test_over_delivery_facts_are_not_appended_to_customer_remark(
    delivery_api_app,
) -> None:
    from app.models.delivery import DeliveryItem

    app, session_factory = delivery_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        payload = _create_payload()
        for line in payload["items"]:
            line["remarks"] = None
        created = client.post("/api/deliveries", json=payload)
        assert created.status_code == 201, created.text
        delivery_id = created.json()["id"]
        with session_factory() as session:
            item = session.scalar(
                select(DeliveryItem).where(
                    DeliveryItem.delivery_id == delivery_id
                )
            )
            item.over_delivery_quantity = 10
            item.ordered_quantity_snapshot = 40
            session.commit()
        printed = client.get(f"/api/deliveries/{delivery_id}/print")

    assert printed.status_code == 200, printed.text
    assert printed.json()["items"][0]["over_delivery_quantity"] == 10
    assert printed.json()["items"][0]["remarks"] is None


def test_new_delivery_uses_customer_prefix_without_changing_historical_dh(
    delivery_api_app,
) -> None:
    from app.models.delivery import Delivery

    app, session_factory = delivery_api_app
    with session_factory() as session:
        historical = Delivery(
            delivery_number="DH-20260612-001",
            customer_id=1,
            delivery_date=date(2026, 6, 12),
            status="dispatched",
            total_quantity=70,
        )
        session.add(historical)
        session.flush()
        historical_id = historical.id
        session.commit()

    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/deliveries", json=_create_payload())

    assert created.status_code == 201, created.text
    assert created.json()["delivery_number"] == "SME-20260613-001"
    with session_factory() as session:
        assert session.get(Delivery, historical_id).delivery_number == "DH-20260612-001"


def test_print_data_uses_product_name_and_customer_remark_only(
    delivery_api_app,
) -> None:
    from app.models.order import OrderItem

    app, session_factory = delivery_api_app
    long_name = "超长产品名称用于验证送货单打印时自动换行并在必要时缩小一个字号而不截断"
    with session_factory() as session:
        order_item = session.get(OrderItem, 1)
        order_item.snapshot_product_name = long_name
        order_item.snapshot_production_notes = "生产备注不得出现在送货单打印中"
        session.commit()

    payload = _create_payload()
    payload["items"][0]["remarks"] = "客户备注需要完整显示在底部备注说明"
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/deliveries", json=payload)
        detail = client.get(f"/api/deliveries/{created.json()['id']}")
        response = client.get(f"/api/deliveries/{created.json()['id']}/print")

    assert detail.status_code == 200, detail.text
    assert detail.json()["items"][0]["remarks"] == "客户备注需要完整显示在底部备注说明"
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["product_name"] == long_name
    assert item["remarks"] == "客户备注需要完整显示在底部备注说明"
    assert "production_notes" not in item


def test_delivery_freezes_product_name_and_specification_at_draft_creation(
    delivery_api_app,
) -> None:
    from app.models.delivery import DeliveryItem
    from app.models.order import OrderItem
    from app.models.product import Product

    app, session_factory = delivery_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/deliveries", json=_create_payload())
        assert created.status_code == 201, created.text
        delivery_id = created.json()["id"]

        with session_factory() as session:
            delivery_item = session.scalar(
                select(DeliveryItem).where(
                    DeliveryItem.delivery_id == delivery_id,
                    DeliveryItem.order_item_id == 1,
                )
            )
            assert delivery_item is not None
            assert delivery_item.product_name_snapshot == "五层加强纸箱"
            assert delivery_item.specification_snapshot == "520×350×300mm"

            order_item = session.get(OrderItem, 1)
            product = session.get(Product, order_item.product_id)
            order_item.snapshot_product_name = "订单后来被改名"
            order_item.snapshot_spec = "999×999×999mm"
            product.product_name = "常用箱后来被改名"
            product.length_mm = Decimal("999")
            session.commit()

        printed = client.get(f"/api/deliveries/{delivery_id}/print")
        detail = client.get(f"/api/deliveries/{delivery_id}")

    assert printed.status_code == 200, printed.text
    assert detail.status_code == 200, detail.text
    for response in (printed.json(), detail.json()):
        item = response["items"][0]
        assert item["product_name"] == "五层加强纸箱"
        assert item["specification"] == "520×350×300mm"


def test_legacy_delivery_missing_own_and_order_snapshots_does_not_guess_product(
    delivery_api_app,
) -> None:
    from app.models.delivery import DeliveryItem
    from app.models.order import OrderItem

    app, session_factory = delivery_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/deliveries", json=_create_payload())
        assert created.status_code == 201, created.text
        delivery_id = created.json()["id"]

        with session_factory() as session:
            delivery_item = session.scalar(
                select(DeliveryItem).where(
                    DeliveryItem.delivery_id == delivery_id,
                    DeliveryItem.order_item_id == 1,
                )
            )
            order_item = session.get(OrderItem, 1)
            delivery_item.product_code_snapshot = None
            delivery_item.product_name_snapshot = None
            delivery_item.specification_snapshot = None
            order_item.snapshot_product_code = None
            order_item.snapshot_product_name = ""
            order_item.snapshot_spec = None
            session.commit()

        printed = client.get(f"/api/deliveries/{delivery_id}/print")
        detail = client.get(f"/api/deliveries/{delivery_id}")

    assert printed.status_code == 200, printed.text
    assert detail.status_code == 200, detail.text
    for response in (printed.json(), detail.json()):
        item = response["items"][0]
        assert item["product_code"] == "存货编码未登记"
        assert item["product_name"] == "产品名称未登记"
        assert item["specification"] == "规格未登记"


def test_clearing_customer_remark_hides_it_from_detail_and_print(
    delivery_api_app,
) -> None:
    app, _ = delivery_api_app
    payload = _create_payload()
    payload["items"][0]["remarks"] = "请核对数量后签字"
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/deliveries", json=payload)
        delivery_id = created.json()["id"]
        cleared_payload = {
            "customer_id": payload["customer_id"],
            "delivery_date": payload["delivery_date"],
            "vehicle_number": payload.get("vehicle_number"),
            "items": [
                {
                    "order_item_id": item["order_item_id"],
                    "delivered_quantity": item["delivered_quantity"],
                    "remarks": None,
                }
                for item in payload["items"]
            ],
        }
        cleared = client.put(
            f"/api/deliveries/{delivery_id}",
            json=cleared_payload,
        )
        detail = client.get(f"/api/deliveries/{delivery_id}")
        printed = client.get(f"/api/deliveries/{delivery_id}/print")

    assert created.status_code == 201, created.text
    assert cleared.status_code == 200, cleared.text
    assert all(item["remarks"] is None for item in detail.json()["items"])
    assert all(item["remarks"] is None for item in printed.json()["items"])


def test_mixed_customer_delivery_is_rejected_without_draft(
    delivery_api_app,
) -> None:
    from app.models.delivery import Delivery

    app, session_factory = delivery_api_app
    payload = _create_payload()
    payload["items"][1]["order_item_id"] = 5
    with TestClient(app) as client:
        _login(client, "admin")
        response = client.post("/api/deliveries", json=payload)

    assert response.status_code == 400
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Delivery)) == 0


def test_delivery_list_returns_customer_and_line_details(
    delivery_api_app,
) -> None:
    app, _ = delivery_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/deliveries", json=_create_payload())
        assert created.status_code == 201, created.text
        listed = client.get("/api/deliveries")

    assert listed.status_code == 200, listed.text
    row = listed.json()["items"][0]
    assert row["delivery_number"].startswith("SME-")
    assert row["customer_name"]
    assert row["items"][0]["order_item_id"] == 1
    assert row["return_receipt_status"] is None


def test_route_suggestions_group_deliverable_customers_and_build_safe_map_legs(
    delivery_api_app,
) -> None:
    from app.models.company_config import CompanyConfig
    from app.models.customer import Customer

    app, session_factory = delivery_api_app
    with session_factory() as session:
        session.add(
            CompanyConfig(
                id=1,
                company_name="天明包装",
                address="苏州市相城区渭塘镇测试路1号",
            )
        )
        other = session.get(Customer, 2)
        other.address = "昆山市玉山镇测试路2号"
        session.commit()

    with TestClient(app) as client:
        _login(client, "admin")
        response = client.get("/api/deliveries/route-suggestions")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["planning_mode"] == "regional_grouping_with_segment_navigation"
    assert body["customer_count"] == 2
    assert "不是实时路况最优解" in body["disclaimer"]
    groups = {row["area"]: row for row in body["groups"]}
    assert set(groups) == {"吴中区", "昆山市"}
    assert groups["吴中区"]["pending_item_count"] == 2
    assert groups["昆山市"]["pending_quantity"] == 100
    first_leg = groups["吴中区"]["customers"][0]
    assert first_leg["navigation_from"] == "苏州市相城区渭塘镇测试路1号"
    assert first_leg["navigation_url"].startswith(
        "https://api.map.baidu.com/direction?"
    )
    assert "mode=driving" in first_leg["navigation_url"]
    assert "src=webapp.tianming.erp" in first_leg["navigation_url"]


def test_route_suggestions_static_path_precedes_delivery_id_route() -> None:
    from app.api.deliveries import router

    paths = [getattr(route, "path", "") for route in router.routes]
    assert paths.index("/route-suggestions") < paths.index("/{delivery_id}")


def test_route_suggestions_mark_same_address_customers_as_one_stop(
    delivery_api_app,
) -> None:
    from app.models.company_config import CompanyConfig
    from app.models.customer import Customer

    app, session_factory = delivery_api_app
    same_address = "苏州市工业园区测试路 8 号"
    with session_factory() as session:
        session.add(
            CompanyConfig(
                id=1,
                company_name="天明包装",
                address="苏州市相城区渭塘镇测试路1号",
            )
        )
        session.get(Customer, 1).address = same_address
        session.get(Customer, 2).address = "苏州市工业园区测试路8号"
        session.commit()

    with TestClient(app) as client:
        _login(client, "admin")
        response = client.get("/api/deliveries/route-suggestions")

    assert response.status_code == 200, response.text
    customers = response.json()["groups"][0]["customers"]
    assert customers[0]["navigation_url"]
    assert customers[1]["same_as_previous_address"] is True
    assert customers[1]["navigation_url"] is None
    assert customers[1]["navigation_note"] == "与上一站同地址，可同站处理"


def test_delivery_list_keeps_newest_created_delivery_first(delivery_api_app) -> None:
    app, _ = delivery_api_app
    first_payload = {
        "customer_id": 1,
        "delivery_date": "2026-06-13",
        "items": [{"order_item_id": 1, "delivered_quantity": 10}],
    }
    second_payload = {
        "customer_id": 1,
        "delivery_date": "2026-06-13",
        "items": [{"order_item_id": 2, "delivered_quantity": 10}],
    }
    with TestClient(app) as client:
        _login(client, "admin")
        first = client.post("/api/deliveries", json=first_payload)
        second = client.post("/api/deliveries", json=second_payload)
        assert first.status_code == 201, first.text
        assert second.status_code == 201, second.text
        dispatched = client.put(f"/api/deliveries/{first.json()['id']}/dispatch")
        assert dispatched.status_code == 200, dispatched.text
        listed = client.get("/api/deliveries")

    assert listed.status_code == 200
    ids = [row["id"] for row in listed.json()["items"]]
    assert ids[:2] == [second.json()["id"], first.json()["id"]]


def test_phase7_migration_preserves_legacy_delivery_tables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE deliveries (
                id INTEGER PRIMARY KEY,
                delivery_no TEXT NOT NULL
            );
            CREATE TABLE delivery_items (
                id INTEGER PRIMARY KEY,
                delivery_id INTEGER NOT NULL
            );
            INSERT INTO deliveries VALUES (7, 'DN20260520001');
            INSERT INTO delivery_items VALUES (8, 7);
            """
        )
        connection.commit()

    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "phase7-migration-test")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        order_item_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(sales_order_items)"
            )
        }
        legacy_delivery = connection.execute(
            "SELECT id, delivery_no FROM deliveries"
        ).fetchone()
        legacy_item = connection.execute(
            "SELECT id, delivery_id FROM delivery_items"
        ).fetchone()

    assert {
        "sales_deliveries",
        "sales_delivery_items",
        "delivery_daily_sequences",
    } <= tables
    assert {"delivered_quantity", "is_force_closed"} <= order_item_columns
    assert legacy_delivery == (7, "DN20260520001")
    assert legacy_item == (8, 7)


def test_print_html_has_required_text_and_no_money_bindings() -> None:
    project_root = Path(__file__).resolve().parents[1]
    source = (project_root / "static" / "delivery-print.html").read_text(
        encoding="utf-8"
    )

    for required in (
        "苏州天明包装有限公司",
        "送货单",
        "客户单号",
        "<th>序号</th>",
        "款号",
        "产品名称 / 规格",
        "<th>单位</th>",
        "备注说明",
        "白联:存档",
        "红联:客户",
        "黄联:回单",
        "@media print",
        "size: 241mm 139.5mm",
        ".product-name.long-name",
        ".product-spec",
        "overflow-wrap: anywhere",
        'data-field="pageNumber"',
        "本页数量",
        "pageFits",
        "if (!pageFits([entry]))",
        "当前纸张高度无法完整打印",
        "break-after: page",
        "`${pageNumber}/${totalPages}页`",
            "/api/system/delivery-print-settings",
            "--paper-width",
            "--paper-height",
            "BroadcastChannel",
            "open_token",
        ):
        assert required in source
    assert "单位(PCS)" not in source
    assert "production_notes" not in source
    lowered = source.lower()
    for forbidden in ("unit_price", "subtotal", "cost_unit_price", "成本", "单价"):
        assert forbidden not in lowered


def test_delivery_print_product_code_removes_appended_description() -> None:
    from app.api.deliveries import _print_product_code

    assert _print_product_code("10250186 / 两口离子风机纸箱 67*23*15") == "10250186"
    assert _print_product_code("21312009") == "21312009"
