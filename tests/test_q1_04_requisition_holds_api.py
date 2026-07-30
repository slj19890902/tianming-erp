from __future__ import annotations

import json
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def hold_api(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.dashboard import router as dashboard_router
    from app.api.deps import get_db
    from app.api.orders import router as orders_router
    from app.api.requisition import router as requisition_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.supplier import Supplier
    from app.models.user import User
    from app.services.supplier_master import normalize_supplier_identity

    engine = create_sqlite_engine(tmp_path / "q1-04-api.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        user = User(
            username="admin",
            password_hash=hash_password("123456"),
            role="admin",
            real_name="Q1 管理员",
            display_name="Q1 管理员",
            must_change_password=False,
        )
        viewer = User(
            username="q1viewer",
            password_hash=hash_password("123456"),
            role="sales",
            real_name="Q1 只读账号",
            display_name="Q1 只读账号",
            must_change_password=False,
        )
        scoped_operator = User(
            username="q1scoped",
            password_hash=hash_password("123456"),
            role="sales",
            real_name="Q1 范围操作员",
            display_name="Q1 范围操作员",
            must_change_password=False,
            customer_access_mode="selected",
        )
        first_customer = Customer(
            customer_number=9801,
            customer_code="Q1C1",
            name="Q1 匿名客户甲",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        second_customer = Customer(
            customer_number=9802,
            customer_code="Q1C2",
            name="Q1 匿名客户乙",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        supplier = Supplier(
            business_code="Q1-SUPPLIER",
            standard_name="Q1 匿名供应商",
            normalized_name=normalize_supplier_identity("Q1 匿名供应商"),
            normalized_business_code=normalize_supplier_identity("Q1-SUPPLIER"),
            display_name="匿名供应商",
            is_active=True,
        )
        session.add_all(
            [
                user,
                viewer,
                scoped_operator,
                first_customer,
                second_customer,
                supplier,
            ]
        )
        session.flush()
        session.add_all(
            [
                UserPermissionOverride(
                    user_id=viewer.id,
                    permission_code="requisition.view",
                    is_allowed=True,
                    granted_by=user.id,
                ),
                UserPermissionOverride(
                    user_id=scoped_operator.id,
                    permission_code="requisition.view",
                    is_allowed=True,
                    granted_by=user.id,
                ),
                UserPermissionOverride(
                    user_id=scoped_operator.id,
                    permission_code="requisition.execute",
                    is_allowed=True,
                    granted_by=user.id,
                ),
                UserCustomerScope(
                    user_id=scoped_operator.id,
                    customer_id=first_customer.id,
                    assigned_by=user.id,
                ),
            ]
        )

        first_product = Product(
            customer_id=first_customer.id,
            product_code="Q1-WAIT-001",
            customer_material_code="Q1-WAIT-001",
            product_name="Q1 匿名待报料箱",
            box_category="normal",
        )
        second_product = Product(
            customer_id=second_customer.id,
            product_code="Q1-WAIT-001",
            customer_material_code="Q1-WAIT-001",
            product_name="Q1 跨客户同编码箱",
            box_category="normal",
        )
        session.add_all([first_product, second_product])
        session.flush()

        def add_item(
            *,
            customer: Customer,
            product: Product,
            suffix: int,
            sequence: int,
        ) -> OrderItem:
            order = Order(
                order_number=f"Q1-ORDER-{suffix:03d}",
                customer_id=customer.id,
                order_date=date(2026, 7, 20 + min(suffix, 8)),
                delivery_date=date(2026, 8, 10),
                status="pending_production",
                payment_status="unpaid",
                total_amount=Decimal("100"),
            )
            session.add(order)
            session.flush()
            item = OrderItem(
                order_id=order.id,
                product_id=product.id,
                item_sequence=sequence,
                quantity=100,
                delivered_quantity=0,
                unit_price=Decimal("1"),
                subtotal=Decimal("100"),
                material_status="pending",
                requisition_status="未报料",
                snapshot_product_code=product.product_code,
                snapshot_product_name=product.product_name,
                snapshot_spec="匿名规格",
                snapshot_material="匿名材质",
                special_process="一开一",
            )
            session.add(item)
            session.flush()
            return item

        previous = add_item(
            customer=first_customer,
            product=first_product,
            suffix=1,
            sequence=1,
        )
        current = add_item(
            customer=first_customer,
            product=first_product,
            suffix=2,
            sequence=1,
        )
        other_current = add_item(
            customer=first_customer,
            product=first_product,
            suffix=3,
            sequence=1,
        )
        cross_customer = add_item(
            customer=second_customer,
            product=second_product,
            suffix=4,
            sequence=1,
        )
        session.commit()
        ids = {
            "previous": previous.id,
            "current": current.id,
            "other_current": other_current.id,
            "cross_customer": cross_customer.id,
            "first_customer": first_customer.id,
            "second_customer": second_customer.id,
            "viewer": viewer.id,
            "scoped_operator": scoped_operator.id,
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(dashboard_router, prefix="/api/dashboard")
    app.include_router(orders_router, prefix="/api/orders")
    app.include_router(requisition_router, prefix="/api/requisition")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    return app, factory, ids


def _login(client: TestClient) -> None:
    _login_as(client, "admin")


def _login_as(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "123456"},
    )
    assert response.status_code == 200


def _previous_hold_payload(current_id: int, previous_id: int) -> dict:
    return {
        "items": [
            {
                "order_item_id": current_id,
                "release_mode": "previous_batch_completed",
                "previous_order_item_id": previous_id,
            }
        ]
    }


def test_hold_moves_item_between_queues_and_execute_post_auto_releases(
    hold_api,
) -> None:
    from app.models.audit import OperationLog
    from app.models.order import OrderItem
    from app.models.requisition import Requisition, RequisitionHold
    from app.models.supplier_requisition_order import SupplierRequisitionOrder
    from app.models.warehouse_inventory import InventoryLot

    app, factory, ids = hold_api
    with TestClient(app) as client:
        _login(client)
        candidates = client.get(
            f"/api/requisition/pending/{ids['current']}/previous-batch-candidates"
        )
        assert candidates.status_code == 200
        candidate_ids = {
            row["order_item_id"] for row in candidates.json()["items"]
        }
        assert ids["previous"] in candidate_ids
        assert ids["cross_customer"] not in candidate_ids

        created = client.post(
            "/api/requisition/holds",
            json=_previous_hold_payload(ids["current"], ids["previous"]),
        )
        assert created.status_code == 201
        assert created.json()["success_count"] == 1

        pending_ids = {
            row["item_id"]
            for row in client.get("/api/requisition/pending").json()["items"]
            if not row.get("is_merge_group")
        }
        assert ids["current"] not in pending_ids
        assert ids["other_current"] in pending_ids

        preview = client.post(
            "/api/requisition/supplier-orders/preview-from-pending-selection",
            json={
                "selections": [
                    {
                        "type": "order_item",
                        "order_item_id": ids["current"],
                    }
                ]
            },
        )
        assert preview.status_code == 409
        assert "等候报料" in preview.json()["detail"]

        with factory() as session:
            previous = session.get(OrderItem, ids["previous"])
            assert previous is not None
            previous.delivered_quantity = previous.quantity
            session.commit()

        read_only_refresh = client.get("/api/requisition/holds")
        assert read_only_refresh.status_code == 200
        assert read_only_refresh.json()["total"] == 1
        assert read_only_refresh.json()["items"][0]["condition_status"] == "due"
        assert read_only_refresh.json()["auto_released_hold_ids"] == []
        dashboard = client.get("/api/dashboard/overview")
        assert dashboard.status_code == 200, dashboard.text
        pending_card = next(
            card
            for card in dashboard.json()["cards"]
            if card["key"] == "pending_material"
        )
        assert pending_card["waiting_count"] == 1
        assert pending_card["waiting_due_count"] == 1

        auto_released = client.post("/api/requisition/holds/auto-release")
        assert auto_released.status_code == 200
        assert auto_released.json()["released_count"] == 1
        assert len(auto_released.json()["released_hold_ids"]) == 1

        holds = client.get("/api/requisition/holds")
        assert holds.status_code == 200
        assert holds.json()["total"] == 0
        assert holds.json()["auto_released_hold_ids"] == []

        pending_ids = {
            row["item_id"]
            for row in client.get("/api/requisition/pending").json()["items"]
            if not row.get("is_merge_group")
        }
        assert ids["current"] in pending_ids

    with factory() as session:
        hold = session.scalar(select(RequisitionHold))
        assert hold is not None
        assert hold.status == "released"
        assert hold.release_source == "automatic_previous_batch_dispatched"
        assert session.scalar(select(func.count(Requisition.id))) == 0
        assert session.scalar(select(func.count(SupplierRequisitionOrder.id))) == 0
        assert session.scalar(select(func.count(InventoryLot.id))) == 0
        actions = set(session.scalars(select(OperationLog.action)).all())
        assert {
            "CREATE_REQUISITION_HOLD",
            "RELEASE_REQUISITION_HOLD",
        } <= actions


def test_date_hold_manual_release_version_and_batch_partial_failure(hold_api) -> None:
    from app.core.time_contract import beijing_today
    from app.models.audit import OperationLog
    from app.models.requisition import RequisitionHold

    app, factory, ids = hold_api
    future_date = beijing_today() + timedelta(days=3)
    with TestClient(app) as client:
        _login(client)
        response = client.post(
            "/api/requisition/holds",
            json={
                "items": [
                    {
                        "order_item_id": ids["current"],
                        "release_mode": "expected_date",
                        "expected_requisition_date": future_date.isoformat(),
                    },
                    {
                        "order_item_id": ids["cross_customer"],
                        "release_mode": "previous_batch_completed",
                        "previous_order_item_id": ids["previous"],
                    },
                ]
            },
        )
        assert response.status_code == 201
        assert response.json()["success_count"] == 1
        assert response.json()["failed_count"] == 1
        assert "同客户" in response.json()["items"][1]["message"]

        hold = client.get("/api/requisition/holds").json()["items"][0]
        updated = client.put(
            f"/api/requisition/holds/{hold['id']}",
            json={
                "expected_version": hold["version"],
                "release_mode": "previous_batch_completed",
                "previous_order_item_id": ids["previous"],
            },
        )
        assert updated.status_code == 200
        hold = updated.json()
        assert hold["release_mode"] == "previous_batch_completed"
        stale = client.post(
            f"/api/requisition/holds/{hold['id']}/release",
            json={"expected_version": hold["version"] + 1},
        )
        assert stale.status_code == 409
        released = client.post(
            f"/api/requisition/holds/{hold['id']}/release",
            json={"expected_version": hold["version"]},
        )
        assert released.status_code == 200
        assert released.json()["status"] == "released"
        assert released.json()["release_mode"] == "previous_batch_completed"

        second = client.post(
            "/api/requisition/holds",
            json={
                "items": [
                    {
                        "order_item_id": ids["other_current"],
                        "release_mode": "expected_date",
                        "expected_requisition_date": future_date.isoformat(),
                    }
                ]
            },
        )
        assert second.json()["success_count"] == 1
        second_hold_id = second.json()["items"][0]["hold"]["id"]
        with factory() as session:
            second_hold = session.get(RequisitionHold, second_hold_id)
            assert second_hold is not None
            second_hold.expected_requisition_date = beijing_today() - timedelta(days=1)
            session.commit()
        first_refresh = client.get("/api/requisition/holds").json()
        assert first_refresh["total"] == 1
        assert first_refresh["items"][0]["id"] == second_hold_id
        assert first_refresh["items"][0]["condition_status"] == "due"
        assert first_refresh["auto_released_hold_ids"] == []

        first_release = client.post("/api/requisition/holds/auto-release")
        assert first_release.status_code == 200
        assert first_release.json()["released_hold_ids"] == [second_hold_id]
        second_release = client.post("/api/requisition/holds/auto-release")
        assert second_release.status_code == 200
        assert second_release.json()["released_hold_ids"] == []

    with factory() as session:
        update_log = session.scalar(
            select(OperationLog)
            .where(
                OperationLog.action == "UPDATE_REQUISITION_HOLD",
                OperationLog.entity_id == ids["current"],
            )
            .order_by(OperationLog.id.desc())
        )
        assert update_log is not None
        update_details = json.loads(update_log.details)
        assert update_log.action_code == "requisition.hold.update"
        assert update_log.result == "success"
        assert update_log.source == "web"
        assert update_details["before"]["release_mode"] == "expected_date"
        assert update_details["before"]["expected_requisition_date"] == str(future_date)
        assert update_details["after"]["release_mode"] == "previous_batch_completed"
        assert update_details["after"]["previous_order_item_id"] == ids["previous"]
        assert (
            update_details["after"]["version"]
            == update_details["before"]["version"] + 1
        )
        release_logs = session.scalars(
            select(OperationLog).where(
                OperationLog.action == "RELEASE_REQUISITION_HOLD",
                OperationLog.entity_id == ids["other_current"],
            )
        ).all()
        assert len(release_logs) == 1


def test_active_hold_blocks_legacy_batch_and_supplier_order_entries(
    hold_api,
) -> None:
    from app.models.order import OrderItem
    from app.models.requisition import Requisition, RequisitionHold
    from app.models.supplier_requisition_order import SupplierRequisitionOrder

    app, factory, ids = hold_api
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/requisition/holds",
            json=_previous_hold_payload(ids["current"], ids["previous"]),
        )
        assert created.status_code == 201
        assert created.json()["success_count"] == 1

        legacy_batch = client.post(
            "/api/requisition/batches",
            json={
                "supplier_name": "Q1 匿名供应商",
                "items": [
                    {
                        "order_item_id": ids["current"],
                        "requisition_qty": 100,
                        "cardboard_len": "500",
                        "cardboard_width": "600",
                        "special_process": "一开一",
                    }
                ],
            },
        )
        assert legacy_batch.status_code == 409
        assert "等候报料" in legacy_batch.json()["detail"]

        legacy_supplier_order = client.post(
            "/api/requisition/supplier-orders",
            json={
                "supplier_name": "Q1 匿名供应商",
                "report_length_mm": 500,
                "report_width_mm": 600,
                "cutting_mode": "一开一",
                "members": [
                    {
                        "item_id": ids["current"],
                        "quantity": 100,
                        "cutting_mode": "一开一",
                    }
                ],
            },
        )
        assert legacy_supplier_order.status_code == 409
        assert "等候报料" in legacy_supplier_order.json()["detail"]

        merge_group = client.post(
            "/api/requisition/merge-groups",
            json={
                "member_item_ids": [ids["current"], ids["other_current"]],
                "supplier_name": "Q1 匿名供应商",
                "report_length_mm": 500,
                "report_width_mm": 600,
                "cutting_mode": "一开一",
                "remark": "等候门禁验证",
            },
        )
        assert merge_group.status_code == 409
        assert "等候报料" in merge_group.json()["detail"]

    with factory() as session:
        hold = session.scalar(select(RequisitionHold))
        item = session.get(OrderItem, ids["current"])
        assert hold is not None
        assert hold.status == "active"
        assert item is not None
        assert item.requisition_status == "未报料"
        assert session.scalar(select(func.count(Requisition.id))) == 0
        assert session.scalar(select(func.count(SupplierRequisitionOrder.id))) == 0


def test_concurrent_same_version_manual_release_has_one_winner_and_one_log(
    hold_api,
) -> None:
    from app.models.audit import OperationLog
    from app.models.requisition import RequisitionHold

    app, factory, ids = hold_api
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/requisition/holds",
            json=_previous_hold_payload(ids["current"], ids["previous"]),
        )
        assert created.status_code == 201
        hold = created.json()["items"][0]["hold"]

    barrier = Barrier(2)

    def release_once() -> tuple[int, dict]:
        with TestClient(app) as concurrent_client:
            _login(concurrent_client)
            barrier.wait(timeout=10)
            response = concurrent_client.post(
                f"/api/requisition/holds/{hold['id']}/release",
                json={"expected_version": hold["version"]},
            )
            return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: release_once(), range(2)))

    assert sorted(status_code for status_code, _body in results) == [200, 409]
    assert sum(body.get("status") == "released" for _status, body in results) == 1

    with factory() as session:
        refreshed = session.get(RequisitionHold, hold["id"])
        assert refreshed is not None
        assert refreshed.status == "released"
        assert refreshed.version == hold["version"] + 1
        release_logs = session.scalars(
            select(OperationLog).where(
                OperationLog.action == "RELEASE_REQUISITION_HOLD",
                OperationLog.entity_id == ids["current"],
            )
        ).all()
        assert len(release_logs) == 1


def test_read_only_get_does_not_write_and_execute_post_auto_releases(
    hold_api,
) -> None:
    from app.core.time_contract import beijing_today
    from app.models.audit import OperationLog
    from app.models.requisition import RequisitionHold

    app, factory, ids = hold_api
    future_date = beijing_today() + timedelta(days=3)
    with TestClient(app) as admin_client:
        _login(admin_client)
        created = admin_client.post(
            "/api/requisition/holds",
            json={
                "items": [
                    {
                        "order_item_id": ids["current"],
                        "release_mode": "expected_date",
                        "expected_requisition_date": future_date.isoformat(),
                    }
                ]
            },
        )
        assert created.status_code == 201
        hold = created.json()["items"][0]["hold"]

    with factory() as session:
        stored = session.get(RequisitionHold, hold["id"])
        assert stored is not None
        stored.expected_requisition_date = beijing_today() - timedelta(days=1)
        session.commit()

    with TestClient(app) as viewer_client:
        _login_as(viewer_client, "q1viewer")
        listed = viewer_client.get("/api/requisition/holds")
        assert listed.status_code == 200
        assert listed.json()["total"] == 1
        assert listed.json()["items"][0]["condition_status"] == "due"
        assert listed.json()["auto_released_hold_ids"] == []
        forbidden = viewer_client.post("/api/requisition/holds/auto-release")
        assert forbidden.status_code == 403

    with factory() as session:
        unchanged = session.get(RequisitionHold, hold["id"])
        assert unchanged is not None
        assert unchanged.status == "active"
        assert unchanged.version == hold["version"]
        assert unchanged.released_by is None
        assert (
            session.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.action == "RELEASE_REQUISITION_HOLD"
                )
            )
            == 0
        )

    with TestClient(app) as admin_client:
        _login(admin_client)
        released = admin_client.post("/api/requisition/holds/auto-release")
        assert released.status_code == 200
        assert released.json() == {
            "released_hold_ids": [hold["id"]],
            "released_count": 1,
        }

    with factory() as session:
        refreshed = session.get(RequisitionHold, hold["id"])
        assert refreshed is not None
        assert refreshed.status == "released"
        assert refreshed.release_source == "automatic_expected_date"
        assert (
            session.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.action == "RELEASE_REQUISITION_HOLD"
                )
            )
            == 1
        )


def test_hold_list_filters_status_dates_and_pagination(hold_api) -> None:
    from app.core.time_contract import beijing_today
    from app.models.order import OrderItem
    from app.models.requisition import RequisitionHold

    app, factory, ids = hold_api
    today = beijing_today()
    future_date = today + timedelta(days=5)
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/requisition/holds",
            json={
                "items": [
                    {
                        "order_item_id": ids["current"],
                        "release_mode": "previous_batch_completed",
                        "previous_order_item_id": ids["previous"],
                    },
                    {
                        "order_item_id": ids["other_current"],
                        "release_mode": "expected_date",
                        "expected_requisition_date": future_date.isoformat(),
                    },
                    {
                        "order_item_id": ids["cross_customer"],
                        "release_mode": "expected_date",
                        "expected_requisition_date": future_date.isoformat(),
                    },
                ]
            },
        )
        assert created.status_code == 201
        assert created.json()["success_count"] == 3

        hold_ids = {
            row["order_item_id"]: row["hold"]["id"]
            for row in created.json()["items"]
        }
        with factory() as session:
            previous = session.get(OrderItem, ids["previous"])
            assert previous is not None
            previous.is_force_closed = True
            current_hold = session.get(RequisitionHold, hold_ids[ids["current"]])
            other_hold = session.get(
                RequisitionHold,
                hold_ids[ids["other_current"]],
            )
            cross_hold = session.get(
                RequisitionHold,
                hold_ids[ids["cross_customer"]],
            )
            assert current_hold is not None
            assert other_hold is not None
            assert cross_hold is not None
            current_hold.product_code_snapshot = "FILTER-ANOMALY"
            current_hold.product_name_snapshot = "异常上一批"
            other_hold.product_code_snapshot = "FILTER-WAITING"
            other_hold.product_name_snapshot = "等待日期"
            cross_hold.product_code_snapshot = "FILTER-DUE-CROSS"
            cross_hold.product_name_snapshot = "跨客户到期"
            cross_hold.expected_requisition_date = today - timedelta(days=1)
            session.commit()

        page = client.get(
            "/api/requisition/holds",
            params={"page": 2, "page_size": 1},
        )
        assert page.status_code == 200
        assert page.json()["total"] == 3
        assert page.json()["page"] == 2
        assert page.json()["page_size"] == 1
        assert page.json()["items"][0]["id"] == hold_ids[ids["other_current"]]

        by_customer = client.get(
            "/api/requisition/holds",
            params={"customer_id": ids["second_customer"]},
        )
        assert by_customer.status_code == 200
        assert [row["id"] for row in by_customer.json()["items"]] == [
            hold_ids[ids["cross_customer"]]
        ]

        by_code = client.get(
            "/api/requisition/holds",
            params={"product_code": "due-cross"},
        )
        assert by_code.status_code == 200
        assert [row["id"] for row in by_code.json()["items"]] == [
            hold_ids[ids["cross_customer"]]
        ]

        by_name = client.get(
            "/api/requisition/holds",
            params={"product_name": "等待"},
        )
        assert by_name.status_code == 200
        assert [row["id"] for row in by_name.json()["items"]] == [
            hold_ids[ids["other_current"]]
        ]

        exact_date = future_date.isoformat()
        by_date = client.get(
            "/api/requisition/holds",
            params={"date_from": exact_date, "date_to": exact_date},
        )
        assert by_date.status_code == 200
        assert [row["id"] for row in by_date.json()["items"]] == [
            hold_ids[ids["other_current"]]
        ]

        expected_by_status = {
            "waiting": hold_ids[ids["other_current"]],
            "due": hold_ids[ids["cross_customer"]],
            "anomaly": hold_ids[ids["current"]],
        }
        for condition_status, expected_hold_id in expected_by_status.items():
            response = client.get(
                "/api/requisition/holds",
                params={"status": condition_status},
            )
            assert response.status_code == 200
            assert [row["id"] for row in response.json()["items"]] == [
                expected_hold_id
            ]

        invalid_status = client.get(
            "/api/requisition/holds",
            params={"status": "unknown"},
        )
        assert invalid_status.status_code == 422


def test_order_item_product_code_change_is_rejected_while_held(hold_api) -> None:
    from app.models.order import OrderItem
    from app.models.requisition import RequisitionHold

    app, factory, ids = hold_api
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/requisition/holds",
            json=_previous_hold_payload(ids["current"], ids["previous"]),
        )
        assert created.status_code == 201
        changed = client.put(
            f"/api/orders/items/{ids['current']}",
            json={
                "quantity": 100,
                "unit_price": "1",
                "product_code": "Q1-WAIT-CHANGED",
                "product_name": "Q1 匿名待报料箱",
                "material": "匿名材质",
                "specification": "匿名规格",
            },
        )
        assert changed.status_code == 409
        assert "等候报料" in changed.json()["detail"]
        assert "修改存货编码" in changed.json()["detail"]

    with factory() as session:
        item = session.get(OrderItem, ids["current"])
        hold = session.scalar(
            select(RequisitionHold).where(
                RequisitionHold.order_item_id == ids["current"]
            )
        )
        assert item is not None
        assert item.snapshot_product_code == "Q1-WAIT-001"
        assert hold is not None
        assert hold.status == "active"


def test_abnormal_previous_batch_stays_waiting_with_clear_warning(hold_api) -> None:
    from app.models.audit import OperationLog
    from app.models.order import OrderItem
    from app.models.requisition import RequisitionHold

    app, factory, ids = hold_api
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/requisition/holds",
            json=_previous_hold_payload(ids["current"], ids["previous"]),
        )
        assert created.json()["success_count"] == 1
        with factory() as session:
            previous = session.get(OrderItem, ids["previous"])
            assert previous is not None
            previous.is_force_closed = True
            session.commit()

        response = client.get("/api/requisition/holds")
        assert response.status_code == 200
        assert response.json()["total"] == 1
        item = response.json()["items"][0]
        assert item["previous_batch"]["state"] == "abnormal"
        assert "短送结档" in item["warning"]
        assert response.json()["auto_released_hold_ids"] == []
        dashboard = client.get("/api/dashboard/overview")
        assert dashboard.status_code == 200
        pending_card = next(
            card
            for card in dashboard.json()["cards"]
            if card["key"] == "pending_material"
        )
        assert pending_card["waiting_count"] == 1
        assert pending_card["waiting_due_count"] == 0

        first_scan = client.post("/api/requisition/holds/auto-release")
        second_scan = client.post("/api/requisition/holds/auto-release")
        assert first_scan.status_code == 200
        assert second_scan.status_code == 200
        assert first_scan.json()["released_count"] == 0
        assert second_scan.json()["released_count"] == 0

    with factory() as session:
        hold = session.scalar(select(RequisitionHold))
        assert hold is not None
        assert hold.status == "active"
        anomaly_logs = session.scalars(
            select(OperationLog).where(
                OperationLog.action_code == "requisition.hold.anomaly",
                OperationLog.entity_id == ids["current"],
            )
        ).all()
        assert len(anomaly_logs) == 1
        log = anomaly_logs[0]
        details = json.loads(log.details)
        assert log.result == "failed"
        assert log.source == "system"
        assert details["before"] == details["after"]
        assert details["source"] == "automatic_previous_batch_anomaly"
        assert "短送结档" in details["warning"]


def test_manual_release_and_update_close_hold_when_eligibility_changed(
    hold_api,
) -> None:
    from app.models.audit import OperationLog
    from app.models.order import OrderItem
    from app.models.requisition import RequisitionHold

    app, factory, ids = hold_api
    future_date = date.today() + timedelta(days=30)
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/requisition/holds",
            json={
                "items": [
                    {
                        "order_item_id": ids["current"],
                        "release_mode": "expected_date",
                        "expected_requisition_date": future_date.isoformat(),
                    },
                    {
                        "order_item_id": ids["other_current"],
                        "release_mode": "expected_date",
                        "expected_requisition_date": future_date.isoformat(),
                    },
                ]
            },
        )
        assert created.status_code == 201
        holds = {
            row["order_item_id"]: row["hold"] for row in created.json()["items"]
        }

        with factory() as session:
            for item_id in (ids["current"], ids["other_current"]):
                item = session.get(OrderItem, item_id)
                assert item is not None
                item.requisition_status = "已报料"
            session.commit()

        release = client.post(
            f"/api/requisition/holds/{holds[ids['current']]['id']}/release",
            json={"expected_version": holds[ids["current"]]["version"]},
        )
        assert release.status_code == 409
        assert "等候记录已关闭" in release.json()["detail"]
        update = client.put(
            f"/api/requisition/holds/{holds[ids['other_current']]['id']}",
            json={
                "expected_version": holds[ids["other_current"]]["version"],
                "release_mode": "expected_date",
                "expected_requisition_date": (
                    future_date + timedelta(days=1)
                ).isoformat(),
            },
        )
        assert update.status_code == 409
        assert "等候记录已关闭" in update.json()["detail"]

    with factory() as session:
        stored = session.scalars(
            select(RequisitionHold).order_by(RequisitionHold.id)
        ).all()
        assert [hold.status for hold in stored] == ["invalidated", "invalidated"]
        assert {
            hold.release_source for hold in stored
        } == {
            "manual_release_eligibility_changed",
            "manual_update_eligibility_changed",
        }
        invalidation_logs = session.scalars(
            select(OperationLog).where(
                OperationLog.action_code == "requisition.hold.invalidate"
            )
        ).all()
        assert len(invalidation_logs) == 2
        for log in invalidation_logs:
            details = json.loads(log.details)
            assert details["before"]["status"] == "active"
            assert details["after"]["status"] == "invalidated"
            assert details["source"] in {
                "manual_release_eligibility_changed",
                "manual_update_eligibility_changed",
            }


def test_direct_hold_id_checks_customer_scope_before_status_response(hold_api) -> None:
    app, _factory, ids = hold_api
    future_date = date.today() + timedelta(days=30)
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/requisition/holds",
            json={
                "items": [
                    {
                        "order_item_id": ids["cross_customer"],
                        "release_mode": "expected_date",
                        "expected_requisition_date": future_date.isoformat(),
                    }
                ]
            },
        )
        assert created.status_code == 201
        hold = created.json()["items"][0]["hold"]
        released = client.post(
            f"/api/requisition/holds/{hold['id']}/release",
            json={"expected_version": hold["version"]},
        )
        assert released.status_code == 200
        released_version = released.json()["version"]

    with TestClient(app) as scoped_client:
        _login_as(scoped_client, "q1scoped")
        update = scoped_client.put(
            f"/api/requisition/holds/{hold['id']}",
            json={
                "expected_version": released_version,
                "release_mode": "expected_date",
                "expected_requisition_date": (
                    future_date + timedelta(days=1)
                ).isoformat(),
            },
        )
        release = scoped_client.post(
            f"/api/requisition/holds/{hold['id']}/release",
            json={"expected_version": released_version},
        )
        assert update.status_code == 403
        assert release.status_code == 403
        assert update.json()["detail"] == "无客户访问权限"
        assert release.json()["detail"] == "无客户访问权限"


def test_deleting_current_item_invalidates_hold_and_keeps_audit_snapshot(
    hold_api,
) -> None:
    from app.models.order import OrderItem
    from app.models.requisition import RequisitionHold

    app, factory, ids = hold_api
    with factory() as session:
        current = session.get(OrderItem, ids["current"])
        assert current is not None
        sibling = OrderItem(
            order_id=current.order_id,
            product_id=current.product_id,
            item_sequence=2,
            quantity=10,
            delivered_quantity=0,
            unit_price=Decimal("1"),
            subtotal=Decimal("10"),
            material_status="pending",
            requisition_status="未报料",
            snapshot_product_code="Q1-WAIT-SIBLING",
            snapshot_product_name="Q1 保留明细",
            snapshot_spec="匿名规格",
            snapshot_material="匿名材质",
            special_process="一开一",
        )
        session.add(sibling)
        session.commit()

    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/requisition/holds",
            json=_previous_hold_payload(ids["current"], ids["previous"]),
        )
        assert created.json()["success_count"] == 1
        deleted = client.delete(f"/api/orders/items/{ids['current']}")
        assert deleted.status_code == 204

    with factory() as session:
        hold = session.scalar(
            select(RequisitionHold).where(
                RequisitionHold.order_item_id_snapshot == ids["current"]
            )
        )
        assert hold is not None
        assert hold.order_item_id is None
        assert hold.status == "invalidated"
        assert hold.release_source == "order_item_deleted"
        assert hold.customer_name_snapshot == "Q1 匿名客户甲"
