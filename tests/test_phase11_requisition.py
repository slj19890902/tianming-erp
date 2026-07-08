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
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def requisition_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.incoming import router as incoming_router
    from app.api.requisition import router as requisition_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "requisition.sqlite3")
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
        session.add_all([*users, customer])
        session.flush()
        product = Product(
            customer_id=customer.id,
            product_code="21301028",
            customer_material_code="SME-028",
            product_name="中性外箱",
            legacy_material_text="K=A-BC",
            length_mm=Decimal("520"),
            width_mm=Decimal("350"),
            height_mm=Decimal("300"),
            box_category="normal",
        )
        session.add(product)
        session.flush()
        order = Order(
            order_number="PO-20260614-001",
            customer_id=customer.id,
            order_date=date(2026, 6, 14),
            delivery_date=date(2026, 6, 21),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("360"),
        )
        session.add(order)
        session.flush()
        session.add(
            OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=100,
                unit_price=Decimal("3.60"),
                subtotal=Decimal("360"),
                material_status="pending",
                snapshot_product_name="中性外箱",
                snapshot_spec="520×350×300mm",
                snapshot_material="K=A-BC",
            )
        )
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(requisition_router, prefix="/api/requisition")
    app.include_router(incoming_router, prefix="/api/incoming")

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


def _batch_payload() -> dict:
    return {
        "supplier_name": "苏州纸板供应商",
        "items": [
            {
                "order_item_id": 1,
                "inventory_deducted_qty": 0,
                "requisition_qty": 27,
                "cardboard_len": "1756",
                "cardboard_width": "1962",
                "special_process": "一开三",
                "remark": "按一开三采购",
            }
        ],
    }


def test_pending_supplier_counts_and_material_change(requisition_app) -> None:
    from app.models.material import Material
    from app.models.order import Order, OrderItem
    from app.models.product import Product

    app, session_factory = requisition_app
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        product = session.get(Product, item.product_id)
        supplier_a = Material(
            code="A6A",
            layer_count=3,
            flute_type="E",
            supplier_name="嘉林亿",
        )
        supplier_b = Material(
            code="CCC-B",
            layer_count=3,
            flute_type="E",
            supplier_name="鸣朋",
        )
        supplier_a_alt = Material(
            code="BC14C",
            layer_count=5,
            flute_type="AB",
            supplier_name="嘉林亿",
        )
        session.add_all([supplier_a, supplier_b, supplier_a_alt])
        session.flush()
        item.material_id = supplier_a.id
        item.snapshot_material = supplier_a.code
        item.snapshot_supplier_name = supplier_a.supplier_name
        item.layer_count = 3
        item.flute_type = "E"
        order = session.get(Order, item.order_id)
        for index in range(9):
            session.add(
                OrderItem(
                    order_id=order.id,
                    product_id=product.id,
                    quantity=10,
                    unit_price=Decimal("1"),
                    subtotal=Decimal("10"),
                    material_status="pending",
                    requisition_status="未报料",
                    snapshot_product_name=f"嘉林亿产品{index}",
                    snapshot_material=supplier_a.code,
                    snapshot_supplier_name=supplier_a.supplier_name,
                    material_id=supplier_a.id,
                    layer_count=3,
                    flute_type="E",
                )
            )
        supplier_b_item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=10,
            unit_price=Decimal("1"),
            subtotal=Decimal("10"),
            material_status="pending",
            requisition_status="未报料",
            snapshot_product_name="鸣朋产品",
            snapshot_material=supplier_b.code,
            snapshot_supplier_name=supplier_b.supplier_name,
            material_id=supplier_b.id,
            layer_count=3,
            flute_type="E",
        )
        session.add(supplier_b_item)
        session.commit()
        supplier_a_id = supplier_a.id
        supplier_a_alt_id = supplier_a_alt.id
        supplier_b_item_id = supplier_b_item.id

    with TestClient(app) as client:
        _login(client, "sales")
        pending = client.get("/api/requisition/pending")
        assert pending.status_code == 200
        counts = {
            row["supplier_name"]: row["count"]
            for row in pending.json()["supplier_counts"]
        }
        assert counts == {"嘉林亿": 10, "鸣朋": 1}
        changed = client.put(
            f"/api/requisition/pending/{supplier_b_item_id}/material",
            json={
                "material_id": supplier_a_id,
                "layer_count": 3,
                "flute_type": "E",
                "sync_product": True,
            },
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["supplier_name"] == "嘉林亿"
        assert changed.json()["material_code"] == "A6A"
        pending_after_supplier_change = client.get("/api/requisition/pending")
        assert pending_after_supplier_change.status_code == 200
        counts_after_supplier_change = {
            row["supplier_name"]: row["count"]
            for row in pending_after_supplier_change.json()["supplier_counts"]
        }
        assert counts_after_supplier_change == {"嘉林亿": 11}
        changed_row = next(
            row
            for row in pending_after_supplier_change.json()["items"]
            if row["item_id"] == supplier_b_item_id
        )
        assert changed_row["requisition_status"] == "未报料"
        assert changed_row["material_display"] == "A6A / E"

        same_supplier_change = client.put(
            f"/api/requisition/pending/{supplier_b_item_id}/material",
            json={
                "material_id": supplier_a_alt_id,
                "layer_count": 5,
                "flute_type": "AB",
                "sync_product": True,
            },
        )
        assert same_supplier_change.status_code == 200, same_supplier_change.text
        assert same_supplier_change.json()["supplier_name"] == "嘉林亿"
        assert same_supplier_change.json()["material_code"] == "BC14C"
        pending_after_material_change = client.get("/api/requisition/pending")
        assert pending_after_material_change.status_code == 200
        same_supplier_row = next(
            row
            for row in pending_after_material_change.json()["items"]
            if row["item_id"] == supplier_b_item_id
        )
        assert same_supplier_row["snapshot_supplier_name"] == "嘉林亿"
        assert same_supplier_row["material_display"] == "BC14C / AB"
        assert {
            row["supplier_name"]: row["count"]
            for row in pending_after_material_change.json()["supplier_counts"]
        } == {"嘉林亿": 11}

    with session_factory() as session:
        changed_item = session.get(OrderItem, supplier_b_item_id)
        changed_product = session.get(Product, changed_item.product_id)
        assert changed_item.snapshot_supplier_name == "嘉林亿"
        assert changed_item.snapshot_material == "BC14C"
        assert changed_item.flute_type == "AB"
        assert changed_item.requisition_status == "未报料"
        assert changed_item.material_status == "pending"
        assert changed_item.material_id == supplier_a_alt_id
        assert changed_product.material_id == supplier_a_alt_id


def test_pending_requisition_sorts_newest_record_first(requisition_app) -> None:
    from app.models.order import OrderItem

    app, session_factory = requisition_app
    with session_factory() as session:
        first = session.get(OrderItem, 1)
        first.created_at = datetime(2026, 6, 1, 8, 0, 0)
        newest = OrderItem(
            order_id=first.order_id,
            product_id=first.product_id,
            quantity=10,
            unit_price=Decimal("1"),
            subtotal=Decimal("10"),
            material_status="pending",
            requisition_status="未报料",
            snapshot_product_name="最近新增报料明细",
            snapshot_material=first.snapshot_material,
            created_at=datetime(2026, 6, 30, 8, 0, 0),
        )
        session.add(newest)
        session.commit()
        newest_id = newest.id

    with TestClient(app) as client:
        _login(client, "sales")
        response = client.get("/api/requisition/pending")

    assert response.status_code == 200
    assert response.json()["items"][0]["item_id"] == newest_id


def test_merge_suggestions_never_merge_different_flute_types(requisition_app) -> None:
    from app.models.material import Material
    from app.models.order import OrderItem

    app, session_factory = requisition_app
    with session_factory() as session:
        source = session.get(OrderItem, 1)
        material = Material(
            code="A416D",
            layer_count=5,
            supplier_name="苏州嘉林亿",
            is_active=True,
        )
        session.add(material)
        session.flush()
        common = {
            "order_id": source.order_id,
            "product_id": source.product_id,
            "quantity": 10,
            "unit_price": Decimal("1"),
            "subtotal": Decimal("10"),
            "material_status": "pending",
            "requisition_status": "未报料",
            "snapshot_material": "A416D",
            "snapshot_supplier_name": "苏州嘉林亿",
            "material_id": material.id,
            "layer_count": 5,
            "snapshot_report_length_mm": 800,
            "snapshot_report_width_mm": 300,
            "snapshot_splice_mode": "single",
            "snapshot_pieces_per_box": 1,
        }
        session.add_all(
            [
                OrderItem(
                    **common,
                    snapshot_product_name="AB楞产品1",
                    flute_type="AB",
                ),
                OrderItem(
                    **common,
                    snapshot_product_name="AB楞产品2",
                    flute_type="AB",
                ),
                OrderItem(
                    **common,
                    snapshot_product_name="BE楞产品",
                    flute_type="BE",
                ),
            ]
        )
        session.commit()

    with TestClient(app) as client:
        _login(client, "sales")
        response = client.get("/api/requisition/merge-suggestions")

    assert response.status_code == 200
    matching = [
        row for row in response.json()["suggestions"]
        if row["material_display"].startswith("A416D")
    ]
    assert len(matching) == 1
    assert matching[0]["flute_type"] == "AB"
    assert matching[0]["member_count"] == 2
    assert all(member["flute_type"] == "AB" for member in matching[0]["members"])



def test_mobile_incoming_exposes_latest_pdf_drawing(requisition_app) -> None:
    from app.models.order import OrderItem
    from app.models.product_drawing import ProductDrawing

    app, session_factory = requisition_app
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        item.requisition_status = "已报料"
        item.requisition_qty = 27
        item.requisition_date = date(2026, 6, 22)
        item.cardboard_len = Decimal("1756")
        item.cardboard_width = Decimal("1962")
        session.add(
            ProductDrawing(
                product_id=item.product_id,
                image_path="/static/uploads/drawings/customer-order.pdf",
                thumbnail_path="/static/uploads/drawings/customer-order.pdf",
            )
        )
        session.commit()

    with TestClient(app) as client:
        _login(client, "workshop")
        response = client.get("/api/incoming/pending")

    assert response.status_code == 200
    row = response.json()["items"][0]
    assert row["drawing_is_pdf"] is True
    assert row["drawing_path"].endswith("customer-order.pdf")
    assert row["product_code"] == "21301028"
    assert row["incoming_quantity"] == row["requisition_qty"]
    assert row["requisition_date"] is not None


def test_incoming_quantity_can_be_changed_when_receiving(requisition_app) -> None:
    from app.models.order import OrderItem
    from app.models.requisition import RequisitionItem

    app, session_factory = requisition_app
    with TestClient(app) as client:
        _login(client, "sales")
        batch = client.post("/api/requisition/batches", json=_batch_payload())
        assert batch.status_code == 201, batch.text
        client.post("/api/auth/logout")
        _login(client, "workshop")
        received = client.put(
            "/api/incoming/receive/1",
            json={"received_quantity": 92},
        )

    assert received.status_code == 200, received.text
    assert received.json()["incoming_quantity"] == 92
    with session_factory() as session:
        assert session.get(OrderItem, 1).requisition_qty == 92
        requisition_item = session.query(RequisitionItem).filter_by(
            order_item_id=1,
            status="有效",
        ).one()
        assert requisition_item.requisition_qty == 92


def test_mobile_entry_returns_lan_url_and_qr_code(requisition_app) -> None:
    app, _ = requisition_app
    with TestClient(app) as client:
        _login(client, "workshop")
        response = client.get("/api/incoming/mobile-entry")

    assert response.status_code == 200
    assert response.json()["url"].endswith(":8000/incoming.html")
    assert response.json()["qr_data_url"].startswith("data:image/png;base64,")


def test_pending_defaults_dimensions_and_batch_submission(requisition_app) -> None:
    from app.models.order import OrderItem
    from app.models.requisition import Requisition, RequisitionItem

    app, session_factory = requisition_app
    with TestClient(app) as client:
        _login(client, "sales")
        pending = client.get("/api/requisition/pending")
        created = client.post("/api/requisition/batches", json=_batch_payload())

    assert pending.status_code == 200
    row = pending.json()["items"][0]
    assert row["requisition_status"] == "未报料"
    assert Decimal(str(row["suggested_cardboard_len"])) == Decimal("1756")
    assert Decimal(str(row["suggested_cardboard_width"])) == Decimal("654")
    assert created.status_code == 201, created.text
    assert created.json()["requisition_number"].startswith(
        f"BL-{date.today():%Y%m%d}-"
    )
    assert created.json()["items"][0]["requisition_qty"] == 34
    assert created.json()["items"][0]["cutting_mode"] == "一开三"
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        assert item.inventory_deducted_qty == 0
        assert item.requisition_qty == 34
        assert item.requisition_status == "已报料"
        assert item.special_process == "一开三"
        assert session.scalar(select(Requisition)) is not None
        assert session.scalar(select(RequisitionItem)) is not None


def test_supplier_schedule_drives_incoming_priority_and_can_cancel_before_receive(
    requisition_app,
) -> None:
    app, _ = requisition_app
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/requisition/batches", json=_batch_payload())
        assert created.status_code == 201, created.text
        scheduled = client.put(
            "/api/requisition/items/1/supplier-schedule",
            json={
                "supplier_delivery_time": "2026-06-15T08:30:00",
                "supplier_order_number": "SUP-8899",
            },
        )
        incoming = client.get("/api/incoming/pending")
        cancelled = client.put(
            "/api/requisition/items/1/cancel",
            json={"reason": "供应商规格确认错误"},
        )

    assert scheduled.status_code == 200
    assert scheduled.json()["requisition_status"] == "供应商已排单"
    assert incoming.status_code == 200
    assert incoming.json()["items"][0]["requisition_status"] == "供应商已排单"
    assert incoming.json()["items"][0]["supplier_delivery_time"].startswith(
        "2026-06-15T08:30"
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["requisition_status"] == "未报料"


def test_submitted_requisition_items_can_be_listed(requisition_app) -> None:
    app, _ = requisition_app
    with TestClient(app) as client:
        _login(client, "admin")
        client.post("/api/requisition/batches", json=_batch_payload())
        client.put(
            "/api/requisition/items/1/supplier-schedule",
            json={"supplier_delivery_time": "2026-06-15T08:30:00"},
        )
        response = client.get("/api/requisition/items", params={"include_history": "true"})

    assert response.status_code == 200
    row = response.json()["items"][0]
    assert row["item_id"] == 1
    assert row["order_number"] == "PO-20260614-001"
    assert row["customer_name"] == "苏州思迈尔包装有限公司"
    assert row["requisition_status"] == "供应商已排单"
    assert row["supplier_delivery_time"].startswith("2026-06-15T08:30")


def test_requisition_api_hides_legacy_history_prefix_in_order_number(
    requisition_app,
) -> None:
    from app.models.order import Order

    app, session_factory = requisition_app
    with session_factory() as session:
        session.get(Order, 1).order_number = "RUIDA-42838"
        session.commit()
    with TestClient(app) as client:
        _login(client, "admin")
        client.post("/api/requisition/batches", json=_batch_payload())
        response = client.get(
            "/api/requisition/items",
            params={"include_history": "true"},
        )

    assert response.status_code == 200
    row = response.json()["items"][0]
    assert row["display_order_number"] == "TM20260614-0001"
    assert row["order_number"] == "TM20260614-0001"
    assert "RUIDA" not in str(response.json())


def test_requisition_cannot_change_or_cancel_after_material_received(
    requisition_app,
) -> None:
    app, _ = requisition_app
    with TestClient(app) as client:
        _login(client, "admin")
        client.post("/api/requisition/batches", json=_batch_payload())
        received = client.put("/api/incoming/receive/1")
        cancelled = client.put(
            "/api/requisition/items/1/cancel",
            json={"reason": "错误操作"},
        )
        edited = client.put(
            "/api/requisition/items/1",
            json={
                "inventory_deducted_qty": 0,
                "requisition_qty": 100,
                "cardboard_len": "1756",
                "cardboard_width": "654",
                "special_process": "一开一",
                "remark": None,
            },
        )

    assert received.status_code == 200
    assert cancelled.status_code == 409
    assert edited.status_code == 409


def test_history_search_returns_latest_successful_requisition(requisition_app) -> None:
    app, _ = requisition_app
    with TestClient(app) as client:
        _login(client, "sales")
        client.post("/api/requisition/batches", json=_batch_payload())
        response = client.get(
            "/api/requisition/search_history",
            params={"keyword": "思迈尔 21301028 520"},
        )

    assert response.status_code == 200
    result = response.json()["items"][0]
    assert result["product_code"] == "21301028"
    assert Decimal(str(result["cardboard_len"])) == Decimal("1756")
    assert result["special_process"] == "一开三"
    assert result["material"] == "K=A-BC"


def test_requisition_print_contract_has_no_financial_fields(requisition_app) -> None:
    from app.models.company_config import CompanyConfig

    app, session_factory = requisition_app
    with session_factory() as session:
        session.add(
            CompanyConfig(
                id=1,
                company_name="测试纸品包装厂",
                address="测试路88号",
                phone="0512-12345678",
            )
        )
        session.commit()
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/requisition/batches", json=_batch_payload())
        response = client.get(
            f"/api/requisition/batches/{created.json()['id']}/print"
        )

    assert response.status_code == 200
    assert response.json()["sender"] == {
        "company_name": "测试纸品包装厂",
        "address": "测试路88号",
        "phone": "0512-12345678",
    }
    serialized = str(response.json()).lower()
    for forbidden in ("unit_price", "subtotal", "cost", "amount"):
        assert forbidden not in serialized


def test_double_splice_with_one_to_three_uses_piece_count(requisition_app) -> None:
    from app.models.order import OrderItem

    app, session_factory = requisition_app
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        item.snapshot_splice_mode = "double"
        item.snapshot_pieces_per_box = 2
        item.snapshot_report_length_mm = 800
        item.snapshot_report_width_mm = 200
        session.commit()

    payload = {
        "supplier_name": "苏州纸板供应商",
        "items": [
            {
                "order_item_id": 1,
                "inventory_deducted_qty": 0,
                "requisition_qty": 4,
                "cardboard_len": "800",
                "cardboard_width": "600",
                "special_process": "一开三",
                "remark": "双拼一开三",
            }
        ],
    }

    with TestClient(app) as client:
        _login(client, "sales")
        pending = client.get("/api/requisition/pending")
        created = client.post("/api/requisition/batches", json=payload)

    assert pending.status_code == 200
    row = pending.json()["items"][0]
    assert row["pieces_per_box"] == 2
    assert row["required_piece_qty"] == 200
    assert created.status_code == 201, created.text
    assert created.json()["items"][0]["requisition_qty"] == 67
    assert created.json()["items"][0]["cardboard_len"] == "800"
    assert created.json()["items"][0]["cardboard_width"] == "600"

    with session_factory() as session:
        item = session.get(OrderItem, 1)
        assert item.requisition_qty == 67
        assert item.special_process == "一开三"


def test_tiandigai_requisition_splits_cover_and_base_rows(requisition_app) -> None:
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.requisition import RequisitionItem

    app, session_factory = requisition_app
    with session_factory() as session:
        product = session.get(Product, 1)
        product.box_style = "A3 天地盖"
        item = session.get(OrderItem, 1)
        item.snapshot_report_length_mm = 660
        item.snapshot_report_width_mm = 460
        item.snapshot_crease_type = "压线"
        item.snapshot_crease_left_mm = 80
        item.snapshot_crease_middle_mm = 300
        item.snapshot_crease_right_mm = 80
        item.snapshot_base_report_length_mm = 635
        item.snapshot_base_report_width_mm = 435
        item.snapshot_base_crease_type = "压线"
        item.snapshot_base_crease_left_mm = 80
        item.snapshot_base_crease_middle_mm = 275
        item.snapshot_base_crease_right_mm = 80
        session.commit()

    payload = {
        "supplier_name": "苏州纸板供应商",
        "items": [
            {
                "order_item_id": 1,
                "inventory_deducted_qty": 0,
                "requisition_qty": 100,
                "cardboard_len": "660",
                "cardboard_width": "460",
                "special_process": "一开一",
                "remark": "天地盖按套报料",
            }
        ],
    }

    with TestClient(app) as client:
        _login(client, "sales")
        pending = client.get("/api/requisition/pending")
        created = client.post("/api/requisition/batches", json=payload)
        _login(client, "admin")
        received = client.put("/api/incoming/receive/1")

    assert pending.status_code == 200
    pending_row = pending.json()["items"][0]
    assert pending_row["snapshot_base_report_length_mm"] == 635
    assert pending_row["snapshot_base_report_width_mm"] == 435
    assert created.status_code == 201, created.text
    assert created.json()["items"][0]["requisition_qty"] == 200
    assert received.status_code == 200, received.text

    with session_factory() as session:
        item = session.get(OrderItem, 1)
        assert item.requisition_qty == 200
        assert item.requisition_status == "已入库"
        rows = session.scalars(
            select(RequisitionItem).order_by(RequisitionItem.id)
        ).all()
        assert len(rows) == 2
        assert [row.requisition_qty for row in rows] == [100, 100]
        assert [Decimal(row.cardboard_len) for row in rows] == [
            Decimal("660"),
            Decimal("635"),
        ]
        assert [Decimal(row.cardboard_width) for row in rows] == [
            Decimal("460"),
            Decimal("435"),
        ]
        assert "天地盖-盖" in rows[0].product_name_snapshot
        assert "天地盖-底" in rows[1].product_name_snapshot



def test_phase11_migration_is_additive_and_preserves_order_items(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "phase11.sqlite3"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "phase11-migration-test")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "f4b2c9d7a110")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO users (
                id, username, password_hash, role, real_name, display_name,
                is_active, must_change_password
            ) VALUES (1, 'admin', 'hash', 'admin', 'admin', 'admin', 1, 1)
            """
        )
        connection.execute(
            """
            INSERT INTO customers (
                id, customer_number, customer_code, name, payment_term_days,
                credit_limit, default_tax_rate, status, is_active
            ) VALUES (1, 1, 'T', '测试客户', 30, 0, 0.13, 'active', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO products (
                id, customer_id, product_code, customer_material_code,
                product_name, box_category, unit, is_active
            ) VALUES (1, 1, 'P1', 'M1', '测试箱', 'normal', '只', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO sales_orders (
                id, order_number, customer_id, order_date, status,
                payment_status, total_amount
            ) VALUES (1, 'PO-OLD-001', 1, '2026-06-01',
                      'pending_production', 'unpaid', 10)
            """
        )
        connection.execute(
            """
            INSERT INTO sales_order_items (
                id, order_id, product_id, quantity, delivered_quantity,
                is_force_closed, unit_price, subtotal, material_status,
                snapshot_product_name
            ) VALUES (9, 1, 1, 10, 0, 0, 1, 10, 'pending', '历史纸箱')
            """
        )
        connection.commit()

    command.upgrade(config, "head")
    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "b71c4a9e2d10_phase11_requisition_workflow.py"
    ).read_text(encoding="utf-8")
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT id, quantity, requisition_status FROM sales_order_items WHERE id=9"
        ).fetchone()
        tables = {
            value[0]
            for value in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

    assert row == (9, 10, "未报料")
    assert {"material_requisitions", "material_requisition_items"} <= tables
    assert "drop_table" not in migration.lower()
    assert "drop_column" not in migration.lower()
