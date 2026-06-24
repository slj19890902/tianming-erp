"""tests/test_phase16_material_mapping.py

Phase 16: 材质代码映射 & 客户料号批量规范 单元 + 集成测试。

运行命令:
    python -X utf8 -m pytest tests/test_phase16_material_mapping.py -q
"""
from __future__ import annotations

import csv
import io
import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker


# ─────────────────────────────────────────────────────────────
# 1. 可信度分级 (classify_confidence)
# ─────────────────────────────────────────────────────────────

class TestClassifyConfidence:
    """12 个场景覆盖所有分支。"""

    def _c(self, label, old="A100", new="B200", conflict=False):
        from app.services.material_mapping import classify_confidence
        return classify_confidence(label, old, new, conflict)

    def test_empty_old_code_is_low(self):
        assert self._c("可直接替换", old="", new="B200") == "低可信"

    def test_empty_new_code_is_low(self):
        assert self._c("可直接替换", new="") == "低可信"

    def test_conflict_flag_is_low(self):
        assert self._c("可直接替换", conflict=True) == "低可信"

    def test_wufa_jiemar_is_low(self):
        assert self._c("无法解码") == "低可信"

    def test_xu_rengong_is_low(self):
        assert self._c("需人工验证") == "低可信"

    def test_7_layer_is_low(self):
        assert self._c("7层板近似(差0g)") == "低可信"

    def test_diff_30g_is_low(self):
        assert self._c("近似(差30g)") == "低可信"

    def test_diff_40g_is_low(self):
        assert self._c("近似(差40g)") == "低可信"

    def test_ke_zhijie_tihuan_is_high(self):
        assert self._c("可直接替换") == "高可信"

    def test_diff_0g_is_high(self):
        assert self._c("近似(差0g)") == "高可信"

    def test_diff_5g_no_tuiding_is_high(self):
        assert self._c("近似(差5g)") == "高可信"

    def test_diff_5g_with_tuiding_is_mid(self):
        assert self._c("近似(差5g)纸种克重为推定") == "中可信"

    def test_diff_10g_is_mid(self):
        assert self._c("近似(差10g)") == "中可信"

    def test_diff_25g_is_mid(self):
        assert self._c("近似(差25g)") == "中可信"

    def test_kezhong_tuisuan_is_mid(self):
        assert self._c("克重推算") == "中可信"

    def test_zhongzhong_tuiding_is_mid(self):
        assert self._c("纸种克重为推定") == "中可信"


# ─────────────────────────────────────────────────────────────
# 2. is_strict_code
# ─────────────────────────────────────────────────────────────

class TestIsStrictCode:
    def _ok(self, s):
        from app.services.material_mapping import is_strict_code
        assert is_strict_code(s) is True, f"{s!r} 应当通过"

    def _fail(self, s):
        from app.services.material_mapping import is_strict_code
        assert is_strict_code(s) is False, f"{s!r} 应当被拒绝"

    def test_pure_5digit(self):       self._ok("21311")
    def test_pure_8digit(self):       self._ok("21311095")
    def test_alpha_digit(self):       self._ok("A113B")
    def test_alpha_digit_lower(self): self._ok("wcx1")
    def test_mix_K617K(self):         self._ok("K617K")

    def test_chinese_start(self):     self._fail("纸箱A100")
    def test_product_keyword(self):   self._fail("A100纸箱")
    def test_forbidden_box(self):     self._fail("box")
    def test_forbidden_cartonbox(self): self._fail("CartonBox")
    def test_pure_4digit(self):       self._fail("1234")
    def test_pure_alpha(self):        self._fail("ABCDE")
    def test_too_long(self):          self._fail("A" * 31)
    def test_empty(self):             self._fail("")
    def test_single_char(self):       self._fail("A")


# ─────────────────────────────────────────────────────────────
# 3. extract_customer_code
# ─────────────────────────────────────────────────────────────

class TestExtractCustomerCode:
    def _ex(self, name):
        from app.services.material_mapping import extract_customer_code
        return extract_customer_code(name)

    def test_slash_rule_valid(self):
        result = self._ex("A1234/外箱")
        assert result is not None
        code, rule = result
        assert code == "A1234"
        assert rule == "slash"

    def test_space_rule_valid(self):
        result = self._ex("21311095 外箱")
        assert result is not None
        code, rule = result
        assert code == "21311095"
        assert rule == "space"

    def test_slash_rejects_cartonbox(self):
        assert self._ex("CartonBox/外箱") is None

    def test_slash_rejects_chinese_start(self):
        assert self._ex("纸箱A/外箱") is None

    def test_no_separator(self):
        assert self._ex("21311095外箱") is None

    def test_slash_rejects_pure_3digit(self):
        assert self._ex("123/外箱") is None

    def test_empty_name(self):
        assert self._ex("") is None

    def test_already_pure_code(self):
        # 没有斜杠也没有空格 → None
        assert self._ex("A1234") is None


# ─────────────────────────────────────────────────────────────
# 共用 fixture
# ─────────────────────────────────────────────────────────────

@pytest.fixture()
def mapping_db(tmp_path: Path):
    """创建内存 SQLite + 所有表的 Session。"""
    from app.core.database import create_sqlite_engine
    from app.models import Base

    engine = create_sqlite_engine(tmp_path / "test_mapping.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session


def _make_csv(rows: list[dict], path: Path) -> Path:
    """根据 rows 写 CSV 文件，返回路径。"""
    if not rows:
        path.write_text("", encoding="utf-8")
        return path
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    return path


# ─────────────────────────────────────────────────────────────
# 4. CSV 导入 (import_csv_to_candidates)
# ─────────────────────────────────────────────────────────────

class TestImportCsvToCandidates:
    def test_import_high_confidence(self, mapping_db: Session, tmp_path: Path):
        from app.services.material_mapping import import_csv_to_candidates

        csv_path = _make_csv(
            [
                {
                    "ERP原代码": "K515R",
                    "嘉林亿新代码": "D414B",
                    "识别供应商": "鸣朋",
                    "层数": "5层",
                    "逐层克重": "牛120/瓦150/瓦120/瓦150/牛120",
                    "嘉林亿价": "1.85",
                    "原供应商价": "1.72",
                    "可信度": "可直接替换",
                }
            ],
            tmp_path / "test.csv",
        )
        stats = import_csv_to_candidates(mapping_db, csv_path, "test.csv")
        mapping_db.commit()
        assert stats.total == 1
        assert stats.high == 1
        assert stats.mid == 0
        assert stats.low == 0

    def test_import_mid_confidence(self, mapping_db: Session, tmp_path: Path):
        from app.services.material_mapping import import_csv_to_candidates

        csv_path = _make_csv(
            [
                {
                    "ERP原代码": "K515R",
                    "嘉林亿新代码": "D414B",
                    "识别供应商": "鸣朋",
                    "层数": "5层",
                    "逐层克重": "牛120/瓦150",
                    "嘉林亿价": "1.85",
                    "原供应商价": "1.72",
                    "可信度": "近似(差10g)",
                }
            ],
            tmp_path / "test.csv",
        )
        stats = import_csv_to_candidates(mapping_db, csv_path, "test.csv")
        mapping_db.commit()
        assert stats.mid == 1

    def test_conflict_detection(self, mapping_db: Session, tmp_path: Path):
        from app.services.material_mapping import import_csv_to_candidates

        csv_path = _make_csv(
            [
                {
                    "ERP原代码": "K515R",
                    "嘉林亿新代码": "D414B",
                    "识别供应商": "鸣朋",
                    "层数": "5层",
                    "逐层克重": "",
                    "嘉林亿价": "",
                    "原供应商价": "",
                    "可信度": "可直接替换",
                },
                {
                    "ERP原代码": "K515R",
                    "嘉林亿新代码": "D414C",  # 同旧代码，不同新代码 → 冲突
                    "识别供应商": "鸣朋",
                    "层数": "5层",
                    "逐层克重": "",
                    "嘉林亿价": "",
                    "原供应商价": "",
                    "可信度": "可直接替换",
                },
            ],
            tmp_path / "test.csv",
        )
        stats = import_csv_to_candidates(mapping_db, csv_path, "test.csv")
        mapping_db.commit()
        assert stats.conflicts == 2
        # 冲突 → 低可信
        assert stats.low == 2
        assert stats.high == 0

    def test_idempotent_reimport(self, mapping_db: Session, tmp_path: Path):
        from app.models.material_mapping import MaterialCodeMappingCandidate
        from app.services.material_mapping import import_csv_to_candidates
        from sqlalchemy import select, func

        csv_path = _make_csv(
            [
                {
                    "ERP原代码": "K515R",
                    "嘉林亿新代码": "D414B",
                    "识别供应商": "鸣朋",
                    "层数": "5层",
                    "逐层克重": "",
                    "嘉林亿价": "1.85",
                    "原供应商价": "1.72",
                    "可信度": "可直接替换",
                }
            ],
            tmp_path / "test.csv",
        )
        import_csv_to_candidates(mapping_db, csv_path, "test.csv")
        mapping_db.commit()
        import_csv_to_candidates(mapping_db, csv_path, "test.csv")
        mapping_db.commit()
        count = mapping_db.scalar(
            select(func.count()).select_from(MaterialCodeMappingCandidate)
        )
        assert count == 1  # 幂等，不重复

    def test_empty_old_code_counted(self, mapping_db: Session, tmp_path: Path):
        from app.services.material_mapping import import_csv_to_candidates

        csv_path = _make_csv(
            [
                {
                    "ERP原代码": "",
                    "嘉林亿新代码": "D414B",
                    "识别供应商": "",
                    "层数": "",
                    "逐层克重": "",
                    "嘉林亿价": "",
                    "原供应商价": "",
                    "可信度": "",
                }
            ],
            tmp_path / "test.csv",
        )
        stats = import_csv_to_candidates(mapping_db, csv_path, "test.csv")
        assert stats.empty_old == 1
        # 无旧代码 → 低可信
        assert stats.low == 1


# ─────────────────────────────────────────────────────────────
# 5. 客户料号批量规范
# ─────────────────────────────────────────────────────────────

class TestCustomerCodeUpdates:
    def _seed_products(self, db: Session):
        """在测试 DB 中插入几个产品。"""
        from app.models.customer import Customer
        from app.models.product import Product

        cust = Customer(
            name="测试客户",
            payment_term_days=30,
            credit_limit=0,
            delivery_method="自提",
            default_tax_rate=0,
            status="active",
            is_active=True,
        )
        db.add(cust)
        db.flush()

        products = [
            Product(
                customer_id=cust.id,
                product_code="PC001",
                customer_material_code="PC001",
                product_name="A1234B/外箱",
            ),
            Product(
                customer_id=cust.id,
                product_code="PC002",
                customer_material_code="PC002",
                product_name="21311095 外箱",
            ),
            Product(
                customer_id=cust.id,
                product_code="PC003",
                customer_material_code="PC003",
                product_name="纸箱外盒三层",  # 无法提取
            ),
            Product(
                customer_id=cust.id,
                product_code="PC004",
                customer_material_code="PC004",
                product_name="CartonBox/外箱",  # forbidden
            ),
        ]
        db.add_all(products)
        db.flush()
        return products

    def test_preview_counts(self, mapping_db: Session):
        from app.services.material_mapping import preview_customer_code_updates

        self._seed_products(mapping_db)
        mapping_db.commit()

        preview = preview_customer_code_updates(mapping_db)
        assert preview.total == 4
        assert preview.will_update == 2  # A1234B, 21311095
        assert preview.skipped == 2

    def test_apply_updates_and_saves_legacy(self, mapping_db: Session):
        from app.models.product import Product
        from app.services.material_mapping import apply_customer_code_updates
        from sqlalchemy import select

        self._seed_products(mapping_db)
        mapping_db.commit()

        result = apply_customer_code_updates(mapping_db)
        mapping_db.commit()

        assert result.updated == 2
        # 旧 customer_material_code 已保存到 legacy_customer_material_code
        p1 = mapping_db.scalar(
            select(Product).where(Product.product_code == "PC001")
        )
        assert p1 is not None
        assert p1.customer_material_code == "A1234B"
        assert p1.legacy_customer_material_code == "PC001"

        p2 = mapping_db.scalar(
            select(Product).where(Product.product_code == "PC002")
        )
        assert p2 is not None
        assert p2.customer_material_code == "21311095"
        assert p2.legacy_customer_material_code == "PC002"

    def test_apply_skips_unique_conflict(self, mapping_db: Session):
        """若提取值与同客户其他产品的 customer_material_code 冲突则跳过。"""
        from app.models.customer import Customer
        from app.models.product import Product
        from app.services.material_mapping import apply_customer_code_updates

        cust = Customer(
            name="冲突客户",
            payment_term_days=30,
            credit_limit=0,
            delivery_method="自提",
            default_tax_rate=0,
            status="active",
            is_active=True,
        )
        mapping_db.add(cust)
        mapping_db.flush()

        # PC010 的品名提取结果 A9999 与 PC011 的 customer_material_code 相同 → 冲突
        mapping_db.add_all([
            Product(
                customer_id=cust.id,
                product_code="PC010",
                customer_material_code="PC010",
                product_name="A9999/外箱",
            ),
            Product(
                customer_id=cust.id,
                product_code="PC011",
                customer_material_code="A9999",  # 已有
                product_name="something else",
            ),
        ])
        mapping_db.commit()

        result = apply_customer_code_updates(mapping_db)
        mapping_db.commit()

        # PC010 应被跳过
        assert result.updated == 0

    def test_apply_does_not_overwrite_existing_legacy(self, mapping_db: Session):
        """legacy_customer_material_code 已有值时不覆盖。"""
        from app.models.customer import Customer
        from app.models.product import Product
        from app.services.material_mapping import apply_customer_code_updates
        from sqlalchemy import select

        cust = Customer(
            name="保留客户",
            payment_term_days=30,
            credit_limit=0,
            delivery_method="自提",
            default_tax_rate=0,
            status="active",
            is_active=True,
        )
        mapping_db.add(cust)
        mapping_db.flush()

        p = Product(
            customer_id=cust.id,
            product_code="PC020",
            customer_material_code="OLD_CODE",
            product_name="A7777/外箱",
            legacy_customer_material_code="ORIGINAL_LEGACY",  # 已有
        )
        mapping_db.add(p)
        mapping_db.commit()

        apply_customer_code_updates(mapping_db)
        mapping_db.commit()

        p_ref = mapping_db.scalar(select(Product).where(Product.product_code == "PC020"))
        assert p_ref is not None
        assert p_ref.legacy_customer_material_code == "ORIGINAL_LEGACY"  # 不覆盖


# ─────────────────────────────────────────────────────────────
# 6. 高可信材质映射写入
# ─────────────────────────────────────────────────────────────

class TestHighConfidenceMaterialMapping:
    def _seed(self, db: Session, tmp_path: Path):
        """插入一个 legacy_material_text 指向旧代码的产品，并创建候选表记录。"""
        from app.models.customer import Customer
        from app.models.material_mapping import MaterialCodeMappingCandidate
        from app.models.product import Product

        cust = Customer(
            name="材质客户",
            payment_term_days=30,
            credit_limit=0,
            delivery_method="自提",
            default_tax_rate=0,
            status="active",
            is_active=True,
        )
        db.add(cust)
        db.flush()

        p = Product(
            customer_id=cust.id,
            product_code="MP001",
            customer_material_code="MP001",
            product_name="产品A",
            legacy_material_text="010 K515R",  # → 旧代码 K515R
            material_id=None,
        )
        db.add(p)

        cand = MaterialCodeMappingCandidate(
            old_code="K515R",
            new_code="D414B",
            new_supplier="嘉林亿",
            old_supplier="鸣朋",
            layer_count="5层",
            weight_structure="牛120/瓦150/瓦120/瓦150/牛120",
            confidence_label="可直接替换",
            confidence_level="高可信",
            review_status="pending",
        )
        db.add(cand)
        db.commit()

        csv_path = _make_csv([], tmp_path / "dummy.csv")
        return p, cand, csv_path

    def test_applies_high_confidence(self, mapping_db: Session, tmp_path: Path):
        from app.models.product import Product
        from app.services.material_mapping import apply_high_confidence_material_mapping
        from sqlalchemy import select

        p, _cand, csv_path = self._seed(mapping_db, tmp_path)
        result = apply_high_confidence_material_mapping(mapping_db, csv_path)
        mapping_db.commit()

        assert result.products_updated == 1
        assert result.materials_created == 1

        p_ref = mapping_db.scalar(select(Product).where(Product.id == p.id))
        assert p_ref is not None
        assert p_ref.material_id is not None

    def test_skips_mid_confidence(self, mapping_db: Session, tmp_path: Path):
        from app.models.customer import Customer
        from app.models.material_mapping import MaterialCodeMappingCandidate
        from app.models.product import Product
        from app.services.material_mapping import apply_high_confidence_material_mapping

        cust = Customer(
            name="中可信客户",
            payment_term_days=30,
            credit_limit=0,
            delivery_method="自提",
            default_tax_rate=0,
            status="active",
            is_active=True,
        )
        mapping_db.add(cust)
        mapping_db.flush()

        p = Product(
            customer_id=cust.id,
            product_code="MID001",
            customer_material_code="MID001",
            product_name="产品B",
            legacy_material_text="020 K600M",
            material_id=None,
        )
        mapping_db.add(p)

        mapping_db.add(MaterialCodeMappingCandidate(
            old_code="K600M",
            new_code="D500B",
            confidence_label="近似(差10g)",
            confidence_level="中可信",
            review_status="pending",
        ))
        mapping_db.commit()

        csv_path = _make_csv([], tmp_path / "dummy.csv")
        result = apply_high_confidence_material_mapping(mapping_db, csv_path)
        mapping_db.commit()

        assert result.products_updated == 0  # 中可信不写入

    def test_skips_already_set_material(self, mapping_db: Session, tmp_path: Path):
        from app.models.customer import Customer
        from app.models.material import Material
        from app.models.material_mapping import MaterialCodeMappingCandidate
        from app.models.product import Product
        from app.services.material_mapping import apply_high_confidence_material_mapping

        cust = Customer(
            name="已设材质客户",
            payment_term_days=30,
            credit_limit=0,
            delivery_method="自提",
            default_tax_rate=0,
            status="active",
            is_active=True,
        )
        mapping_db.add(cust)
        mat = Material(code="EXISTING_MAT", flute_type="B")
        mapping_db.add(mat)
        mapping_db.flush()

        p = Product(
            customer_id=cust.id,
            product_code="SET001",
            customer_material_code="SET001",
            product_name="产品C",
            legacy_material_text="030 K800X",
            material_id=mat.id,  # 已设置
        )
        mapping_db.add(p)

        mapping_db.add(MaterialCodeMappingCandidate(
            old_code="K800X",
            new_code="D800Z",
            confidence_label="可直接替换",
            confidence_level="高可信",
            review_status="pending",
        ))
        mapping_db.commit()

        csv_path = _make_csv([], tmp_path / "dummy.csv")
        result = apply_high_confidence_material_mapping(mapping_db, csv_path)
        mapping_db.commit()

        assert result.products_updated == 0  # 已有 material_id 不覆盖
        assert result.skipped_already_set == 0  # 过滤在 query 层完成

    def test_legacy_material_text_not_modified(self, mapping_db: Session, tmp_path: Path):
        """apply_high_confidence_material_mapping 不得修改 legacy_material_text。"""
        from app.models.product import Product
        from app.services.material_mapping import apply_high_confidence_material_mapping
        from sqlalchemy import select

        p, _cand, csv_path = self._seed(mapping_db, tmp_path)
        original_legacy = p.legacy_material_text

        apply_high_confidence_material_mapping(mapping_db, csv_path)
        mapping_db.commit()

        p_ref = mapping_db.scalar(select(Product).where(Product.id == p.id))
        assert p_ref is not None
        assert p_ref.legacy_material_text == original_legacy  # 未被修改


# ─────────────────────────────────────────────────────────────
# 7. API 端点集成测试
# ─────────────────────────────────────────────────────────────

@pytest.fixture()
def mapping_api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.system import router as system_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.user import User

    db_path = tmp_path / "api_test.sqlite3"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("ERP_SECRET_KEY", "phase16-secret")

    eng = create_sqlite_engine(db_path)
    Base.metadata.create_all(eng)
    # alembic_version テーブルが必要（auth router が確認する場合）
    with sqlite3.connect(db_path) as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS alembic_version "
            "(version_num VARCHAR(32) NOT NULL)"
        )
        con.execute("INSERT INTO alembic_version VALUES ('i59f0a8b4c35')")
        con.commit()
    factory = sessionmaker(bind=eng, expire_on_commit=False)

    with factory() as s:
        s.add(User(
            username="admin",
            password_hash=hash_password("Admin123!"),
            role="admin",
            real_name="管理员",
            must_change_password=False,
        ))
        s.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(system_router, prefix="/api/system")

    def _override_db() -> Generator[Session, None, None]:
        with factory() as s:
            yield s

    app.dependency_overrides[get_db] = _override_db
    return app, db_path, factory


def _login(client: TestClient, username: str, password: str) -> None:
    """Cookie-based login (auth router uses session cookies, not JWT)."""
    resp = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert resp.status_code == 200, f"login failed: {resp.text}"


class TestMaterialMappingApi:
    def test_stats_empty(self, mapping_api):
        app, _db_path, _factory = mapping_api
        with TestClient(app) as client:
            _login(client, "admin", "Admin123!")
            resp = client.get("/api/system/material-mapping/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0

    def test_import_csv_not_found(self, mapping_api):
        app, _db_path, _factory = mapping_api
        with TestClient(app) as client:
            _login(client, "admin", "Admin123!")
            resp = client.post("/api/system/material-mapping/import-csv")
        assert resp.status_code == 404

    def test_import_csv_and_list(self, mapping_api, tmp_path: Path):
        """写一个真实 CSV 到 data 目录旁边，然后通过 API 导入并列出。"""
        app, db_path, _factory = mapping_api

        # 把 CSV 放在 DB 同目录
        csv_path = db_path.parent / "material_code_mapping.csv"
        _make_csv(
            [
                {
                    "ERP原代码": "K515R",
                    "嘉林亿新代码": "D414B",
                    "识别供应商": "鸣朋",
                    "层数": "5层",
                    "逐层克重": "牛120/瓦150",
                    "嘉林亿价": "1.85",
                    "原供应商价": "1.72",
                    "可信度": "可直接替换",
                }
            ],
            csv_path,
        )

        with TestClient(app) as client:
            _login(client, "admin", "Admin123!")
            resp = client.post("/api/system/material-mapping/import-csv")
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True
            assert data["total"] == 1
            assert data["high"] == 1

            # 列出
            resp2 = client.get("/api/system/material-mapping")
            assert resp2.status_code == 200
            items = resp2.json()["items"]
            assert len(items) == 1
            assert items[0]["old_code"] == "K515R"
            assert items[0]["review_status"] == "pending"

    def test_update_status(self, mapping_api, tmp_path: Path):
        app, db_path, _factory = mapping_api

        csv_path = db_path.parent / "material_code_mapping.csv"
        _make_csv(
            [
                {
                    "ERP原代码": "K515R",
                    "嘉林亿新代码": "D414B",
                    "识别供应商": "鸣朋",
                    "层数": "5层",
                    "逐层克重": "",
                    "嘉林亿价": "1.85",
                    "原供应商价": "1.72",
                    "可信度": "可直接替换",
                }
            ],
            csv_path,
        )

        with TestClient(app) as client:
            _login(client, "admin", "Admin123!")
            client.post("/api/system/material-mapping/import-csv")
            list_resp = client.get("/api/system/material-mapping")
            item_id = list_resp.json()["items"][0]["id"]

            upd = client.put(
                f"/api/system/material-mapping/{item_id}/status",
                json={"review_status": "approved", "review_note": "人工确认"},
            )
            assert upd.status_code == 200
            assert upd.json()["review_status"] == "approved"

    def test_update_status_invalid(self, mapping_api, tmp_path: Path):
        app, db_path, _factory = mapping_api

        csv_path = db_path.parent / "material_code_mapping.csv"
        _make_csv(
            [{"ERP原代码": "X", "嘉林亿新代码": "Y", "识别供应商": "", "层数": "",
              "逐层克重": "", "嘉林亿价": "", "原供应商价": "", "可信度": ""}],
            csv_path,
        )

        with TestClient(app) as client:
            _login(client, "admin", "Admin123!")
            client.post("/api/system/material-mapping/import-csv")
            list_resp = client.get("/api/system/material-mapping")
            item_id = list_resp.json()["items"][0]["id"]

            upd = client.put(
                f"/api/system/material-mapping/{item_id}/status",
                json={"review_status": "invalid_status"},
            )
            assert upd.status_code == 400

    def test_preview_customer_codes(self, mapping_api):
        app, _db_path, _factory = mapping_api
        with TestClient(app) as client:
            _login(client, "admin", "Admin123!")
            resp = client.get("/api/system/material-mapping/preview-customer-codes")
        assert resp.status_code == 200
        data = resp.json()
        assert "will_update" in data

    def test_non_admin_rejected(self, mapping_api):
        """非管理员访问应被拒绝（403）。"""
        app, _db_path, factory = mapping_api
        from app.core.security import hash_password
        from app.models.user import User

        with factory() as s:
            s.add(User(
                username="finance",
                password_hash=hash_password("Finance123!"),
                role="finance",
                real_name="财务",
                must_change_password=False,
            ))
            s.commit()

        with TestClient(app) as client:
            _login(client, "finance", "Finance123!")
            resp = client.get("/api/system/material-mapping/stats")
        assert resp.status_code == 403
