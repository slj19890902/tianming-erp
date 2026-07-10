"""tests/test_phase192_report_crease.py

v0.19.2-B: 报料尺寸 + 压线字段测试套件。

覆盖：
  PROD  : 常用箱产品报料字段 CRUD
  ORDER : 新建订单时写入报料快照
  API   : 订单 API 返回报料快照字段
  REQ   : 报料管理 pending 返回报料字段
  MERGE : 合并建议 API 返回可合并分组
  ITEM  : 订单明细编辑 PUT 更新报料快照 + 材质联动字段
  SYNC  : sync-fields 支持报料字段白名单

运行命令:
    python -X utf8 -m pytest tests/test_phase192_report_crease.py -q
"""
from __future__ import annotations

import sys
sys.path.insert(0, ".")

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker, Session

import app.models  # noqa: F401 – registers all ORM models / tables


# ─────────────────────────────────────────────────────────────────────────────
# Shared app fixture (module scope for speed)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def api_app(tmp_path_factory):
    """Build a minimal FastAPI app backed by a fresh SQLite DB."""
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.api.deps import get_db

    db_path = tmp_path_factory.mktemp("rptcrease") / "rpc.sqlite3"
    engine = create_sqlite_engine(db_path)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False,
                                   expire_on_commit=False)

    # Override get_db to use our engine
    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    # Import routers
    from app.api.auth import router as auth_router
    from app.api.customers import router as customers_router
    from app.api.materials import router as materials_router
    from app.api.products import router as products_router
    from app.api.orders import router as orders_router
    from app.api.requisition import router as req_router

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(customers_router, prefix="/api/master/customers", tags=["master-customers"])
    app.include_router(materials_router, prefix="/api/master/materials", tags=["master-materials"])
    app.include_router(products_router, prefix="/api/master/products", tags=["master-products"])
    app.include_router(orders_router, prefix="/api/orders")
    app.include_router(req_router, prefix="/api/requisition")
    app.dependency_overrides[get_db] = override_get_db

    return app, session_factory


@pytest.fixture(scope="module")
def client(api_app):
    app, _ = api_app
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture(scope="module")
def db_session(api_app):
    _, session_factory = api_app
    db = session_factory()
    yield db
    db.close()


@pytest.fixture(scope="module")
def admin_cookies(api_app, client):
    from app.core.security import hash_password
    from app.models.user import User
    _, session_factory = api_app
    # 直接往 DB 写 admin 用户，绕过 init endpoint
    with session_factory() as db:
        existing = db.query(User).filter(User.username == "admin").first()
        if not existing:
            user = User(
                username="admin",
                password_hash=hash_password("Admin1234!"),
                role="admin",
                real_name="管理员",
                display_name="管理员",
                must_change_password=False,
                is_active=True,
            )
            db.add(user)
            db.commit()
    r = client.post("/api/auth/login", json={"username": "admin", "password": "Admin1234!"})
    assert r.status_code == 200, r.text
    return r.cookies


@pytest.fixture(scope="module")
def customer_id(client, admin_cookies):
    r = client.post(
        "/api/master/customers",
        json={"name": "测试客户RC", "customer_code": "RC001",
              "customer_number": 9901, "payment_days": 30,
              "delivery_method": "配送"},
        cookies=admin_cookies,
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


@pytest.fixture(scope="module")
def material_id(client, admin_cookies):
    r = client.post(
        "/api/master/materials",
        json={"code": "A6D-B", "layer_count": 5, "flute_type": "AB",
              "basis_weight_description": "150/130/130",
              "supplier_name": "苏州嘉林亿", "quote_price": "1.2"},
        cookies=admin_cookies,
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


@pytest.fixture(scope="module")
def product_id_a(client, admin_cookies, customer_id, material_id):
    r = client.post(
        "/api/master/products",
        json={
            "customer_id": customer_id,
            "product_code": "21301851",
            "customer_material_code": "21301851",
            "product_name": "测试纸箱A",
            "material_id": material_id,
            "length_mm": "435", "width_mm": "325", "height_mm": "275",
            "box_category": "normal",
            "layer_count": 5, "flute_type": "AB",
            "sale_unit_price": "3.50",
            "report_length_mm": 1550,
            "report_width_mm": 600,
            "crease_type": "压线",
            "crease_left_mm": 162,
            "crease_middle_mm": 276,
            "crease_right_mm": 162,
            "report_notes": "供应商直发",
        },
        cookies=admin_cookies,
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


@pytest.fixture(scope="module")
def product_id_b(client, admin_cookies, customer_id, material_id):
    """同报料规格的第二个产品（用于合并建议测试）。"""
    r = client.post(
        "/api/master/products",
        json={
            "customer_id": customer_id,
            "product_code": "21301852",
            "customer_material_code": "21301852",
            "product_name": "测试纸箱B",
            "material_id": material_id,
            "length_mm": "435", "width_mm": "325", "height_mm": "275",
            "box_category": "normal",
            "layer_count": 5, "flute_type": "AB",
            "sale_unit_price": "3.50",
            "report_length_mm": 1550,
            "report_width_mm": 600,
            "crease_type": "压线",
            "crease_left_mm": 162,
            "crease_middle_mm": 276,
            "crease_right_mm": 162,
        },
        cookies=admin_cookies,
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


# ─────────────────────────────────────────────────────────────────────────────
# PROD: 常用箱产品报料字段 CRUD
# ─────────────────────────────────────────────────────────────────────────────

class TestProductReportFields:
    def test_get_has_report_fields(self, client, admin_cookies, product_id_a):
        r = client.get(f"/api/master/products/{product_id_a}", cookies=admin_cookies)
        assert r.status_code == 200
        d = r.json()
        assert d["report_length_mm"] == 1550
        assert d["report_width_mm"] == 600
        assert d["crease_type"] == "压线"
        assert d["crease_left_mm"] == 162
        assert d["crease_middle_mm"] == 276
        assert d["crease_right_mm"] == 162
        assert d["report_notes"] == "供应商直发"

    def test_update_crease_type_净料(self, client, admin_cookies, customer_id, material_id, product_id_a):
        r = client.get(f"/api/master/products/{product_id_a}", cookies=admin_cookies)
        base = r.json()
        payload = {
            "customer_id": base["customer_id"],
            "product_code": base["product_code"],
            "customer_material_code": base["customer_material_code"],
            "product_name": base["product_name"],
            "material_id": base["material_id"],
            "length_mm": base["length_mm"],
            "width_mm": base["width_mm"],
            "height_mm": base["height_mm"],
            "box_category": base["box_category"],
            "layer_count": base["layer_count"],
            "flute_type": base["flute_type"],
            "sale_unit_price": base["sale_unit_price"],
            "report_length_mm": 560,
            "report_width_mm": 430,
            "crease_type": "净料",
            "crease_left_mm": None,
            "crease_middle_mm": None,
            "crease_right_mm": None,
        }
        r2 = client.put(f"/api/master/products/{product_id_a}", json=payload, cookies=admin_cookies)
        assert r2.status_code == 200, r2.text
        d = r2.json()
        assert d["crease_type"] == "净料"
        assert d["crease_left_mm"] is None
        assert d["report_length_mm"] == 560

    def test_update_crease_type_毛片(self, client, admin_cookies, product_id_a):
        r = client.get(f"/api/master/products/{product_id_a}", cookies=admin_cookies)
        base = r.json()
        payload = {k: v for k, v in base.items()
                   if k in {"customer_id", "product_code", "customer_material_code",
                             "product_name", "material_id", "length_mm", "width_mm",
                             "height_mm", "box_category", "layer_count", "flute_type",
                             "sale_unit_price", "report_length_mm", "report_width_mm"}}
        payload["crease_type"] = "毛片"
        r2 = client.put(f"/api/master/products/{product_id_a}", json=payload, cookies=admin_cookies)
        assert r2.status_code == 200, r2.text
        assert r2.json()["crease_type"] == "毛片"

    def test_update_crease_压线_three_segments(self, client, admin_cookies, product_id_a):
        r = client.get(f"/api/master/products/{product_id_a}", cookies=admin_cookies)
        base = r.json()
        payload = {k: v for k, v in base.items()
                   if k in {"customer_id", "product_code", "customer_material_code",
                             "product_name", "material_id", "length_mm", "width_mm",
                             "height_mm", "box_category", "layer_count", "flute_type",
                             "sale_unit_price", "report_length_mm", "report_width_mm"}}
        payload.update(crease_type="压线", crease_left_mm=162,
                       crease_middle_mm=276, crease_right_mm=162)
        r2 = client.put(f"/api/master/products/{product_id_a}", json=payload, cookies=admin_cookies)
        assert r2.status_code == 200, r2.text
        d = r2.json()
        assert d["crease_type"] == "压线"
        assert d["crease_left_mm"] == 162
        assert d["crease_middle_mm"] == 276
        assert d["crease_right_mm"] == 162
        # 压线无小数点（整数存储）
        assert isinstance(d["crease_left_mm"], int)

    def test_sync_fields_report(self, client, admin_cookies, product_id_a):
        r = client.post(
            f"/api/master/products/{product_id_a}/sync-fields",
            json={"fields": {"report_length_mm": 1560, "report_width_mm": 610,
                             "crease_type": "压线",
                             "crease_left_mm": 162, "crease_middle_mm": 286,
                             "crease_right_mm": 162}},
            cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text
        assert "report_length_mm" in r.json()["updated"]
        assert "crease_type" in r.json()["updated"]

    def test_sync_fields_enforces_product_layer_flute_boundary(
        self, client, admin_cookies, product_id_a
    ):
        rejected = client.post(
            f"/api/master/products/{product_id_a}/sync-fields",
            json={"fields": {"layer_count": 3, "flute_type": "AB"}},
            cookies=admin_cookies,
        )
        assert rejected.status_code == 400, rejected.text
        assert "三层瓦楞只能是 A / B / E" in rejected.json()["detail"]

        unchanged = client.get(
            f"/api/master/products/{product_id_a}", cookies=admin_cookies
        ).json()
        assert (unchanged["layer_count"], unchanged["flute_type"]) == (5, "AB")

        accepted = client.post(
            f"/api/master/products/{product_id_a}/sync-fields",
            json={"fields": {"layer_count": 5, "flute_type": "AB"}},
            cookies=admin_cookies,
        )
        assert accepted.status_code == 200, accepted.text


# ─────────────────────────────────────────────────────────────────────────────
# ORDER: 新建订单时写入报料快照
# ─────────────────────────────────────────────────────────────────────────────

class TestOrderReportSnapshot:
    @pytest.fixture(scope="class")
    def order_resp(self, client, admin_cookies, customer_id, product_id_a):
        r = client.post(
            "/api/orders",
            json={
                "customer_id": customer_id,
                "customer_po": "PO-REPORT-001",
                "items": [{"product_id": product_id_a, "quantity": 500, "unit_price": "3.50"}],
            },
            cookies=admin_cookies,
        )
        assert r.status_code in (200, 201), r.text
        return r.json()

    def test_order_item_snapshot_fields_exist(self, order_resp):
        items = order_resp.get("items", [])
        assert len(items) >= 1
        item = items[0]
        for field in ("snapshot_report_length_mm", "snapshot_report_width_mm",
                      "snapshot_crease_type", "snapshot_crease_left_mm",
                      "snapshot_crease_middle_mm", "snapshot_crease_right_mm",
                      "snapshot_report_notes", "snapshot_splice_mode",
                      "snapshot_pieces_per_box", "snapshot_flap_mm"):
            assert field in item, f"missing {field}"

    def test_order_detail_has_report_snapshot(self, client, admin_cookies, order_resp):
        order_id = order_resp["id"]
        r = client.get(f"/api/orders/{order_id}", cookies=admin_cookies)
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) >= 1
        item = items[0]
        assert "snapshot_report_length_mm" in item
        assert "snapshot_crease_type" in item


# ─────────────────────────────────────────────────────────────────────────────
# REQ: 报料管理 pending 返回报料字段
# ─────────────────────────────────────────────────────────────────────────────

class TestRequisitionReportFields:
    def test_pending_has_report_fields(self, client, admin_cookies):
        r = client.get("/api/requisition/pending", cookies=admin_cookies)
        assert r.status_code == 200
        items = r.json()["items"]
        if items:
            item = items[0]
            assert "snapshot_report_length_mm" in item
            assert "snapshot_report_width_mm" in item
            assert "snapshot_crease_type" in item
            assert "pieces_per_box" in item
            assert "required_piece_qty" in item
            assert "cutting_mode" in item


# ─────────────────────────────────────────────────────────────────────────────
# MERGE: 合并建议 API
# ─────────────────────────────────────────────────────────────────────────────

class TestMergeSuggestions:
    @pytest.fixture(scope="class", autouse=True)
    def seed_two_orders(self, client, admin_cookies, customer_id, product_id_a, product_id_b):
        """创建两个产品各一张订单，两者报料规格一致，应出现合并建议。"""
        for pid in [product_id_a, product_id_b]:
            r = client.post(
                "/api/orders",
                json={
                    "customer_id": customer_id,
                    "customer_po": f"PO-MERGE-{pid}",
                    "items": [{"product_id": pid, "quantity": 200, "unit_price": "3.50"}],
                },
                cookies=admin_cookies,
            )
            assert r.status_code in (200, 201), r.text

    def test_merge_suggestions_returns_list(self, client, admin_cookies):
        r = client.get("/api/requisition/merge-suggestions", cookies=admin_cookies)
        assert r.status_code == 200, r.text
        assert "suggestions" in r.json()

    def test_merge_group_has_two_members(self, client, admin_cookies):
        r = client.get("/api/requisition/merge-suggestions", cookies=admin_cookies)
        suggestions = r.json()["suggestions"]
        # 至少有一组包含 >= 2 个成员
        multi = [s for s in suggestions if s["member_count"] >= 2]
        assert len(multi) >= 1, f"No multi-member group found. suggestions={suggestions}"

    def test_merge_group_fields(self, client, admin_cookies):
        r = client.get("/api/requisition/merge-suggestions", cookies=admin_cookies)
        for s in r.json()["suggestions"]:
            assert "supplier_name" in s
            assert "report_length_mm" in s
            assert "report_width_mm" in s
            assert "crease_display" in s
            assert "total_quantity" in s
            assert "pieces_per_box" in s
            assert "splice_mode" in s
            assert "cutting_mode" in s
            assert "total_required_piece_qty" in s
            assert "members" in s
            for m in s["members"]:
                assert "product_code" in m
                assert "quantity" in m
                assert "pieces_per_box" in m
                assert "required_piece_qty" in m


# ─────────────────────────────────────────────────────────────────────────────
# ITEM: 订单明细 PUT 更新报料快照 + 材质联动
# ─────────────────────────────────────────────────────────────────────────────

class TestOrderItemUpdateWithReport:
    @pytest.fixture(scope="class")
    def item_id(self, client, admin_cookies, customer_id, product_id_a):
        r = client.post(
            "/api/orders",
            json={
                "customer_id": customer_id,
                "customer_po": "PO-ITEM-EDIT-001",
                "items": [{"product_id": product_id_a, "quantity": 100, "unit_price": "3.50"}],
            },
            cookies=admin_cookies,
        )
        assert r.status_code in (200, 201), r.text
        return r.json()["items"][0]["id"]

    def test_update_report_snapshot(self, client, admin_cookies, item_id):
        r = client.put(
            f"/api/orders/items/{item_id}",
            json={
                "quantity": 100,
                "unit_price": "3.50",
                "product_code": "21301851",
                "product_name": "测试纸箱A",
                "snapshot_report_length_mm": 1555,
                "snapshot_report_width_mm": 605,
                "snapshot_crease_type": "压线",
                "snapshot_crease_left_mm": 163,
                "snapshot_crease_middle_mm": 279,
                "snapshot_crease_right_mm": 163,
                "snapshot_report_notes": "手动修改报料",
            },
            cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text

    def test_update_material_id_and_layer(self, client, admin_cookies, item_id, material_id):
        r = client.put(
            f"/api/orders/items/{item_id}",
            json={
                "quantity": 100,
                "unit_price": "3.50",
                "product_code": "21301851",
                "product_name": "测试纸箱A",
                "material_id": material_id,
                "layer_count": 5,
                "flute_type": "AB",
            },
            cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text


# ─────────────────────────────────────────────────────────────────────────────
# MANUAL: 常用箱"未修改/已修改"状态标记（v0.22.1 阶段 1A · Task E2）
# ─────────────────────────────────────────────────────────────────────────────

class TestManualModifiedFlag:
    def test_create_sets_manual_modified(self, client, admin_cookies, product_id_b):
        r = client.get(f"/api/master/products/{product_id_b}", cookies=admin_cookies)
        assert r.status_code == 200
        d = r.json()
        assert d["manual_modified"] is True
        assert d["manual_modified_at"] is not None

    def test_update_product_sets_manual_modified(self, client, admin_cookies, product_id_b):
        r = client.get(f"/api/master/products/{product_id_b}", cookies=admin_cookies)
        base = r.json()
        payload = {k: v for k, v in base.items()
                   if k in {"customer_id", "product_code", "customer_material_code",
                             "product_name", "material_id", "length_mm", "width_mm",
                             "height_mm", "box_category", "layer_count", "flute_type",
                             "sale_unit_price"}}
        r2 = client.put(f"/api/master/products/{product_id_b}", json=payload, cookies=admin_cookies)
        assert r2.status_code == 200, r2.text
        assert r2.json()["manual_modified"] is True
        assert r2.json()["manual_modified_at"] is not None

    def test_status_toggle_does_not_change_manual_modified_at(
        self, client, admin_cookies, product_id_b
    ):
        before = client.get(f"/api/master/products/{product_id_b}", cookies=admin_cookies).json()
        toggled = client.put(
            f"/api/master/products/{product_id_b}/status",
            json={"is_active": False},
            cookies=admin_cookies,
        )
        assert toggled.status_code == 200, toggled.text
        after = toggled.json()
        assert after["is_active"] is False
        assert after["manual_modified_at"] == before["manual_modified_at"]
        # 恢复状态，避免影响其它用例
        restore = client.put(
            f"/api/master/products/{product_id_b}/status",
            json={"is_active": True},
            cookies=admin_cookies,
        )
        assert restore.status_code == 200
        assert restore.json()["manual_modified_at"] == before["manual_modified_at"]

    def test_sync_fields_does_not_change_manual_modified_at(
        self, client, admin_cookies, product_id_b
    ):
        before = client.get(f"/api/master/products/{product_id_b}", cookies=admin_cookies).json()
        r = client.post(
            f"/api/master/products/{product_id_b}/sync-fields",
            json={"fields": {"remark": "来自订单同步的备注"}},
            cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text
        after = client.get(f"/api/master/products/{product_id_b}", cookies=admin_cookies).json()
        assert after["manual_modified_at"] == before["manual_modified_at"]
