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
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def mold_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.orders import router as orders_router
    from app.api.products import router as products_router
    from app.api.warehouse import router as warehouse_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserPermissionOverride
    from app.models.customer import Customer
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "mold-tools.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        users = [
            User(
                username=role,
                password_hash=hash_password("RolePass123!"),
                role=role,
                real_name=role,
                display_name=role,
                must_change_password=False,
            )
            for role in ("admin", "workshop", "sales")
        ]
        db.add_all(users)
        db.flush()
        db.add(
            UserPermissionOverride(
                user_id=users[2].id,
                permission_code="warehouse.view",
                is_allowed=True,
            )
        )
        db.add(
            Customer(
                customer_number=9901,
                customer_code="MOLD-C",
                name="模具联动测试客户",
                payment_term_days=30,
                credit_limit=Decimal("100000"),
            )
        )
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(products_router, prefix="/api/master/products")
    app.include_router(warehouse_router, prefix="/api/warehouse")
    app.include_router(orders_router, prefix="/api/orders")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    return app, factory


def _login(client: TestClient, role: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": role, "password": "RolePass123!"},
    )
    assert response.status_code == 200, response.text


def _protected_business_state(factory) -> dict[str, object]:
    from sqlalchemy import text

    queries = {
        "warehouse_locations": "SELECT COUNT(*) FROM warehouse_locations",
        "inventory_lots": (
            "SELECT COUNT(*), COALESCE(SUM(quantity_available), 0), "
            "COALESCE(SUM(quantity_reserved), 0), COALESCE(SUM(quantity_consumed), 0) "
            "FROM inventory_lots"
        ),
        "inventory_movements": "SELECT COUNT(*) FROM inventory_movements",
        "inventory_location_movements": (
            "SELECT COUNT(*) FROM inventory_location_movements"
        ),
        "sales_orders": (
            "SELECT COUNT(*), COALESCE(SUM(total_amount), 0) FROM sales_orders"
        ),
        "sales_order_items": (
            "SELECT COUNT(*), COALESCE(SUM(quantity), 0) FROM sales_order_items"
        ),
        "material_requisitions": (
            "SELECT COUNT(*) FROM material_requisitions"
        ),
        "material_requisition_items": (
            "SELECT COUNT(*), COALESCE(SUM(requisition_qty), 0) "
            "FROM material_requisition_items"
        ),
    }
    with factory() as db:
        return {
            name: tuple(db.execute(text(sql)).one())
            for name, sql in queries.items()
        }


def test_admin_creates_mold_and_common_box_binding_is_searchable(mold_app) -> None:
    app, _factory = mold_app
    with TestClient(app) as client:
        _login(client, "admin")
        created_mold = client.post(
            "/api/warehouse/molds",
            json={
                "mold_code": "MJ-A1-001",
                "mold_name": "21301010 开槽模",
                "rack_location": "二楼模具架 B-12",
                "remarks": "天华常用模具",
            },
        )
        assert created_mold.status_code == 201, created_mold.text
        mold = created_mold.json()

        created_product = client.post(
            "/api/master/products",
            json={
                "customer_id": 1,
                "product_code": "21301010",
                "customer_material_code": "TH-21301010",
                "product_name": "天华测试外箱",
                "box_category": "normal",
                "production_process": "模切",
                "mold_tool_id": mold["id"],
            },
        )
        assert created_product.status_code == 201, created_product.text
        product = created_product.json()
        assert product["mold_tool"]["mold_code"] == "MJ-A1-001"
        assert product["mold_tool"]["rack_location"] == "二楼模具架 B-12"

        by_product = client.get("/api/warehouse/molds", params={"q": "21301010"})
        assert by_product.status_code == 200, by_product.text
        row = by_product.json()["items"][0]
        assert row["mold_code"] == "MJ-A1-001"
        assert row["product_count"] == 1
        assert row["products"][0]["product_code"] == "21301010"

        legacy_lookup = client.get(
            "/api/warehouse/references/template-locations",
            params={"q": "B-12"},
        )
        assert legacy_lookup.status_code == 200, legacy_lookup.text
        assert legacy_lookup.json()["items"][0]["template_location"] == "二楼模具架 B-12"


def test_workshop_can_query_but_cannot_modify_mold(mold_app) -> None:
    app, factory = mold_app
    from app.models.mold_tool import MoldTool
    from app.models.product import Product

    with factory() as db:
        mold = MoldTool(
            mold_code="MJ-W-001",
            mold_name="车间查询模",
            rack_location="一楼模具架 A-03",
        )
        db.add(mold)
        db.flush()
        db.add(
            Product(
                customer_id=1,
                product_code="WORK-001",
                customer_material_code="WORK-M-001",
                product_name="车间查询产品",
                box_category="normal",
                mold_tool_id=mold.id,
            )
        )
        db.commit()

    with TestClient(app) as client:
        _login(client, "workshop")
        listed = client.get("/api/warehouse/molds", params={"q": "WORK-001"})
        assert listed.status_code == 200, listed.text
        assert listed.json()["items"][0]["rack_location"] == "一楼模具架 A-03"
        denied = client.post(
            "/api/warehouse/molds",
            json={
                "mold_code": "DENIED",
                "mold_name": "无权限",
                "rack_location": "无权限",
            },
        )
        assert denied.status_code == 403


def test_workshop_can_open_structured_location_label_and_qr(
    mold_app, monkeypatch
) -> None:
    app, factory = mold_app
    from app.api import warehouse
    from app.models.mold_tool import MoldTool
    from app.models.product import Product

    monkeypatch.setattr(warehouse, "_lan_ip", lambda: "192.168.3.80")
    with factory() as db:
        mold = MoldTool(
            mold_code="MJ-MOBILE-001",
            mold_name="手机查找测试模",
            rack_location="3F-M-R02-L2-D03-P08",
        )
        db.add(mold)
        db.flush()
        db.add(
            Product(
                customer_id=1,
                product_code="MOBILE-P001",
                customer_material_code="MOBILE-M001",
                product_name="手机查找测试产品",
                box_category="die_cut",
                production_process="模切",
                report_length_mm=1200,
                report_width_mm=800,
                report_notes="长边顺瓦楞方向",
                mold_tool_id=mold.id,
            )
        )
        db.commit()
        mold_id = mold.id

    with TestClient(app, base_url="http://testserver:18045") as client:
        _login(client, "workshop")
        listed = client.get("/api/warehouse/molds", params={"q": "MOBILE-P001"})
        assert listed.status_code == 200, listed.text
        row = listed.json()["items"][0]
        assert row["location_guide"]["kind"] == "flat"
        assert "三楼模具区" in row["location_guide"]["prompt"]
        assert "第2号货架" in row["location_guide"]["prompt"]
        assert row["products"][0]["report_specification"] == "1200 × 800"
        assert row["products"][0]["direction_note"] == "长边顺瓦楞方向"

        label = client.get(f"/api/warehouse/molds/{mold_id}/label")
        assert label.status_code == 200, label.text
        data = label.json()
        assert data["lookup_url"] == (
            "http://192.168.3.80:18045/mobile/mold-lookup?mold=MJ-MOBILE-001"
        )
        assert data["qr_data_url"].startswith("data:image/png;base64,")
        assert data["products"][0]["product_code"] == "MOBILE-P001"


@pytest.mark.parametrize(
    ("location", "kind", "expected"),
    [
        ("3F-M-R02-L2-D03-P08", "flat", "第3排，从左到右第8块"),
        ("3F-M-R01-L1-V-P12", "vertical", "底层（第1层）竖放区，从左到右第12块"),
        ("二楼模具架 B-12", "manual", "请前往“二楼模具架 B-12”查找"),
    ],
)
def test_mold_location_prompt_is_immediately_readable(
    location: str, kind: str, expected: str
) -> None:
    from app.services.mold_location import describe_mold_location

    result = describe_mold_location(location)
    assert result["kind"] == kind
    assert expected in result["prompt"]
    assert "核对模具编号和存货编码" in result["prompt"]


def test_order_response_exposes_current_mold_location_to_workshop(mold_app) -> None:
    app, factory = mold_app
    from app.models.mold_tool import MoldTool
    from app.models.order import Order, OrderItem
    from app.models.product import Product

    with factory() as db:
        mold = MoldTool(
            mold_code="MJ-ORDER-001",
            mold_name="订单生产模",
            rack_location="生产模具架 C-08",
        )
        db.add(mold)
        db.flush()
        product = Product(
            customer_id=1,
            product_code="ORDER-MOLD-001",
            customer_material_code="ORDER-MOLD-M1",
            product_name="订单模具联动产品",
            box_category="normal",
            production_process="模切",
            mold_tool_id=mold.id,
        )
        db.add(product)
        db.flush()
        order = Order(
            order_number="TM20990101001",
            customer_id=1,
            customer_po="MOLD-PO-001",
            order_date=date.today(),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("10.00"),
        )
        db.add(order)
        db.flush()
        db.add(
            OrderItem(
                order_id=order.id,
                product_id=product.id,
                item_order_number="TM20990101001-001",
                item_sequence=1,
                quantity=10,
                delivered_quantity=0,
                unit_price=Decimal("1.0000"),
                subtotal=Decimal("10.00"),
                material_status="pending",
                snapshot_product_name=product.product_name,
                snapshot_product_code=product.product_code,
                requisition_status="未报料",
                special_process="无",
            )
        )
        db.commit()

    with TestClient(app) as client:
        _login(client, "workshop")
        response = client.get("/api/orders", params={"page_size": 25})
        assert response.status_code == 200, response.text
        item = response.json()["items"][0]["items"][0]
        assert item["mold_code"] == "MJ-ORDER-001"
        assert item["mold_name"] == "订单生产模"
        assert item["mold_location"] == "生产模具架 C-08"
        assert "unit_price" not in item


def test_mold_frontend_connects_location_common_box_and_order_display() -> None:
    warehouse = Path("static/warehouse.html").read_text(encoding="utf-8")
    index = Path("static/index.html").read_text(encoding="utf-8")
    for marker in (
        "新增 / 编辑生产模具",
        "固定货架位置",
        "/api/warehouse/molds",
        "已绑定常用箱",
    ):
        assert marker in warehouse
    assert 'v-model="productForm.mold_tool_id"' in index
    assert 'v-if="productUsesMold(productForm)"' in index
    assert "productMoldError" in index
    assert "moldLocationText(item)" in index
    assert "模具：{{ moldLocationText(item) }}" in index


@pytest.mark.parametrize(
    ("target_location", "expected_kind"),
    [
        ("3f-m-r02-l2-d03-p08", "flat"),
        ("3F-M-R01-L1-V-P12", "vertical"),
    ],
)
def test_double_code_move_is_versioned_idempotent_and_does_not_touch_business_data(
    mold_app,
    monkeypatch,
    target_location: str,
    expected_kind: str,
) -> None:
    app, factory = mold_app
    from app.api import warehouse
    from app.core.time_contract import utc_now_naive
    from app.models.mold_tool import MoldLocationMovement, MoldTool
    from app.models.order import Order
    from app.models.requisition import Requisition
    from app.models.warehouse_inventory import InventoryLot, WarehouseLocation
    from app.models.audit import OperationLog
    from sqlalchemy import func, select

    monkeypatch.setattr(warehouse, "_lan_ip", lambda: "192.168.3.80")
    with factory() as db:
        mold = MoldTool(
            mold_code="MJ-MOVE-001",
            mold_name="双码移动测试模",
            rack_location="二楼模具架 B-12",
        )
        location = WarehouseLocation(
            location_code="FG-GUARD-01",
            location_name="成品保护基线",
            warehouse_type="finished",
        )
        db.add_all([mold, location])
        db.flush()
        db.add_all(
            [
                InventoryLot(
                    lot_number="FG-GUARD-LOT-01",
                    inventory_type="finished",
                    warehouse_location_id=location.id,
                    quantity_available=17,
                    quantity_reserved=3,
                    quantity_consumed=2,
                    unit="boxes",
                    source_type="manual",
                    stock_date=date.today(),
                    last_movement_at=utc_now_naive(),
                ),
                Order(
                    order_number="TM20990101088",
                    customer_id=1,
                    order_date=date.today(),
                    status="pending_production",
                    payment_status="unpaid",
                    total_amount=Decimal("88.00"),
                ),
                Requisition(
                    requisition_number="MR20990101088",
                    requisition_date=date.today(),
                ),
            ]
        )
        db.commit()
        mold_id = mold.id

    protected_before = _protected_business_state(factory)
    payload = {
        "mold_code": "MJ-MOVE-001",
        "target_location": target_location,
    }
    with TestClient(app, base_url="http://testserver:18066") as client:
        _login(client, "workshop")
        preview = client.post(
            "/api/warehouse/molds/location-movement/preview",
            json=payload,
        )
        assert preview.status_code == 200, preview.text
        preview_data = preview.json()
        assert preview_data["target_guide"]["kind"] == expected_kind
        assert preview_data["target_location"].startswith("3F-M-")
        assert preview_data["expected_version"] == 1
        assert preview_data["can_confirm"] is True

        confirmation = {
            **payload,
            "expected_version": preview_data["expected_version"],
            "idempotency_key": f"n022-move-{expected_kind}-0001",
            "source": "scanner_paste",
            "note": "专项测试移动",
        }
        moved = client.post(
            "/api/warehouse/molds/location-movement/confirm",
            json=confirmation,
        )
        assert moved.status_code == 200, moved.text
        moved_data = moved.json()
        assert moved_data["idempotent_replay"] is False
        assert moved_data["mold"]["location_version"] == 2
        assert moved_data["mold"]["rack_location"] == preview_data["target_location"]
        assert moved_data["movement"]["expected_version"] == 1
        assert moved_data["movement"]["resulting_version"] == 2

        replayed = client.post(
            "/api/warehouse/molds/location-movement/confirm",
            json=confirmation,
        )
        assert replayed.status_code == 200, replayed.text
        replayed_data = replayed.json()
        assert replayed_data["idempotent_replay"] is True
        assert replayed_data["movement"] == moved_data["movement"]

        stale = client.post(
            "/api/warehouse/molds/location-movement/confirm",
            json={**confirmation, "idempotency_key": f"stale-{expected_kind}-0002"},
        )
        assert stale.status_code == 409

        whitespace_key = client.post(
            "/api/warehouse/molds/location-movement/confirm",
            json={
                "mold_code": "MJ-MOVE-A",
                "target_location": "3F-M-R03-L1-D01-P01",
                "expected_version": 1,
                "idempotency_key": "        ",
                "source": "api",
            },
        )
        assert whitespace_key.status_code == 422

        listed = client.get("/api/warehouse/molds", params={"q": "MJ-MOVE-001"})
        assert listed.status_code == 200, listed.text
        assert listed.json()["items"][0]["rack_location"] == preview_data["target_location"]
        label = client.get(f"/api/warehouse/molds/{mold_id}/label")
        assert label.status_code == 200, label.text
        assert label.json()["rack_location"] == preview_data["target_location"]

    assert _protected_business_state(factory) == protected_before
    with factory() as db:
        movements = db.scalars(select(MoldLocationMovement)).all()
        assert len(movements) == 1
        assert movements[0].to_location == preview_data["target_location"]
        assert db.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.description == "双码确认模具位置移动"
            )
        ) == 1


def test_move_rejects_invalid_occupied_stale_and_reused_requests_without_noop_ledger(
    mold_app,
) -> None:
    app, factory = mold_app
    from app.models.mold_tool import MoldLocationMovement, MoldTool
    from sqlalchemy import func, select

    with factory() as db:
        db.add_all(
            [
                MoldTool(
                    mold_code="MJ-MOVE-A",
                    mold_name="待移动模具",
                    rack_location="3F-M-R01-L1-D01-P01",
                ),
                MoldTool(
                    mold_code="MJ-MOVE-B",
                    mold_name="占位模具",
                    rack_location="3F-M-R02-L1-D01-P01",
                ),
            ]
        )
        db.commit()

    with TestClient(app) as client:
        _login(client, "workshop")
        for invalid in ("二楼模具架 B-12", "3F-M-R02-L4-D03-P08"):
            response = client.post(
                "/api/warehouse/molds/location-movement/preview",
                json={"mold_code": "MJ-MOVE-A", "target_location": invalid},
            )
            assert response.status_code == 422, response.text

        conflict_preview = client.post(
            "/api/warehouse/molds/location-movement/preview",
            json={
                "mold_code": "MJ-MOVE-A",
                "target_location": "3f-m-r02-l1-d01-p01",
            },
        )
        assert conflict_preview.status_code == 200, conflict_preview.text
        assert conflict_preview.json()["can_confirm"] is False
        assert conflict_preview.json()["occupancy_conflict"]["mold_code"] == "MJ-MOVE-B"
        occupied = client.post(
            "/api/warehouse/molds/location-movement/confirm",
            json={
                "mold_code": "MJ-MOVE-A",
                "target_location": "3F-M-R02-L1-D01-P01",
                "expected_version": 1,
                "idempotency_key": "occupied-target-001",
                "source": "manual_input",
            },
        )
        assert occupied.status_code == 409

        stale = client.post(
            "/api/warehouse/molds/location-movement/confirm",
            json={
                "mold_code": "MJ-MOVE-A",
                "target_location": "3F-M-R03-L1-D01-P01",
                "expected_version": 2,
                "idempotency_key": "stale-version-001",
                "source": "api",
            },
        )
        assert stale.status_code == 409

        same = client.post(
            "/api/warehouse/molds/location-movement/confirm",
            json={
                "mold_code": "MJ-MOVE-A",
                "target_location": "3f-m-r01-l1-d01-p01",
                "expected_version": 1,
                "idempotency_key": "same-location-001",
                "source": "manual_input",
            },
        )
        assert same.status_code == 200, same.text
        assert same.json()["no_change"] is True
        assert same.json()["movement"] is None

        moved_payload = {
            "mold_code": "MJ-MOVE-A",
            "target_location": "3F-M-R03-L1-D01-P01",
            "expected_version": 1,
            # A no-change request creates no business fact, so it does not
            # consume this key; the first real movement may use it.
            "idempotency_key": "same-location-001",
            "source": "manual_input",
            "note": "首次真实移动",
        }
        moved = client.post(
            "/api/warehouse/molds/location-movement/confirm",
            json=moved_payload,
        )
        assert moved.status_code == 200, moved.text
        replayed = client.post(
            "/api/warehouse/molds/location-movement/confirm",
            json=moved_payload,
        )
        assert replayed.status_code == 200, replayed.text
        assert replayed.json()["idempotent_replay"] is True
        assert replayed.json()["movement"] == moved.json()["movement"]

        changed_business_requests = (
            {"mold_code": "MJ-MOVE-B"},
            {"target_location": "3F-M-R04-L1-D01-P01"},
            {"expected_version": 2},
            {"source": "api"},
            {"note": "不同备注"},
        )
        for changed_fields in changed_business_requests:
            conflict = client.post(
                "/api/warehouse/molds/location-movement/confirm",
                json={**moved_payload, **changed_fields},
            )
            assert conflict.status_code == 409, (
                changed_fields,
                conflict.text,
            )
            assert "幂等键已用于不同的模具移动业务" in conflict.json()["detail"]

        _login(client, "admin")
        bypass = client.put(
            f"/api/warehouse/molds/{moved.json()['mold']['id']}",
            json={
                "mold_code": "MJ-MOVE-A",
                "mold_name": "待移动模具",
                "rack_location": "3F-M-R05-L1-D01-P01",
            },
        )
        assert bypass.status_code == 409

    from app.services.mold_location import MoldLocationError, confirm_mold_location_move

    with factory() as db:
        with pytest.raises(MoldLocationError, match="至少需要 8 个字符"):
            confirm_mold_location_move(
                db,
                mold_code="MJ-MOVE-A",
                target_location="3F-M-R05-L1-D01-P01",
                expected_version=2,
                idempotency_key="        ",
                actor_id=None,
                source="api",
                note=None,
            )
        assert db.scalar(select(func.count(MoldLocationMovement.id))) == 1


def test_view_only_permission_can_preview_but_cannot_confirm_mold_move(mold_app) -> None:
    app, factory = mold_app
    from app.models.mold_tool import MoldTool

    with factory() as db:
        db.add(
            MoldTool(
                mold_code="MJ-VIEW-ONLY",
                mold_name="只读预览模具",
                rack_location="3F-M-R01-L1-D01-P02",
            )
        )
        db.commit()

    with TestClient(app) as client:
        _login(client, "sales")
        preview = client.post(
            "/api/warehouse/molds/location-movement/preview",
            json={
                "mold_code": "MJ-VIEW-ONLY",
                "target_location": "3F-M-R02-L1-D01-P02",
            },
        )
        assert preview.status_code == 200, preview.text
        denied = client.post(
            "/api/warehouse/molds/location-movement/confirm",
            json={
                "mold_code": "MJ-VIEW-ONLY",
                "target_location": "3F-M-R02-L1-D01-P02",
                "expected_version": 1,
                "idempotency_key": "view-only-denied-001",
                "source": "manual_input",
            },
        )
        assert denied.status_code == 403


def test_mobile_mold_page_keeps_lookup_and_adds_double_code_confirmation() -> None:
    mobile = Path("static/mobile_mold_lookup.html").read_text(encoding="utf-8")
    for marker in (
        "模具双码移动确认",
        "moveMoldCode",
        "moveLocationCode",
        "/api/warehouse/molds/location-movement/preview",
        "/api/warehouse/molds/location-movement/confirm",
        "warehouse.execute",
        "idempotency_key",
        "/mold-label.html?mold_id=",
        "/api/warehouse/molds?q=",
    ):
        assert marker in mobile


def test_mobile_mold_page_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for frontend syntax validation"
    mobile = Path("static/mobile_mold_lookup.html").read_text(encoding="utf-8")
    scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>", mobile, flags=re.DOTALL
        )
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "mobile-mold-location-movement-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_mobile_mold_lookup_and_print_label_are_local_and_auth_guarded() -> None:
    root = Path(__file__).resolve().parents[1]
    mobile = (root / "static" / "mobile_mold_lookup.html").read_text(
        encoding="utf-8"
    )
    label = (root / "static" / "mold-label.html").read_text(encoding="utf-8")
    main = (root / "app" / "main.py").read_text(encoding="utf-8")
    warehouse = (root / "static" / "warehouse.html").read_text(encoding="utf-8")

    assert "/mobile/mold-lookup" in main
    assert "/mold-label.html" in main
    assert "/api/auth/me" in mobile
    assert "/api/warehouse/molds?q=" in mobile
    assert "location_guide?.prompt" in mobile
    assert "大模具请按现场要求两人搬运" in mobile
    assert "/api/warehouse/molds/${id}/label" in label
    assert "window.print()" in label
    assert "3F-M-R02-L2-D03-P08" in warehouse
    assert "打印标签" in warehouse
    assert "<script src=" not in mobile
    assert "<script src=" not in label


def test_mold_is_required_only_for_die_cut_products(mold_app) -> None:
    app, _factory = mold_app
    with TestClient(app) as client:
        _login(client, "admin")
        missing = client.post(
            "/api/master/products",
            json={
                "customer_id": 1,
                "product_code": "DIE-NO-MOLD",
                "customer_material_code": "DIE-NO-MOLD",
                "product_name": "缺少模具",
                "box_category": "die_cut",
                "production_process": "模切",
            },
        )
        assert missing.status_code == 400
        assert "必须选择已登记的生产模具" in missing.json()["detail"]

        mold = client.post(
            "/api/warehouse/molds",
            json={
                "mold_code": "MJ-NON-DIE",
                "mold_name": "非模切不应绑定",
                "rack_location": "测试架 Z-01",
            },
        ).json()
        ordinary = client.post(
            "/api/master/products",
            json={
                "customer_id": 1,
                "product_code": "NO-DIE-CLEAR",
                "customer_material_code": "NO-DIE-CLEAR",
                "product_name": "普通产品",
                "box_category": "normal",
                "production_process": "粘贴",
                "mold_tool_id": mold["id"],
            },
        )
        assert ordinary.status_code == 201, ordinary.text
        assert ordinary.json()["mold_tool_id"] is None
        assert ordinary.json()["mold_tool"] is None
