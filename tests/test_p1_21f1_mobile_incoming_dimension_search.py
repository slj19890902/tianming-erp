from __future__ import annotations

from collections.abc import Generator
from datetime import date
from decimal import Decimal
from pathlib import Path
import re
import shutil
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def mobile_incoming_search_app(tmp_path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.mobile_erp import router as mobile_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.production import ProductionTask
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "p1-21f1.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        worker = User(
            username="p1-21f1-worker",
            password_hash=hash_password("123456"),
            role="workshop",
            real_name="收料员工",
            must_change_password=False,
            customer_access_mode="selected",
        )
        visible = Customer(name="天华电子", customer_code="TH")
        hidden = Customer(name="其他客户", customer_code="OTHER")
        db.add_all([worker, visible, hidden])
        db.flush()
        db.add(UserCustomerScope(user_id=worker.id, customer_id=visible.id))
        visible_product = Product(
            customer_id=visible.id,
            product_code="SAME-CODE",
            customer_material_code="SAME-CODE",
            product_name="护角外箱",
            box_category="normal",
        )
        hidden_product = Product(
            customer_id=hidden.id,
            product_code="SAME-CODE",
            customer_material_code="HIDDEN-SAME",
            product_name="越权同款",
            box_category="normal",
        )
        db.add_all([visible_product, hidden_product])
        db.flush()

        def add_pending(
            *,
            customer: Customer,
            product: Product,
            order_number: str,
            product_name: str,
            length: int,
            width: int,
            snapshot_product_code: str = "SAME-CODE",
        ) -> tuple[int, int]:
            order = Order(
                order_number=order_number,
                customer_id=customer.id,
                customer_po=f"PO-{order_number}",
                order_date=date(2026, 8, 11),
                delivery_date=date(2026, 8, 15),
                status="pending_production",
                payment_status="unpaid",
                total_amount=Decimal("0"),
            )
            db.add(order)
            db.flush()
            item = OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=1000,
                delivered_quantity=0,
                unit_price=Decimal("0"),
                subtotal=Decimal("0"),
                material_status="pending",
                requisition_status="已报料",
                requisition_qty=1000,
                snapshot_product_code=snapshot_product_code,
                snapshot_product_name=product_name,
                snapshot_spec="870×50×50×5",
                snapshot_material="K=A",
                snapshot_supplier_name="护角供应商",
                flute_type="B",
                cardboard_len=Decimal(length),
                cardboard_width=Decimal(width),
                snapshot_crease_type="净料",
                snapshot_crease_left_mm=50,
                snapshot_crease_middle_mm=50,
                snapshot_crease_right_mm=5,
            )
            db.add(item)
            db.flush()
            task = ProductionTask(
                order_item_id=item.id,
                status="pending",
                planned_quantity=1000,
                ordered_quantity_snapshot=1000,
                material_received_quantity=0,
                material_input_quantity=0,
                output_factor=1,
                version=1,
            )
            db.add(task)
            db.flush()
            return item.id, task.id

        first_item, first_task = add_pending(
            customer=visible,
            product=visible_product,
            order_number="F1-ORDER-LENGTH",
            product_name="护角外箱一",
            length=870,
            width=50,
        )
        second_item, second_task = add_pending(
            customer=visible,
            product=visible_product,
            order_number="F1-ORDER-WIDTH",
            product_name="护角外箱二",
            length=50,
            width=870,
            snapshot_product_code="CUSTOMER-MODEL-14",
        )
        substring_item, substring_task = add_pending(
            customer=visible,
            product=visible_product,
            order_number="F1-ORDER-SUBSTRING",
            product_name="1430纸板候选",
            length=1430,
            width=516,
        )
        partial_width_item, partial_width_task = add_pending(
            customer=visible,
            product=visible_product,
            order_number="F1-ORDER-PARTIAL-WIDTH",
            product_name="宽度片段候选",
            length=600,
            width=614,
        )
        add_pending(
            customer=hidden,
            product=hidden_product,
            order_number="F1-HIDDEN",
            product_name="越权同款",
            length=870,
            width=870,
        )
        db.commit()
        ids = {
            "first_item": first_item,
            "first_task": first_task,
            "second_item": second_item,
            "second_task": second_task,
            "substring_item": substring_item,
            "substring_task": substring_task,
            "partial_width_item": partial_width_item,
            "partial_width_task": partial_width_task,
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(mobile_router, prefix="/api/mobile/erp")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, factory, ids
    finally:
        engine.dispose()


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "p1-21f1-worker", "password": "123456"},
    )
    assert response.status_code == 200, response.text


def test_search_dimensions_text_scope_and_read_only(mobile_incoming_search_app) -> None:
    from app.models.audit import OperationLog
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.production import ProductionTask

    app, factory, _ids = mobile_incoming_search_app
    with TestClient(app) as client:
        assert client.get("/api/mobile/erp/incoming/search", params={"q": "870"}).status_code == 401
        _login(client)
        with factory() as db:
            before = (
                db.scalar(select(func.count(ProductionTask.id))),
                db.scalar(select(func.count(IncomingReceiptItem.id))),
                db.scalar(select(func.count(OperationLog.id))),
            )

        any_side = client.get(
            "/api/mobile/erp/incoming/search", params={"q": "870"}
        )
        assert any_side.status_code == 200, any_side.text
        assert any_side.headers["cache-control"] == "private, no-store"
        payload = any_side.json()
        assert payload["total"] == 2
        assert payload["page_size"] == 20
        assert payload["read_only"] is True
        assert {tuple(row["search_match"]["dimension_sides"]) for row in payload["items"]} == {
            ("length",),
            ("width",),
        }
        assert all("production_detail_url" in row for row in payload["items"])
        assert "F1-HIDDEN" not in any_side.text

        partial_dimension = client.get(
            "/api/mobile/erp/incoming/search", params={"q": "14"}
        ).json()
        assert partial_dimension["total"] == 2
        assert {
            row["order_number"]: row["search_match"]["dimension_sides"]
            for row in partial_dimension["items"]
        } == {
            "F1-ORDER-SUBSTRING": ["length"],
            "F1-ORDER-PARTIAL-WIDTH": ["width"],
        }
        assert all(
            row["search_match"]["text_fields"] == []
            for row in partial_dimension["items"]
        )

        partial_length = client.get(
            "/api/mobile/erp/incoming/search",
            params={"q": "14", "dimension_mode": "length"},
        ).json()
        assert [row["order_number"] for row in partial_length["items"]] == [
            "F1-ORDER-SUBSTRING"
        ]
        partial_width = client.get(
            "/api/mobile/erp/incoming/search",
            params={"q": "14", "dimension_mode": "width"},
        ).json()
        assert [row["order_number"] for row in partial_width["items"]] == [
            "F1-ORDER-PARTIAL-WIDTH"
        ]
        precise_dimension = client.get(
            "/api/mobile/erp/incoming/search", params={"q": "1430"}
        ).json()
        assert precise_dimension["total"] == 1
        normalized_dimension = client.get(
            "/api/mobile/erp/incoming/search", params={"q": "014.0"}
        ).json()
        assert {
            row["order_number"] for row in normalized_dimension["items"]
        } == {"F1-ORDER-SUBSTRING", "F1-ORDER-PARTIAL-WIDTH"}
        full_dimension = client.get(
            "/api/mobile/erp/incoming/search", params={"q": "1430*516"}
        ).json()
        assert [row["order_number"] for row in full_dimension["items"]] == [
            "F1-ORDER-SUBSTRING"
        ]
        assert full_dimension["items"][0]["search_match"]["dimension_sides"] == [
            "length",
            "width",
        ]

        by_length = client.get(
            "/api/mobile/erp/incoming/search",
            params={"q": "870", "dimension_mode": "length"},
        ).json()
        assert [row["order_number"] for row in by_length["items"]] == [
            "F1-ORDER-LENGTH"
        ]
        by_width = client.get(
            "/api/mobile/erp/incoming/search",
            params={"q": "870", "dimension_mode": "width"},
        ).json()
        assert [row["order_number"] for row in by_width["items"]] == [
            "F1-ORDER-WIDTH"
        ]
        by_customer = client.get(
            "/api/mobile/erp/incoming/search", params={"q": "天华"}
        ).json()
        assert by_customer["total"] == 4
        assert all(
            row["search_match"]["text_fields"] == ["customer_name"]
            for row in by_customer["items"]
        )
        by_customer_code = client.get(
            "/api/mobile/erp/incoming/search", params={"q": "TH"}
        ).json()
        assert by_customer_code["total"] == 4
        assert all(
            row["search_match"]["text_fields"] == ["customer_code"]
            for row in by_customer_code["items"]
        )
        by_name = client.get(
            "/api/mobile/erp/incoming/search", params={"q": "护角外箱二"}
        ).json()
        assert by_name["total"] == 0
        by_customer_model = client.get(
            "/api/mobile/erp/incoming/search", params={"q": "CUSTOMER-MODEL-14"}
        ).json()
        assert by_customer_model["total"] == 0
        invalid = client.get(
            "/api/mobile/erp/incoming/search",
            params={"q": "护角", "dimension_mode": "length"},
        )
        assert invalid.status_code == 422
        assert client.get(
            "/api/mobile/erp/incoming/search",
            params={"q": "870", "page_size": 21},
        ).status_code == 422

        with factory() as db:
            after = (
                db.scalar(select(func.count(ProductionTask.id))),
                db.scalar(select(func.count(IncomingReceiptItem.id))),
                db.scalar(select(func.count(OperationLog.id))),
            )
        assert after == before


def test_exact_route_detail_never_crosses_same_product_code(
    mobile_incoming_search_app,
) -> None:
    app, _factory, ids = mobile_incoming_search_app
    with TestClient(app) as client:
        _login(client)
        first = client.get(
            f"/api/mobile/erp/incoming/{ids['first_item']}/production-detail"
        )
        assert first.status_code == 200, first.text
        payload = first.json()
        assert payload["route_id"] == str(ids["first_item"])
        assert payload["order_item_id"] == ids["first_item"]
        assert [row["task_id"] for row in payload["production_tasks"]] == [
            ids["first_task"]
        ]
        assert payload["incoming_item"]["order_number"] == "F1-ORDER-LENGTH"
        assert str(ids["second_task"]) not in {
            str(row["task_id"]) for row in payload["production_tasks"]
        }

        second = client.get(
            f"/api/mobile/erp/incoming/{ids['second_item']}/production-detail"
        ).json()
        assert [row["task_id"] for row in second["production_tasks"]] == [
            ids["second_task"]
        ]
        assert client.get("/api/mobile/erp/incoming/not-a-route/production-detail").status_code == 422


def test_mobile_incoming_search_ui_is_paged_latest_wins_and_read_only_detail(
    tmp_path: Path,
) -> None:
    html = (ROOT / "static/incoming.html").read_text(encoding="utf-8")
    for marker in (
        "查待收料：客户名称或报料长宽尺寸",
        '<option value="any">任意报料边</option>',
        'id="pendingSearchInput"',
        'id="pendingDimensionMode"',
        'value="any"',
        'value="length"',
        'value="width"',
        'pendingPageSize: 20',
        'endpoint = "/api/mobile/erp/incoming/search"',
        'params.set("dimension_mode", requestedDimensionMode)',
        'search_match?.summary',
        'data-production-detail=',
        'beginLatestRequest("incoming:production-detail")',
        'String(state.productionDetailRouteId || "") !== routeId',
        "按当前待收料明细精确关联，不按款号跨订单猜测",
    ):
        assert marker in html
    assert "客户、款号、名称、订单号或单边尺寸" not in html
    assert "任意边/普通搜索" not in html
    load_pending = html.split("async function loadPending", 1)[1].split(
        "async function changePendingPage", 1
    )[0]
    assert 'beginLatestRequest("incoming:pending")' in load_pending
    assert 'isLatestRequest("incoming:pending", controller)' in load_pending
    assert "state.pendingAppliedQuery = requestedQuery" in load_pending
    assert load_pending.index('isLatestRequest("incoming:pending", controller)') < load_pending.index(
        "state.pendingAppliedQuery = requestedQuery"
    )
    assert 'method: "POST"' not in html.split("async function openProductionDetail", 1)[1].split(
        "async function receive", 1
    )[0]

    node = shutil.which("node")
    assert node is not None
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, re.DOTALL)
    target = tmp_path / "p1-21f1-incoming.js"
    target.write_text("\n".join(scripts), encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
