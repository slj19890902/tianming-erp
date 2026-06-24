"""
Phase 17 Tests: 楞型识别服务 + 版本 API + 楞型批量写入端点。

测试覆盖：
1. 楞型解析 parse_flute_from_text（8 场景）
2. GET /api/system/version 端点
3. GET /api/system/flute-mapping/preview
4. POST /api/system/flute-mapping/apply
5. 产品 API 包含 flute_type 字段
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

# ── Windows subprocess 编码护栏 ──────────────────────────────────────────
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")


# ─────────────────────────────── parse_flute_from_text 单元测试 ─────────

from app.services.flute_mapping import parse_flute_from_text


@pytest.mark.parametrize("text,expected_flute,expected_layer,expected_src", [
    # 5 个克重段 → AB 五层
    ("120/160/160/160/120", "AB", 5, "weight_count"),
    # W 面纸标识 + 4 克重段 = 5 层 → AB 五层
    ("W/160/160/160/120", "AB", 5, "weight_count"),
    # 3 个克重段 → A 三层
    ("160/160/120", "A", 3, "weight_count"),
    # /A 后缀覆盖 → A 五层（后缀优先级高于段数）
    ("120/160/120/A", "A", 5, "suffix"),
    # /E 后缀 → E 三层
    ("120/160/120/E", "E", 3, "suffix"),
    # /B 后缀 → B 三层
    ("120/160/120/B", "B", 3, "suffix"),
    # 纯中文无法识别
    ("无材质", None, None, "unrecognized"),
    # 空字符串
    ("", None, None, "unrecognized"),
])
def test_parse_flute_from_text(text, expected_flute, expected_layer, expected_src):
    result = parse_flute_from_text(text)
    assert result.flute_type == expected_flute, (
        f"text={text!r}: flute_type={result.flute_type!r}, expected={expected_flute!r}"
    )
    assert result.layer_count == expected_layer, (
        f"text={text!r}: layer_count={result.layer_count}, expected={expected_layer}"
    )
    assert result.source == expected_src, (
        f"text={text!r}: source={result.source!r}, expected={expected_src!r}"
    )


def test_parse_flute_none_input():
    result = parse_flute_from_text(None)
    assert result.flute_type is None
    assert result.layer_count is None
    assert result.source == "unrecognized"


def test_parse_flute_case_insensitive_suffix():
    result = parse_flute_from_text("120/160/120/e")
    assert result.flute_type == "E"
    assert result.layer_count == 3


# ─────────────────────────────── 集成测试 fixtures ─────────────────────────


@pytest.fixture()
def flute_api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """
    返回 (app, db_path, factory) 供集成测试使用。
    使用与 Phase 16 相同的模式：mini FastAPI app + dependency_overrides。
    """
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.products import router as products_router
    from app.api.system import router as system_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.user import User

    db_path = tmp_path / "test_phase17.sqlite3"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("ERP_SECRET_KEY", "phase17-secret")

    eng = create_sqlite_engine(db_path)
    Base.metadata.create_all(eng)
    # auth middleware が alembic_version を確認する場合に必要
    with sqlite3.connect(db_path) as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS alembic_version "
            "(version_num VARCHAR(32) NOT NULL)"
        )
        con.execute("INSERT INTO alembic_version VALUES ('j60d1a9b5e47')")
        con.commit()

    factory = sessionmaker(bind=eng, expire_on_commit=False)
    with factory() as s:
        s.add(User(
            username="admin",
            password_hash=hash_password("Admin123!"),
            role="admin",
            real_name="Admin",
            must_change_password=False,
        ))
        s.commit()

    mini_app = FastAPI()
    mini_app.include_router(auth_router, prefix="/api/auth")
    mini_app.include_router(system_router, prefix="/api/system")
    mini_app.include_router(products_router, prefix="/api/products")

    def _override_db() -> Generator[Session, None, None]:
        with factory() as s:
            yield s

    mini_app.dependency_overrides[get_db] = _override_db
    return mini_app, db_path, factory


def _login(client: TestClient) -> None:
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "Admin123!"})
    assert resp.status_code == 200, f"login failed: {resp.text}"


# ─────────────────────────────── 版本 API 测试 ─────────────────────────────


def test_version_endpoint_no_auth(flute_api):
    """GET /api/system/version 不需要登录。"""
    app, _, _factory = flute_api
    with TestClient(app) as client:
        resp = client.get("/api/system/version")
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == "v0.17.0"
        assert "changelog" in data
        assert isinstance(data["changelog"], list)
        assert len(data["changelog"]) > 0


# ─────────────────────────────── 楞型预览/写入 API 测试 ─────────────────────


def _insert_test_products(factory) -> None:
    """使用 ORM 插入若干测试产品。"""
    from app.models.customer import Customer
    from app.models.product import Product

    with factory() as s:
        # 确保有客户
        cust = s.query(Customer).first()
        if cust is None:
            cust = Customer(
                name="TestCo",
                payment_term_days=30,
                credit_limit=0,
                delivery_method="自提",
                default_tax_rate=0,
                status="active",
                is_active=True,
            )
            s.add(cust)
            s.flush()

        # 各产品的 legacy_material_text 场景
        products_data = [
            ("P001", "C001", "ProductA", "120/160/160/160/120"),   # → AB, 5
            ("P002", "C002", "ProductB", "160/160/120"),            # → A, 3
            ("P003", "C003", "ProductC", "120/160/120/E"),          # → E, 3
            ("P004", "C004", "ProductD", "no-material-text"),       # → unrecognized
        ]
        for pcode, ccode, pname, mat_text in products_data:
            existing = s.query(Product).filter_by(product_code=pcode, customer_id=cust.id).first()
            if existing is None:
                s.add(Product(
                    customer_id=cust.id,
                    product_code=pcode,
                    customer_material_code=ccode,
                    product_name=pname,
                    legacy_material_text=mat_text,
                    box_category="normal",
                ))
        s.commit()


def test_flute_mapping_preview(flute_api):
    """GET /api/system/flute-mapping/preview 返回正确预览。"""
    app, db_path, factory = flute_api
    _insert_test_products(factory)
    with TestClient(app) as client:
        _login(client)
        resp = client.get("/api/system/flute-mapping/preview")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_products" in data
        assert "will_update" in data
        assert "already_set" in data
        assert "unrecognized" in data
        # 我们插入了 4 个产品：3 可识别，1 无法识别
        assert data["will_update"] >= 3
        assert data["unrecognized"] >= 1


def test_flute_mapping_apply(flute_api):
    """POST /api/system/flute-mapping/apply 写入楞型，并验证结果。"""
    app, db_path, factory = flute_api
    _insert_test_products(factory)
    with TestClient(app) as client:
        _login(client)
        resp = client.post("/api/system/flute-mapping/apply")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["updated"] >= 3

    # 验证数据库中的写入结果
    with factory() as s:
        from app.models.product import Product
        rows = s.query(Product).filter(Product.is_active.is_(True)).all()
        flute_map = {p.product_code: (p.flute_type, p.layer_count) for p in rows}
    assert flute_map.get("P001") == ("AB", 5), f"P001: {flute_map.get('P001')}"
    assert flute_map.get("P002") == ("A", 3), f"P002: {flute_map.get('P002')}"
    assert flute_map.get("P003") == ("E", 3), f"P003: {flute_map.get('P003')}"
    # P004 无法识别，应保持 None
    assert flute_map.get("P004") == (None, None), f"P004: {flute_map.get('P004')}"


def test_flute_mapping_apply_no_overwrite(flute_api):
    """已有 flute_type 的产品不会被覆盖。"""
    app, db_path, factory = flute_api
    _insert_test_products(factory)  # 确保产品存在
    # 手动将 P001 预设为 E/3
    with factory() as s:
        from app.models.product import Product
        p = s.query(Product).filter_by(product_code="P001").first()
        if p:
            p.flute_type = "E"
            p.layer_count = 3
            s.commit()

    with TestClient(app) as client:
        _login(client)
        resp = client.post("/api/system/flute-mapping/apply")
        assert resp.status_code == 200

    with factory() as s:
        from app.models.product import Product
        p = s.query(Product).filter_by(product_code="P001").first()
        # 应仍为 E/3，不被覆盖为 AB/5
        assert p is not None and p.flute_type == "E", f"Expected E, got {p.flute_type if p else 'None'}"
        assert p.layer_count == 3


def test_flute_mapping_requires_admin(flute_api):
    """楞型写入端点要求 admin 权限。"""
    app, _, _factory = flute_api
    with TestClient(app) as client:
        # 未登录
        resp = client.post("/api/system/flute-mapping/apply")
        assert resp.status_code in (401, 403)


# ─────────────────────────────── 产品 API 含楞型字段 ──────────────────────


def test_product_create_with_flute(flute_api):
    """POST /api/products 支持 flute_type / layer_count 字段。"""
    app, db_path, factory = flute_api
    # 先确保有客户
    _insert_test_products(factory)
    with factory() as s:
        from app.models.customer import Customer
        cust = s.query(Customer).first()
        cust_id = cust.id

    with TestClient(app) as client:
        _login(client)
        payload = {
            "customer_id": cust_id,
            "product_code": "PTEST_FLUTE",
            "customer_material_code": "CMTEST",
            "product_name": "FluteTestProduct",
            "box_category": "normal",
            "flute_type": "AB",
            "layer_count": 5,
        }
        resp = client.post("/api/products", json=payload)
        assert resp.status_code in (200, 201), resp.text
        data = resp.json()
        assert data["flute_type"] == "AB"
        assert data["layer_count"] == 5
