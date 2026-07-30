from __future__ import annotations

from datetime import date
from decimal import Decimal
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker


SUPPLIER = "苏州嘉林亿"


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed_rules(db):
    from app.models.material_price_history import MaterialPriceAdjustmentBatch
    from app.models.supplier_material_rule import (
        SupplierMaterialBasePrice,
        SupplierMaterialSubstitutionRule,
    )

    db.add_all(
        [
            SupplierMaterialBasePrice(
                supplier_name=SUPPLIER, material_code="C4C", layer_count=3,
                base_price=Decimal("1.21"), effective_date=date(2026, 4, 14),
                source="test",
            ),
            SupplierMaterialBasePrice(
                supplier_name=SUPPLIER, material_code="J414J", layer_count=5,
                base_price=Decimal("2.66"), effective_date=date(2026, 4, 14),
                source="test",
            ),
            SupplierMaterialSubstitutionRule(
                supplier_name=SUPPLIER, rule_type="corrugated_b",
                from_code="4", to_code="6", price_delta=Decimal("0.10"),
                effective_date=date(2026, 4, 14), source="test",
            ),
            SupplierMaterialSubstitutionRule(
                supplier_name=SUPPLIER, rule_type="corrugated_a",
                from_code="4", to_code="6", price_delta=Decimal("0.10"),
                effective_date=date(2026, 4, 14), source="test",
            ),
            MaterialPriceAdjustmentBatch(
                supplier_name=SUPPLIER, adjust_percent=Decimal("5"),
                effective_date=date(2026, 6, 26), affected_count=135,
                remark="统一涨价5%", operator="admin",
            ),
        ]
    )


def test_corrugated_structure_prices_and_save(tmp_path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.materials import router as materials_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.material import Material
    from app.models.supplier import Supplier
    from app.models.supplier_paper_code import SupplierPaperCode
    from app.models.supplier_flute_price_rule import SupplierFlutePriceRule
    from app.models.user import User
    from app.services.corrugated_material_pricing import estimate_material_price
    from app.services.supplier_master import normalize_supplier_identity

    engine = create_sqlite_engine(tmp_path / "rules.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add(
            User(
                username="rules-admin",
                password_hash=hash_password("RulesPass123!"),
                role="admin",
                real_name="规则管理员",
                must_change_password=False,
            )
        )
        db.add(
            Supplier(
                standard_name=SUPPLIER,
                normalized_name=normalize_supplier_identity(SUPPLIER),
                display_name="嘉林亿",
                sort_order=10,
                is_active=True,
                version=1,
            )
        )
        for code, weight, name in (
            ("C", 80, "国产A级牛卡"),
            ("6", 130, "国产A级施胶高瓦"),
            ("J", 190, "国产AA级牛卡"),
            ("1", 50, "国产普瓦"),
            ("A", 150, "国产A级牛卡"),
            ("4", 100, "国产A级施胶高瓦"),
            ("D", 130, "国产A级牛卡"),
        ):
            db.add(
                SupplierPaperCode(
                    supplier_name=SUPPLIER, code_char=code, paper_name=name,
                    gram_weight=weight, is_active=True,
                )
            )
        _seed_rules(db)
        db.add(
            Material(
                code="A416D-AB/EB",
                supplier_name=SUPPLIER,
                layer_count=5,
                flute_type="AB",
                quote_price=Decimal("2.27"),
                is_active=True,
            )
        )
        db.add(
            SupplierFlutePriceRule(
                supplier_name=SUPPLIER,
                layer_count=3,
                flute_type="A",
                price_delta=Decimal("0.04"),
                is_active=True,
            )
        )
        db.commit()
        c6c = estimate_material_price(
            db, supplier_name=SUPPLIER, material_code="C6C"
        )
        assert c6c["quotation_base_price"] == Decimal("1.31")
        assert c6c["current_suggested_price"] == Decimal("1.38")
        assert c6c["steps"][0]["role"] == "瓦楞纸"
        c6c_a = estimate_material_price(
            db, supplier_name=SUPPLIER, material_code="C6C",
            usage_flute_type="A",
        )
        assert c6c_a["quotation_base_price"] == Decimal("1.31")
        assert c6c_a["usage_base_price"] == Decimal("1.35")
        assert c6c_a["current_suggested_price"] == Decimal("1.42")
        j616j = estimate_material_price(
            db, supplier_name=SUPPLIER, material_code="J616J"
        )
        assert j616j["quotation_base_price"] == Decimal("2.86")
        assert j616j["current_suggested_price"] == Decimal("3.00")
        assert [row["role"] for row in j616j["steps"]] == [
            "B楞瓦纸", "A楞瓦纸"
        ]

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(materials_router, prefix="/api/master/materials")

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={"username": "rules-admin", "password": "RulesPass123!"},
        ).status_code == 200
        preview = client.post(
            "/api/master/materials/compose/preview",
            json={"supplier_name": SUPPLIER, "material_code": "C6C"},
        )
        assert preview.status_code == 200
        assert [row["role"] for row in preview.json()["layers"]] == [
            "面纸", "瓦楞纸", "里纸"
        ]
        assert preview.json()["quotation_base_price"] == "1.31"
        assert preview.json()["current_suggested_price"] == "1.38"
        saved = client.post(
            "/api/master/materials/compose/save",
            json={
                "supplier_name": SUPPLIER,
                "layer_count": 3,
                "material_code": "C6C",
                "parsed_supplier_name": SUPPLIER,
                "parsed_layer_count": 3,
                "parsed_material_code": "C6C",
                "price_source": "suggested",
            },
        )
        assert saved.status_code == 200
        assert saved.json()["material"]["flute_type"] is None
        assert saved.json()["material"]["rule_base_price"] == "1.3100"
        assert saved.json()["material"]["quote_price"] == "1.3800"
        duplicate = client.post(
            "/api/master/materials/compose/save",
            json={
                "supplier_name": SUPPLIER,
                "layer_count": 3,
                "material_code": "C6C",
                "parsed_supplier_name": SUPPLIER,
                "parsed_layer_count": 3,
                "parsed_material_code": "C6C",
                "price_source": "suggested",
            },
        )
        assert duplicate.status_code == 409
        assert "不能重复保存。楞型请在常用箱中选择" in duplicate.json()["detail"]
        normalized_duplicate = client.post(
            "/api/master/materials/compose/save",
            json={
                "supplier_name": SUPPLIER,
                "layer_count": 5,
                "material_code": "A416D",
                "quote_price": 2.27,
                "parsed_supplier_name": SUPPLIER,
                "parsed_layer_count": 5,
                "parsed_material_code": "A416D",
                "price_source": "manual",
            },
        )
        assert normalized_duplicate.status_code == 409
        assert "五层材质代码 A416D" in normalized_duplicate.json()["detail"]

        stale = client.post(
            "/api/master/materials/compose/save",
            json={
                "supplier_name": SUPPLIER,
                "layer_count": 5,
                "material_code": "J616J",
                "quote_price": 1.38,
                "parsed_supplier_name": SUPPLIER,
                "parsed_layer_count": 3,
                "parsed_material_code": "C6C",
                "price_source": "suggested",
            },
        )
        assert stale.status_code == 409
        assert "旧解析结果已失效" in stale.json()["detail"]
        current = client.post(
            "/api/master/materials/compose/preview",
            json={
                "supplier_name": SUPPLIER,
                "layer_count": 5,
                "material_code": "J616J",
            },
        )
        assert current.json()["current_suggested_price"] == "3.00"
        saved_j = client.post(
            "/api/master/materials/compose/save",
            json={
                "supplier_name": SUPPLIER,
                "layer_count": 5,
                "material_code": "J616J",
                "quote_price": 1.38,
                "parsed_supplier_name": SUPPLIER,
                "parsed_layer_count": 5,
                "parsed_material_code": "J616J",
                "price_source": "suggested",
            },
        )
        assert saved_j.status_code == 200
        assert saved_j.json()["material"]["quote_price"] == "3.0000"

    with factory() as db:
        from app.services.material_pricing import get_effective_material_price

        row = db.query(Material).filter(Material.code == "C6C").one()
        assert row.rule_base_price == Decimal("1.3100")
        assert row.quote_price == Decimal("1.3800")
        assert row.flute_type is None
        effective = get_effective_material_price(
            db, material=row, flute_type="A"
        )
        assert Decimal(str(effective["effective_price"])) == Decimal("1.42")


def test_import_script_dry_run_and_idempotent_apply(tmp_path):
    from app.core.database import create_sqlite_engine
    from app.models import Base

    database = tmp_path / "import.sqlite3"
    Base.metadata.create_all(create_sqlite_engine(database))
    report_dir = tmp_path / "reports"
    backup_dir = tmp_path / "backups"
    script = Path(__file__).resolve().parents[1] / "scripts" / "admin" / "import_jialinyi_material_rules.py"
    before = _hash(database)
    dry = subprocess.run(
        [
            sys.executable, "-X", "utf8", str(script), "--dry-run",
            "--database", str(database), "--report-dir", str(report_dir),
        ],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert dry.returncode == 0, dry.stderr
    assert _hash(database) == before
    assert "pending_confirmation" in dry.stdout

    apply = subprocess.run(
        [
            sys.executable, "-X", "utf8", str(script), "--apply",
            "--database", str(database), "--report-dir", str(report_dir),
            "--backup-dir", str(backup_dir),
        ],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert apply.returncode == 0, apply.stderr
    with sqlite3.connect(database) as connection:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "supplier_paper_codes",
                "supplier_material_base_prices",
                "supplier_material_substitution_rules",
                "supplier_material_rule_configs",
            )
        }
        assert counts == {
            "supplier_paper_codes": 18,
            "supplier_material_base_prices": 86,
            "supplier_material_substitution_rules": 19,
            "supplier_material_rule_configs": 7,
        }
        assert connection.execute("SELECT COUNT(*) FROM materials").fetchone()[0] == 0
        pending = connection.execute(
            "SELECT status,participates_in_pricing FROM supplier_material_rule_configs "
            "WHERE rule_key='short_length_split_surcharge'"
        ).fetchone()
        assert pending == ("pending_confirmation", 0)

    second = subprocess.run(
        [
            sys.executable, "-X", "utf8", str(script), "--apply",
            "--database", str(database), "--report-dir", str(report_dir),
            "--backup-dir", str(backup_dir),
        ],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert second.returncode == 0, second.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM supplier_material_base_prices"
        ).fetchone()[0] == 86


def test_requisition_minimum_dimension_warnings():
    from app.api.requisition import _supplier_dimension_warnings

    warnings = _supplier_dimension_warnings(SUPPLIER, 480, 260, "一开一")
    assert any("最小切长 500mm" in row for row in warnings)
    assert any("一开二后采购宽 = 520mm，满足最小切宽" in row for row in warnings)
    assert _supplier_dimension_warnings(SUPPLIER, 500, 520, "一开二") == []
    assert _supplier_dimension_warnings("其他供应商", 480, 260, "一开一") == []


def test_corrugated_rules_frontend_copy():
    html = (
        Path(__file__).resolve().parents[1] / "static" / "index.html"
    ).read_text(encoding="utf-8")
    assert "报价表基准价" in html
    assert "当前使用建议价" in html
    assert "材质规则维护" in html
    assert "新增/组合材质" in html
    assert "输入材质代码" in html
    assert "输入总克重" in html
    assert "分纸加价金额待确认，暂不参与自动报价" in html
    assert "warning-input" in html
    assert "当前解析结果已失效，请重新解析" in html
    assert "materialComposerCanSave" in html
    material_editor = html.split(
        'modal.type === \'material\'', 1
    )[1].split('modal.type === \'orderPdfImport\'', 1)[0]
    assert "<label>楞型" not in material_editor
    composer = html.split(
        'v-if="showMaterialComposer"', 1
    )[1].split('v-if="!displayedMaterials.length"', 1)[0]
    assert "<label>楞型" not in composer
    assert 'v-model="productForm.flute_type"' in html
