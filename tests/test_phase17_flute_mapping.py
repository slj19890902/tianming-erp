"""
Phase 17 / v0.17.1 Tests: 楞型识别服务 + 一致性校验 + API 端点。

核心业务规则：
  三层（单楞）：只能 A / B / E
  五层（双楞）：只能 AB / BE

测试覆盖：
1. parse_flute_from_text — 基础场景（含修复后的 /A 后缀 = 三层）
2. parse_flute_from_text — 歧义检测 (B/E、A4B-B/E)
3. parse_flute_from_text — 面纸类型检测 (白/W)
4. validate_flute_consistency — 合法/非法组合校验
5. apply_flute_consistency_fix — 历史错误修复
6. GET /api/system/version
7. GET /api/system/flute-mapping/preview
8. POST /api/system/flute-mapping/apply
9. POST /api/system/flute-mapping/fix-consistency
10. POST /api/products — 携带 flute_type/layer_count，非法组合被拒绝
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

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")


# ═══════════════════════════════════════════════════════════════════════════
# 1. parse_flute_from_text 单元测试
# ═══════════════════════════════════════════════════════════════════════════

from app.services.flute_mapping import parse_flute_from_text, validate_flute_consistency


@pytest.mark.parametrize("text,expected_flute,expected_layer,expected_src", [
    # 五层场景
    ("150/105/50/125/130",       "AB", 5, "weight_count"),  # 用户指定 case 1
    ("120/160/160/160/120",      "AB", 5, "weight_count"),  # 5 段克重
    # 含白面纸的五层
    ("130/85/50/105/130",        "AB", 5, "weight_count"),  # 用户 case 2（无白字, 5段→AB）
    ("W/160/160/160/120",        "AB", 5, "weight_count"),  # W 面纸 + 4 克重 = 5 层
    # 三层场景
    ("105/105/80",               "A",  3, "weight_count"),  # 用户 case 3：3段→A
    ("160/160/120",              "A",  3, "weight_count"),  # 3 段克重
    # 后缀覆盖：/A /B /E = 三层单楞（修复前错误返回五层）
    ("120/160/120/A",            "A",  3, "suffix"),        # /A 后缀 → 三层 A（修复）
    ("120/160/120/B",            "B",  3, "suffix"),        # /B 后缀 → 三层 B
    ("120/160/120/E",            "E",  3, "suffix"),        # /E 后缀 → 三层 E
    ("130/115/95/E",             "E",  3, "suffix"),        # 用户 case 4（简化白面纸版）
    # 含英文数字的后缀不影响
    ("105/105/80/B",             "B",  3, "suffix"),        # 用户 case 5 简化版
    # 无法识别
    ("无材质",                   None, None, "unrecognized"),
    ("",                         None, None, "unrecognized"),
])
def test_parse_flute_from_text_basic(text, expected_flute, expected_layer, expected_src):
    result = parse_flute_from_text(text)
    assert result.flute_type == expected_flute, (
        f"text={text!r}: flute={result.flute_type!r}, expected={expected_flute!r} (src={result.source})"
    )
    assert result.layer_count == expected_layer, (
        f"text={text!r}: layer={result.layer_count}, expected={expected_layer}"
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
    """后缀字母大小写不敏感。"""
    result = parse_flute_from_text("120/160/120/e")
    assert result.flute_type == "E"
    assert result.layer_count == 3
    assert result.source == "suffix"


# ─── 歧义检测 ───────────────────────────────────────────────────────────────

def test_ambiguous_be():
    """B/E 多候选 → ambiguous，不自动写楞型。"""
    result = parse_flute_from_text("B/E")
    assert result.flute_type is None, f"Expected None (ambiguous), got {result.flute_type!r}"
    assert result.source == "ambiguous"


def test_ambiguous_a4b_be():
    """A4B-B/E 多候选 → ambiguous。"""
    result = parse_flute_from_text("A4B-B/E")
    assert result.flute_type is None, f"Expected None, got {result.flute_type!r}"
    assert result.source == "ambiguous"


def test_ambiguous_a_slash_b():
    """A/B 多候选 → ambiguous。"""
    result = parse_flute_from_text("A/B")
    assert result.flute_type is None
    assert result.source == "ambiguous"


def test_not_ambiguous_80A_slash_B():
    """80A/B → B 后缀（A 前有数字，不是独立楞型候选）。"""
    result = parse_flute_from_text("105/105/80/B")
    assert result.flute_type == "B"
    assert result.layer_count == 3
    assert result.source == "suffix"


def test_suffix_with_chinese_grade():
    """105国产AA/105A/80A/B → B 后缀三层（A 是纸张等级，不是楞型候选）。"""
    result = parse_flute_from_text("105/105/80A/B")
    assert result.flute_type == "B"
    assert result.layer_count == 3
    assert result.source == "suffix"


# ─── 面纸类型检测 ─────────────────────────────────────────────────────────

def test_surface_paper_white_w():
    """W 面纸段 → surface_paper_type='white'。"""
    result = parse_flute_from_text("W/160/160/160/120")
    assert result.surface_paper_type == "white"
    assert result.flute_type == "AB"  # 5段


def test_surface_paper_bai():
    """含'白'的段 → surface_paper_type='white'。"""
    result = parse_flute_from_text("130白/115/95/E")
    assert result.surface_paper_type == "white"
    assert result.flute_type == "E"
    assert result.layer_count == 3


# ═══════════════════════════════════════════════════════════════════════════
# 2. validate_flute_consistency 校验
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("flute_type,layer_count,expect_error", [
    # 合法组合
    ("A",  3, False),
    ("B",  3, False),
    ("E",  3, False),
    ("AB", 5, False),
    ("BE", 5, False),
    ("AAA", 7, False),
    ("ABC", 7, False),
    # 非法：3层+双楞
    ("AB", 3, True),
    ("BE", 3, True),
    # 非法：5层+单楞
    ("A",  5, True),
    ("B",  5, True),
    ("E",  5, True),
    ("A",  7, True),
    ("AB", 7, True),
    # None 值不校验
    (None, 3, False),
    (None, 7, False),  # Historical empty flute values remain readable.
    ("A",  None, False),
    (None, None, False),
])
def test_validate_flute_consistency(flute_type, layer_count, expect_error):
    err = validate_flute_consistency(flute_type, layer_count)
    if expect_error:
        assert err is not None, f"Expected error for {flute_type}/{layer_count} but got None"
    else:
        assert err is None, f"Expected no error for {flute_type}/{layer_count} but got: {err}"


def test_parse_seven_layer_text_does_not_assign_default_flute():
    result = parse_flute_from_text("120/105/80/100/80/105/120")
    assert result.layer_count == 7
    assert result.flute_type is None
    assert result.source == "weight_count"


# ═══════════════════════════════════════════════════════════════════════════
# 集成测试 fixture
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def flute_api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
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
    resp = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "Admin123!"},
    )
    assert resp.status_code == 200, f"login failed: {resp.text}"


def _insert_test_products(factory) -> None:
    from app.models.customer import Customer
    from app.models.product import Product

    with factory() as s:
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

        products_data = [
            ("P001", "C001", "ProductA", "120/160/160/160/120"),   # → AB, 5
            ("P002", "C002", "ProductB", "160/160/120"),            # → A, 3
            ("P003", "C003", "ProductC", "120/160/120/E"),          # → E, 3 (fix: was E/3)
            ("P004", "C004", "ProductD", "no-material-text"),       # → unrecognized
            ("P005", "C005", "ProductE", "240/165/240/A"),          # → A, 3 (suffix /A = 3层)
        ]
        for pcode, ccode, pname, mat_text in products_data:
            existing = s.query(Product).filter_by(
                product_code=pcode, customer_id=cust.id
            ).first()
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


# ═══════════════════════════════════════════════════════════════════════════
# 3. 版本 API
# ═══════════════════════════════════════════════════════════════════════════

def test_version_endpoint_no_auth(flute_api):
    app, _, _f = flute_api
    with TestClient(app) as client:
        resp = client.get("/api/system/version")
        assert resp.status_code == 200
        data = resp.json()
        assert "version" in data
        assert isinstance(data["changelog"], list)
        assert len(data["changelog"]) > 0


# ═══════════════════════════════════════════════════════════════════════════
# 4. 楞型预览 / 写入 API
# ═══════════════════════════════════════════════════════════════════════════

def test_flute_mapping_preview(flute_api):
    app, _, factory = flute_api
    _insert_test_products(factory)
    with TestClient(app) as client:
        _login(client)
        resp = client.get("/api/system/flute-mapping/preview")
        assert resp.status_code == 200
        data = resp.json()
        assert data["will_update"] >= 3   # P001/P002/P003/P005 可识别
        assert data["unrecognized"] >= 1  # P004 不可识别


def test_flute_mapping_apply(flute_api):
    app, _, factory = flute_api
    _insert_test_products(factory)
    with TestClient(app) as client:
        _login(client)
        resp = client.post("/api/system/flute-mapping/apply")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["updated"] >= 3

    with factory() as s:
        from app.models.product import Product
        prods = {p.product_code: p for p in s.query(Product).all()}
    assert prods["P001"].flute_type == "AB" and prods["P001"].layer_count == 5
    assert prods["P002"].flute_type == "A"  and prods["P002"].layer_count == 3
    assert prods["P003"].flute_type == "E"  and prods["P003"].layer_count == 3
    # P005: 240/165/240/A → /A suffix → A, 3层（原 bug 会写 5 层，修复后应为 3）
    assert prods["P005"].flute_type == "A"  and prods["P005"].layer_count == 3, (
        f"P005: expected A/3, got {prods['P005'].flute_type}/{prods['P005'].layer_count}"
    )
    # P004: 无法识别
    assert prods["P004"].flute_type is None


def test_flute_mapping_apply_no_overwrite(flute_api):
    """已有 flute_type 的产品不覆盖。"""
    app, _, factory = flute_api
    _insert_test_products(factory)
    with factory() as s:
        from app.models.product import Product
        p = s.query(Product).filter_by(product_code="P001").first()
        if p:
            p.flute_type = "E"; p.layer_count = 3; s.commit()
    with TestClient(app) as client:
        _login(client)
        client.post("/api/system/flute-mapping/apply")
    with factory() as s:
        from app.models.product import Product
        p = s.query(Product).filter_by(product_code="P001").first()
        assert p.flute_type == "E"
        assert p.layer_count == 3


def test_flute_mapping_requires_admin(flute_api):
    app, _, _f = flute_api
    with TestClient(app) as client:
        resp = client.post("/api/system/flute-mapping/apply")
        assert resp.status_code in (401, 403)


# ═══════════════════════════════════════════════════════════════════════════
# 5. 一致性修复 API
# ═══════════════════════════════════════════════════════════════════════════

def test_flute_consistency_fix(flute_api):
    """修复 5层+A → 改层数为 3；3层+AB → 楞型置 null。"""
    app, _, factory = flute_api
    _insert_test_products(factory)

    # 人工制造非法数据
    with factory() as s:
        from app.models.product import Product
        p1 = s.query(Product).filter_by(product_code="P001").first()
        p2 = s.query(Product).filter_by(product_code="P002").first()
        if p1: p1.flute_type = "A";  p1.layer_count = 5   # 非法: 5层+A
        if p2: p2.flute_type = "AB"; p2.layer_count = 3   # 非法: 3层+AB
        s.commit()

    with TestClient(app) as client:
        _login(client)
        resp = client.post("/api/system/flute-mapping/fix-consistency")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["fixed_5layer_to_3"] >= 1    # P001: layer 5→3
        assert data["fixed_3layer_to_null"] >= 1  # P002: AB→null

    with factory() as s:
        from app.models.product import Product
        p1 = s.query(Product).filter_by(product_code="P001").first()
        p2 = s.query(Product).filter_by(product_code="P002").first()
        assert p1.flute_type == "A" and p1.layer_count == 3, (
            f"P001 after fix: {p1.flute_type}/{p1.layer_count}"
        )
        assert p2.flute_type is None and p2.layer_count == 3, (
            f"P002 after fix: {p2.flute_type}/{p2.layer_count}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 6. 产品 API 楞型字段 + 校验
# ═══════════════════════════════════════════════════════════════════════════

def test_product_create_with_flute(flute_api):
    """POST /api/products 合法楞型/层数组合可以保存。"""
    app, _, factory = flute_api
    _insert_test_products(factory)
    with factory() as s:
        from app.models.customer import Customer
        cust_id = s.query(Customer).first().id

    with TestClient(app) as client:
        _login(client)
        payload = {
            "customer_id": cust_id,
            "product_code": "PFLUTE_AB",
            "customer_material_code": "CM_AB",
            "product_name": "FluteTestAB",
            "box_category": "normal",
            "flute_type": "AB",
            "layer_count": 5,
        }
        resp = client.post("/api/products", json=payload)
        assert resp.status_code in (200, 201), resp.text
        assert resp.json()["flute_type"] == "AB"
        assert resp.json()["layer_count"] == 5


def test_product_reject_3layer_ab(flute_api):
    """POST /api/products: 3层+AB 非法组合应被拒绝（422）。"""
    app, _, factory = flute_api
    _insert_test_products(factory)
    with factory() as s:
        from app.models.customer import Customer
        cust_id = s.query(Customer).first().id

    with TestClient(app) as client:
        _login(client)
        payload = {
            "customer_id": cust_id,
            "product_code": "P_ILLEGAL_1",
            "customer_material_code": "CM_ILL1",
            "product_name": "Illegal3LayerAB",
            "box_category": "normal",
            "flute_type": "AB",
            "layer_count": 3,           # 非法：3层 + AB
        }
        resp = client.post("/api/products", json=payload)
        assert resp.status_code == 422, (
            f"Expected 422 for 3-layer+AB, got {resp.status_code}: {resp.text[:200]}"
        )
        assert "三层瓦楞只能是 A / B / E" in resp.text


def test_product_reject_5layer_b(flute_api):
    """POST /api/products: 5层+B 非法组合应被拒绝（422）。"""
    app, _, factory = flute_api
    _insert_test_products(factory)
    with factory() as s:
        from app.models.customer import Customer
        cust_id = s.query(Customer).first().id

    with TestClient(app) as client:
        _login(client)
        payload = {
            "customer_id": cust_id,
            "product_code": "P_ILLEGAL_2",
            "customer_material_code": "CM_ILL2",
            "product_name": "Illegal5LayerB",
            "box_category": "normal",
            "flute_type": "B",
            "layer_count": 5,           # 非法：5层 + B
        }
        resp = client.post("/api/products", json=payload)
        assert resp.status_code == 422, (
            f"Expected 422 for 5-layer+B, got {resp.status_code}: {resp.text[:200]}"
        )
        assert "五层瓦楞只能是 AB / BE" in resp.text
