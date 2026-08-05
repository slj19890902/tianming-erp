"""Phase 1: 送货单状态流闭环 —— 编辑 / 删除 / 取消发货 / 数量回滚 / 门禁 / 权限。

状态机（用户拍板 2026-06-27）：
- 保存后 pending：可编辑、可删除。
- 确认发货 dispatched。
- 取消发货：dispatched -> pending，回滚已送数量并重算订单状态，
  清除发货人/发货时间/打印状态。
- 有回单禁止取消；已对账优先提示禁止；本阶段不开放反审核。
- 不新增 draft/cancelled 数据库状态。
- 所有写操作事务化并记录日志。
"""
from __future__ import annotations

from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from pathlib import Path
from threading import Barrier

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def delivery_api_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deliveries import order_actions_router, router as deliveries_router
    from app.api.deps import get_db
    from app.api.finance import router as finance_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserPermissionOverride
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "phase1_delivery.sqlite3")
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
        sales = next(user for user in users if user.username == "sales")
        session.add_all(
            [
                UserPermissionOverride(
                    user_id=sales.id,
                    permission_code="deliveries.view",
                    is_allowed=True,
                ),
                UserPermissionOverride(
                    user_id=sales.id,
                    permission_code="deliveries.execute",
                    is_allowed=True,
                ),
            ]
        )
        products = [
            Product(
                customer_id=customer.id,
                product_code="SME-001",
                customer_material_code="KH-001",
                product_name="五层加强纸箱",
                legacy_material_text="K=A-BC",
                box_category="normal",
            ),
            Product(
                customer_id=customer.id,
                product_code="SME-002",
                customer_material_code="KH-002",
                product_name="三层瓦楞外箱",
                legacy_material_text="A=B",
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
        ]
        session.add_all(orders)
        session.flush()
        # item1: 已送20/100; item2: 0/100; item3: 0/80
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
                    quantity=80,
                    unit_price=Decimal("2.45"),
                    subtotal=Decimal("196"),
                    material_status="received",
                    delivered_quantity=0,
                    snapshot_product_name="三层瓦楞外箱-备",
                    snapshot_spec="380×260×220mm",
                    snapshot_material="A=B",
                ),
            ]
        )
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(deliveries_router, prefix="/api/deliveries")
    app.include_router(order_actions_router, prefix="/api/orders")
    app.include_router(finance_router, prefix="/api/finance")

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
    assert response.status_code == 200, response.text


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


def _create_pending(client: TestClient) -> int:
    created = client.post("/api/deliveries", json=_create_payload())
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _create_and_dispatch(client: TestClient) -> int:
    delivery_id = _create_pending(client)
    dispatched = client.put(f"/api/deliveries/{delivery_id}/dispatch")
    assert dispatched.status_code == 200, dispatched.text
    return delivery_id


def _make_return_receipt(session_factory, delivery_id: int, *, with_statement: bool):
    """直接构造回单（可选对账），用于门禁测试。"""
    from app.models.delivery import DeliveryItem
    from app.models.finance import (
        ReturnReceipt,
        ReturnReceiptItem,
        Statement,
        StatementItem,
    )

    with session_factory() as session:
        receipt = ReturnReceipt(
            delivery_id=delivery_id,
            actual_received_date=date(2026, 6, 14),
            signed_by="客户王经理",
            status="confirmed",
        )
        session.add(receipt)
        session.flush()
        lines = session.scalars(
            select(DeliveryItem).where(DeliveryItem.delivery_id == delivery_id)
        ).all()
        receipt_items = []
        for line in lines:
            ri = ReturnReceiptItem(
                return_receipt_id=receipt.id,
                delivery_item_id=line.id,
                actual_received_quantity=line.delivered_quantity,
            )
            session.add(ri)
            receipt_items.append(ri)
        session.flush()
        if with_statement:
            statement = Statement(
                statement_number="ST-202606-001",
                customer_id=1,
                statement_month="2026-06",
                total_receivable=Decimal("0"),
                total_gross_profit=Decimal("0"),
                status="unsettled",
            )
            session.add(statement)
            session.flush()
            for ri in receipt_items:
                session.add(
                    StatementItem(
                        statement_id=statement.id,
                        return_receipt_item_id=ri.id,
                        actual_received_quantity=ri.actual_received_quantity,
                        unit_price_snapshot=Decimal("2.45"),
                        unit_cost_snapshot=Decimal("1.50"),
                        receivable_amount=Decimal("0"),
                        gross_profit_amount=Decimal("0"),
                    )
                )
        session.commit()


def _race_requests(*requests):
    """让多个独立 HTTP 客户端同时发起写请求。"""
    barrier = Barrier(len(requests) + 1)

    def run(request):
        barrier.wait()
        return request()

    with ThreadPoolExecutor(max_workers=len(requests)) as pool:
        futures = [pool.submit(run, request) for request in requests]
        barrier.wait()
        return [future.result() for future in futures]


# --------------------------------------------------------------------------- #
# 编辑
# --------------------------------------------------------------------------- #
def test_edit_pending_updates_fields_and_keeps_number(delivery_api_app) -> None:
    from app.models.delivery import Delivery, DeliveryItem

    app, session_factory = delivery_api_app
    with TestClient(app) as client:
        _login(client, "sales")
        delivery_id = _create_pending(client)
        original_number = client.get(
            f"/api/deliveries/{delivery_id}"
        ).json()["delivery_number"]
        edited = client.put(
            f"/api/deliveries/{delivery_id}",
            json={
                "delivery_date": "2026-06-14",
                "vehicle_number": "苏E·99999",
                "items": [
                    {"order_item_id": 1, "delivered_quantity": 10},
                    {"order_item_id": 3, "delivered_quantity": 25, "remarks": "改单"},
                ],
            },
        )

    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert body["delivery_number"] == original_number
    assert body["vehicle_number"] == "苏E·99999"
    assert body["total_quantity"] == 35
    assert {it["order_item_id"] for it in body["items"]} == {1, 3}
    with session_factory() as session:
        delivery = session.get(Delivery, delivery_id)
        assert delivery.total_quantity == 35
        # 编辑不应改动订单已送数量（pending 阶段从未累计）
        from app.models.order import OrderItem

        assert session.get(OrderItem, 1).delivered_quantity == 20
        assert session.get(OrderItem, 2).delivered_quantity == 0
        line_items = session.scalars(
            select(DeliveryItem).where(DeliveryItem.delivery_id == delivery_id)
        ).all()
        assert len(line_items) == 2


def test_edit_rejected_after_dispatch(delivery_api_app) -> None:
    app, _ = delivery_api_app
    with TestClient(app) as client:
        _login(client, "sales")
        delivery_id = _create_and_dispatch(client)
        edited = client.put(
            f"/api/deliveries/{delivery_id}",
            json={"vehicle_number": "苏E·00000", "items": [
                {"order_item_id": 1, "delivered_quantity": 5}
            ]},
        )
    assert edited.status_code == 409, edited.text


def test_edit_requires_operate_role(delivery_api_app) -> None:
    app, _ = delivery_api_app
    with TestClient(app) as client:
        _login(client, "sales")
        delivery_id = _create_pending(client)
    payload = {"items": [{"order_item_id": 1, "delivered_quantity": 5}]}
    for role in ("finance", "workshop"):
        with TestClient(app) as client:
            _login(client, role)
            resp = client.put(f"/api/deliveries/{delivery_id}", json=payload)
            assert resp.status_code == 403, f"{role}: {resp.text}"


def test_edit_and_dispatch_keep_lines_and_quantities_consistent(
    delivery_api_app,
) -> None:
    """编辑与发货竞争后，已送数量必须对应最终送货单明细。"""
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.order import OrderItem

    app, session_factory = delivery_api_app
    with TestClient(app) as setup_client:
        _login(setup_client, "sales")
        delivery_id = _create_pending(setup_client)

    edit_payload = {
        "delivery_date": "2026-06-14",
        "vehicle_number": "苏E·并发",
        "items": [
            {"order_item_id": 1, "delivered_quantity": 10},
            {"order_item_id": 3, "delivered_quantity": 25},
        ],
    }

    def edit_request():
        with TestClient(app) as client:
            _login(client, "sales")
            return client.put(
                f"/api/deliveries/{delivery_id}",
                json=edit_payload,
            )

    def dispatch_request():
        with TestClient(app) as client:
            _login(client, "sales")
            return client.put(f"/api/deliveries/{delivery_id}/dispatch")

    edited, dispatched = _race_requests(edit_request, dispatch_request)
    assert edited.status_code in (200, 409), edited.text
    assert dispatched.status_code in (200, 409), dispatched.text
    assert 200 in (edited.status_code, dispatched.status_code)

    with session_factory() as session:
        delivery = session.get(Delivery, delivery_id)
        lines = session.scalars(
            select(DeliveryItem).where(
                DeliveryItem.delivery_id == delivery_id
            )
        ).all()
        line_quantities = {
            line.order_item_id: line.delivered_quantity for line in lines
        }
        actual = {
            item_id: session.get(OrderItem, item_id).delivered_quantity
            for item_id in (1, 2, 3)
        }
        initial = {1: 20, 2: 0, 3: 0}
        if delivery.status == "dispatched":
            assert actual == {
                item_id: initial[item_id] + line_quantities.get(item_id, 0)
                for item_id in initial
            }
        else:
            assert delivery.status == "pending"
            assert actual == initial


# --------------------------------------------------------------------------- #
# 删除
# --------------------------------------------------------------------------- #
def test_delete_pending_does_not_touch_order_quantity(delivery_api_app) -> None:
    from app.models.delivery import Delivery
    from app.models.order import OrderItem

    app, session_factory = delivery_api_app
    with TestClient(app) as client:
        _login(client, "sales")
        delivery_id = _create_pending(client)
        deleted = client.delete(f"/api/deliveries/{delivery_id}")
        repeated = client.delete(f"/api/deliveries/{delivery_id}")

    assert deleted.status_code in (200, 204), deleted.text
    assert deleted.json() == {
        "deleted": True,
        "voided": False,
        "disposition": "deleted",
        "id": delivery_id,
    }
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == deleted.json()
    with session_factory() as session:
        assert session.get(Delivery, delivery_id) is None
        assert session.scalar(select(func.count()).select_from(Delivery)) == 0
        assert session.get(OrderItem, 1).delivered_quantity == 20
        assert session.get(OrderItem, 2).delivered_quantity == 0


def test_delete_rejected_after_dispatch(delivery_api_app) -> None:
    from app.models.delivery import Delivery

    app, session_factory = delivery_api_app
    with TestClient(app) as client:
        _login(client, "sales")
        delivery_id = _create_and_dispatch(client)
        deleted = client.delete(f"/api/deliveries/{delivery_id}")

    assert deleted.status_code == 409, deleted.text
    with session_factory() as session:
        assert session.get(Delivery, delivery_id) is not None


def test_delete_after_cancel_voids_without_changing_order_quantity(
    delivery_api_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.order import OrderItem

    app, session_factory = delivery_api_app
    with TestClient(app) as client:
        _login(client, "sales")
        delivery_id = _create_and_dispatch(client)
        cancelled = client.put(f"/api/deliveries/{delivery_id}/cancel")
        before_quantities = {
            item_id: session_quantity
            for item_id, session_quantity in (
                (1, 20),
                (2, 0),
            )
        }
        removed = client.delete(f"/api/deliveries/{delivery_id}")
        repeated = client.delete(f"/api/deliveries/{delivery_id}")
        default_list = client.get("/api/deliveries")
        voided_list = client.get(
            "/api/deliveries",
            params={"status": "voided"},
        )

    assert cancelled.status_code == 200, cancelled.text
    assert removed.status_code == 200, removed.text
    assert removed.json() == {
        "deleted": False,
        "voided": True,
        "disposition": "voided",
        "id": delivery_id,
    }
    assert repeated.json() == removed.json()
    assert all(
        row["id"] != delivery_id
        for row in default_list.json()["items"]
    )
    assert any(
        row["id"] == delivery_id
        for row in voided_list.json()["items"]
    )
    with session_factory() as session:
        delivery = session.get(Delivery, delivery_id)
        assert delivery.status == "voided"
        assert delivery.ever_dispatched_at is not None
        assert delivery.voided_at is not None
        assert session.scalar(
            select(func.count(DeliveryItem.id)).where(
                DeliveryItem.delivery_id == delivery_id
            )
        ) == 2
        assert {
            item_id: session.get(OrderItem, item_id).delivered_quantity
            for item_id in before_quantities
        } == before_quantities
        assert session.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.resource == "Delivery",
                OperationLog.entity_id == delivery_id,
                OperationLog.action == "VOID_AFTER_CANCEL",
            )
        ) == 1


def test_delete_unknown_restrict_reference_returns_chinese_conflict(
    delivery_api_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.delivery import Delivery

    app, session_factory = delivery_api_app
    with TestClient(app) as client:
        _login(client, "sales")
        delivery_id = _create_pending(client)
        with session_factory() as session:
            session.execute(
                text(
                    """
                    CREATE TABLE delivery_delete_test_blockers (
                        id INTEGER PRIMARY KEY,
                        delivery_id INTEGER NOT NULL,
                        FOREIGN KEY(delivery_id)
                            REFERENCES sales_deliveries(id)
                            ON DELETE RESTRICT
                    )
                    """
                )
            )
            session.execute(
                text(
                    """
                    INSERT INTO delivery_delete_test_blockers
                        (id, delivery_id)
                    VALUES (1, :delivery_id)
                    """
                ),
                {"delivery_id": delivery_id},
            )
            session.commit()
        removed = client.delete(f"/api/deliveries/{delivery_id}")

    assert removed.status_code == 409, removed.text
    assert "关联业务记录" in removed.json()["detail"]
    assert "不能物理删除" in removed.json()["detail"]
    with session_factory() as session:
        assert session.get(Delivery, delivery_id) is not None
        assert session.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.resource == "Delivery",
                OperationLog.entity_id == delivery_id,
                OperationLog.action.in_(("DELETE", "VOID_AFTER_CANCEL")),
            )
        ) == 0


def test_delete_requires_operate_role(delivery_api_app) -> None:
    app, _ = delivery_api_app
    with TestClient(app) as client:
        _login(client, "sales")
        delivery_id = _create_pending(client)
    for role in ("finance", "workshop"):
        with TestClient(app) as client:
            _login(client, role)
            resp = client.delete(f"/api/deliveries/{delivery_id}")
            assert resp.status_code == 403, f"{role}: {resp.text}"


def test_delete_and_dispatch_cannot_both_succeed(delivery_api_app) -> None:
    """删除和发货竞争时只能有一个成功，不能留下无送货单的已送数量。"""
    from app.models.delivery import Delivery

    app, session_factory = delivery_api_app
    with TestClient(app) as setup_client:
        _login(setup_client, "sales")
        delivery_id = _create_pending(setup_client)

    def delete_request():
        with TestClient(app) as client:
            _login(client, "sales")
            return client.delete(f"/api/deliveries/{delivery_id}")

    def dispatch_request():
        with TestClient(app) as client:
            _login(client, "sales")
            return client.put(f"/api/deliveries/{delivery_id}/dispatch")

    deleted, dispatched = _race_requests(delete_request, dispatch_request)
    assert not (
        deleted.status_code == 200 and dispatched.status_code == 200
    ), (deleted.text, dispatched.text)

    with session_factory() as session:
        delivery = session.get(Delivery, delivery_id)
        if delivery is None:
            from app.models.order import OrderItem

            assert session.get(OrderItem, 1).delivered_quantity == 20
            assert session.get(OrderItem, 2).delivered_quantity == 0


# --------------------------------------------------------------------------- #
# 取消发货 + 数量回滚
# --------------------------------------------------------------------------- #
def test_cancel_dispatch_rolls_back_quantity_and_status(delivery_api_app) -> None:
    from app.models.delivery import Delivery
    from app.models.order import Order, OrderItem

    app, session_factory = delivery_api_app
    with TestClient(app) as client:
        _login(client, "sales")
        delivery_id = _create_and_dispatch(client)
        with session_factory() as session:
            assert session.get(OrderItem, 1).delivered_quantity == 50
            assert session.get(OrderItem, 2).delivered_quantity == 40
        cancelled = client.put(f"/api/deliveries/{delivery_id}/cancel")
        detail = client.get(f"/api/deliveries/{delivery_id}")

    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "pending"
    assert detail.json()["dispatched_at"] is None
    assert detail.json()["is_printed"] is False
    with session_factory() as session:
        # 精确扣回
        assert session.get(OrderItem, 1).delivered_quantity == 20
        assert session.get(OrderItem, 2).delivered_quantity == 0
        assert session.get(Delivery, delivery_id).status == "pending"
        # 订单重回待送范围
        assert session.get(Order, 1).status == "partially_delivered"
        assert session.get(Order, 2).status == "pending_delivery"


def test_cancel_twice_is_blocked(delivery_api_app) -> None:
    from app.models.order import OrderItem

    app, session_factory = delivery_api_app
    with TestClient(app) as client:
        _login(client, "sales")
        delivery_id = _create_and_dispatch(client)
        first = client.put(f"/api/deliveries/{delivery_id}/cancel")
        second = client.put(f"/api/deliveries/{delivery_id}/cancel")

    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text
    with session_factory() as session:
        # 不能二次扣回
        assert session.get(OrderItem, 1).delivered_quantity == 20
        assert session.get(OrderItem, 2).delivered_quantity == 0


def test_cancel_blocked_when_return_receipt_exists(delivery_api_app) -> None:
    from app.models.order import OrderItem

    app, session_factory = delivery_api_app
    with TestClient(app) as client:
        _login(client, "sales")
        delivery_id = _create_and_dispatch(client)
    _make_return_receipt(session_factory, delivery_id, with_statement=False)
    with TestClient(app) as client:
        _login(client, "sales")
        cancelled = client.put(f"/api/deliveries/{delivery_id}/cancel")

    assert cancelled.status_code == 409, cancelled.text
    assert "回单" in cancelled.json()["detail"]
    with session_factory() as session:
        # 门禁拦截，数量未回滚
        assert session.get(OrderItem, 1).delivered_quantity == 50
        assert session.get(OrderItem, 2).delivered_quantity == 40


def test_cancel_blocked_when_statement_exists(delivery_api_app) -> None:
    from app.models.order import OrderItem

    app, session_factory = delivery_api_app
    with TestClient(app) as client:
        _login(client, "sales")
        delivery_id = _create_and_dispatch(client)
    _make_return_receipt(session_factory, delivery_id, with_statement=True)
    with TestClient(app) as client:
        _login(client, "sales")
        cancelled = client.put(f"/api/deliveries/{delivery_id}/cancel")

    assert cancelled.status_code == 409, cancelled.text
    assert "对账" in cancelled.json()["detail"]
    with session_factory() as session:
        assert session.get(OrderItem, 1).delivered_quantity == 50
        assert session.get(OrderItem, 2).delivered_quantity == 40


def test_cancel_and_create_receipt_cannot_both_succeed(
    delivery_api_app,
) -> None:
    """取消发货和确认回单竞争时，只能有一个业务动作成功。"""
    from app.models.delivery import Delivery
    from app.models.finance import ReturnReceipt

    app, session_factory = delivery_api_app
    with TestClient(app) as setup_client:
        _login(setup_client, "sales")
        delivery_id = _create_and_dispatch(setup_client)
        delivery = setup_client.get(
            f"/api/deliveries/{delivery_id}"
        ).json()

    receipt_payload = {
        "delivery_id": delivery_id,
        "actual_received_date": "2026-06-14",
        "signed_by": "并发测试",
        "items": [
            {
                "delivery_item_id": item["id"],
                "actual_received_quantity": item["delivered_quantity"],
                "difference_reason": None,
            }
            for item in delivery["items"]
        ],
    }

    def cancel_request():
        with TestClient(app) as client:
            _login(client, "sales")
            return client.put(f"/api/deliveries/{delivery_id}/cancel")

    def receipt_request():
        with TestClient(app) as client:
            _login(client, "finance")
            return client.post(
                "/api/finance/return_receipts",
                json=receipt_payload,
            )

    cancelled, received = _race_requests(cancel_request, receipt_request)
    assert not (
        cancelled.status_code == 200 and received.status_code == 201
    ), (cancelled.text, received.text)

    with session_factory() as session:
        delivery_row = session.get(Delivery, delivery_id)
        receipt = session.scalar(
            select(ReturnReceipt).where(
                ReturnReceipt.delivery_id == delivery_id
            )
        )
        assert not (
            delivery_row.status == "pending"
            and receipt is not None
            and receipt.status == "confirmed"
        )


def test_delivery_frontend_preserves_line_remarks() -> None:
    index = (
        Path(__file__).resolve().parents[1] / "static" / "index.html"
    ).read_text(encoding="utf-8")

    assert "deliveryForm.lines" in index
    assert "searchDeliveryLine(line)" in index
    assert "remarks: line.remarks || null" in index
    assert "客户备注（会打印）" in index
    assert 'placeholder="仅填写给客户看的内容"' in index
    assert "内部说明（不打印）" in index


def test_pending_delivery_search_scopes_customer_and_empty_keyword(
    delivery_api_app,
) -> None:
    from app.models.order import Order, OrderItem
    from app.models.product import Product

    app, session_factory = delivery_api_app
    with session_factory() as session:
        other_product = Product(
            customer_id=2,
            product_code="SME-001",
            customer_material_code="HC-001",
            product_name="其他客户同编码产品",
            legacy_material_text="B=B",
            box_category="normal",
        )
        session.add(other_product)
        session.flush()
        other_order = Order(
            order_number="PO-20260613-099",
            customer_id=2,
            customer_po="HC-PO-001",
            order_date=date(2026, 6, 13),
            delivery_date=date(2026, 6, 18),
            status="pending_delivery",
            payment_status="unpaid",
            total_amount=Decimal("88"),
        )
        session.add(other_order)
        session.flush()
        session.add(
            OrderItem(
                order_id=other_order.id,
                product_id=other_product.id,
                quantity=30,
                unit_price=Decimal("2.93"),
                subtotal=Decimal("88"),
                material_status="received",
                delivered_quantity=0,
                snapshot_product_name="其他客户同编码产品",
                snapshot_spec="300×200×100mm",
                snapshot_material="B=B",
            )
        )
        session.commit()

    with TestClient(app) as client:
        _login(client, "sales")
        empty = client.get(
            "/api/deliveries/pending-items/search",
            params={"customer_id": 1, "inventory_code": ""},
        )
        matched_inventory_code = client.get(
            "/api/deliveries/pending-items/search",
            params={"customer_id": 1, "inventory_code": "SME-001"},
        )
        matched_q_inventory = client.get(
            "/api/deliveries/pending-items/search",
            params={"customer_id": 1, "q": "SME-001"},
        )
        matched_q_po = client.get(
            "/api/deliveries/pending-items/search",
            params={"customer_id": 1, "q": "CPO-001"},
        )
        matched_q_name = client.get(
            "/api/deliveries/pending-items/search",
            params={"customer_id": 1, "q": "五层"},
        )
        matched_search_type_po = client.get(
            "/api/deliveries/pending-items/search",
            params={"customer_id": 1, "q": "CPO-001", "search_type": "customer_po"},
        )
        matched_product_name = client.get(
            "/api/deliveries/pending-items/search",
            params={"customer_id": 1, "product_name": "三层瓦楞外箱"},
        )
        listed = client.get(
            "/api/deliveries/pending-items/search",
            params={"customer_id": 1, "list_all": "1"},
        )
        limited = client.get(
            "/api/deliveries/pending-items/search",
            params={"customer_id": 1, "list_all": "1", "limit": 2},
        )

    assert empty.status_code == 200
    assert empty.json()["items"] == []
    assert matched_inventory_code.status_code == 200
    inventory_items = matched_inventory_code.json()["items"]
    assert inventory_items
    assert all(item["customer_id"] == 1 for item in inventory_items)
    assert all(item["remaining_quantity"] > 0 for item in inventory_items)
    assert all(item["product_code"] == "SME-001" for item in inventory_items)
    assert matched_q_inventory.status_code == 200
    q_inventory_items = matched_q_inventory.json()["items"]
    assert q_inventory_items
    assert all(item["customer_id"] == 1 for item in q_inventory_items)
    assert all(item["product_code"] == "SME-001" for item in q_inventory_items)
    assert matched_q_po.status_code == 200
    po_items = matched_q_po.json()["items"]
    assert po_items
    assert all(item["customer_id"] == 1 for item in po_items)
    assert any(item["customer_po"] == "CPO-001" for item in po_items)
    assert matched_q_name.status_code == 200
    q_name_items = matched_q_name.json()["items"]
    assert q_name_items
    assert all(item["customer_id"] == 1 for item in q_name_items)
    assert all("五层" in item["product_name"] for item in q_name_items)
    assert matched_search_type_po.status_code == 200
    typed_po_items = matched_search_type_po.json()["items"]
    assert typed_po_items
    assert all(item["customer_id"] == 1 for item in typed_po_items)
    assert all(item["customer_po"] == "CPO-001" for item in typed_po_items)
    assert matched_product_name.status_code == 200
    name_items = matched_product_name.json()["items"]
    assert name_items
    assert all(item["customer_id"] == 1 for item in name_items)
    assert all("三层瓦楞外箱" in item["product_name"] for item in name_items)
    assert listed.status_code == 200
    listed_items = listed.json()["items"]
    assert listed_items
    assert all(item["customer_id"] == 1 for item in listed_items)
    assert all(item["remaining_quantity"] > 0 for item in listed_items)
    assert {item["order_item_id"] for item in listed_items} == {1, 2, 3}
    assert limited.status_code == 200
    assert len(limited.json()["items"]) == 2


def test_delivery_frontend_uses_explicit_all_pending_selection_without_blank_rows() -> None:
    index = (
        Path(__file__).resolve().parents[1] / "static" / "index.html"
    ).read_text(encoding="utf-8")

    assert "resetDeliveryLines(count = 0)" in index
    assert "新增5行" not in index
    assert "选择待送订单" in index
    assert "/api/deliveries/pending-items/search" in index
    assert "未找到该客户下可送货的存货编码、客户单号或产品名称" in index
    assert "请选择具体订单明细" in index


def test_delivery_frontend_exposes_guarded_status_actions() -> None:
    index = (
        Path(__file__).resolve().parents[1] / "static" / "index.html"
    ).read_text(encoding="utf-8")

    assert '@click="editDelivery(row)"' in index
    assert '@click="deleteDelivery(row)"' in index
    assert '@click="cancelDelivery(row)"' in index
    assert "canDelivery && row.status==='pending'" in index
    assert "canDelivery && row.status==='dispatched'" in index
    assert "未发生发货的草稿将直接删除" in index
    assert "库存流水和审计记录均已保留" in index
    assert "仅删除送货单本身" not in index


def test_cancel_is_atomic_on_rollback_failure(delivery_api_app) -> None:
    """若某条明细回滚守卫失败，整笔取消必须回滚，不得部分扣回。"""
    from app.models.delivery import Delivery
    from app.models.order import OrderItem

    app, session_factory = delivery_api_app
    with TestClient(app) as client:
        _login(client, "sales")
        delivery_id = _create_and_dispatch(client)
        # 篡改 item2 已送数量到低于本单送货量，制造回滚守卫失败
        with session_factory() as session:
            tampered = session.get(OrderItem, 2)
            tampered.delivered_quantity = 10
            session.commit()
        cancelled = client.put(f"/api/deliveries/{delivery_id}/cancel")

    assert cancelled.status_code == 409, cancelled.text
    with session_factory() as session:
        # item1 不得被部分扣回；状态仍为已发货
        assert session.get(OrderItem, 1).delivered_quantity == 50
        assert session.get(OrderItem, 2).delivered_quantity == 10
        assert session.get(Delivery, delivery_id).status == "dispatched"


def test_cancel_requires_operate_role(delivery_api_app) -> None:
    app, session_factory = delivery_api_app
    with TestClient(app) as client:
        _login(client, "sales")
        delivery_id = _create_and_dispatch(client)
    for role in ("finance", "workshop"):
        with TestClient(app) as client:
            _login(client, role)
            resp = client.put(f"/api/deliveries/{delivery_id}/cancel")
            assert resp.status_code == 403, f"{role}: {resp.text}"


def test_cancel_writes_audit_log(delivery_api_app) -> None:
    from app.models.audit import OperationLog

    app, session_factory = delivery_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        delivery_id = _create_and_dispatch(client)
        cancelled = client.put(f"/api/deliveries/{delivery_id}/cancel")

    assert cancelled.status_code == 200, cancelled.text
    with session_factory() as session:
        action = session.scalar(
            select(OperationLog.action).where(
                OperationLog.action == "CANCEL_DISPATCH",
                OperationLog.entity_id == delivery_id,
            )
        )
    assert action == "CANCEL_DISPATCH"


def test_edit_and_delete_audit_logged(delivery_api_app) -> None:
    from app.models.audit import OperationLog

    app, session_factory = delivery_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        delivery_id = _create_pending(client)
        client.put(
            f"/api/deliveries/{delivery_id}",
            json={"items": [{"order_item_id": 1, "delivered_quantity": 5}]},
        )
        client.delete(f"/api/deliveries/{delivery_id}")

    with session_factory() as session:
        actions = set(
            session.scalars(
                select(OperationLog.action).where(
                    OperationLog.entity_id == delivery_id,
                    OperationLog.resource == "Delivery",
                )
            ).all()
        )
    assert {"UPDATE", "DELETE"} <= actions
