from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker


def test_supplier_paper_codes_and_material_composer(tmp_path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.materials import router as materials_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.supplier import Supplier
    from app.models.user import User
    from app.services.supplier_master import normalize_supplier_identity

    engine = create_sqlite_engine(tmp_path / "material-composer.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add_all(
            [
                Customer(
                    customer_number=1,
                    customer_code="COMPOSER-CUSTOMER",
                    name="材质组合测试客户",
                    credit_limit=Decimal("0"),
                ),
                User(
                    username="composer-admin",
                    password_hash=hash_password("ComposerPass123!"),
                    role="admin",
                    real_name="材质管理员",
                    must_change_password=False,
                ),
                User(
                    username="composer-workshop",
                    password_hash=hash_password("ComposerPass123!"),
                    role="workshop",
                    real_name="车间用户",
                    must_change_password=False,
                ),
                Supplier(
                    standard_name="供应商A",
                    normalized_name=normalize_supplier_identity("供应商A"),
                    display_name="供应商A",
                    sort_order=10,
                    is_active=True,
                    version=1,
                ),
                Supplier(
                    standard_name="供应商B",
                    normalized_name=normalize_supplier_identity("供应商B"),
                    display_name="供应商B",
                    sort_order=20,
                    is_active=True,
                    version=1,
                ),
                Material(
                    code="J616J",
                    supplier_name="供应商A",
                    layer_count=5,
                    flute_type="AB",
                    quote_price=Decimal("2.3400"),
                    is_active=True,
                ),
            ]
        )
        db.commit()

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
            json={"username": "composer-admin", "password": "ComposerPass123!"},
        ).status_code == 200

        base_rows = [
            ("J", 190, "国产AA级牛卡"),
            ("6", 130, "国产A级施胶荷瓦"),
            ("1", 50, "国产普瓦"),
            ("A", 160, "国产A级牛卡"),
        ]
        for code, weight, paper_name in base_rows:
            response = client.post(
                "/api/master/materials/paper-codes",
                json={
                    "supplier_name": "供应商A",
                    "code_char": code,
                    "paper_name": paper_name,
                    "gram_weight": weight,
                    "paper_grade": "测试等级",
                    "paper_role": "通用",
                },
            )
            assert response.status_code == 201
        assert client.post(
            "/api/master/materials/paper-codes",
            json={
                "supplier_name": "供应商A",
                "code_char": "J",
                "paper_name": "重复代码",
                "gram_weight": 190,
            },
        ).status_code == 409
        listed = client.get(
            "/api/master/materials/paper-codes",
            params={"supplier_name": "供应商A"},
        )
        assert listed.status_code == 200
        assert listed.json()["total"] == 4

        five_layer = client.post(
            "/api/master/materials/compose/preview",
            json={
                "supplier_name": "供应商A",
                "material_code": "j616j",
                "flute_type": "AB",
            },
        )
        assert five_layer.status_code == 200
        result = five_layer.json()
        assert result["material_code"] == "J616J"
        assert result["layer_count"] == 5
        assert [row["gram_weight"] for row in result["layers"]] == [
            190,
            130,
            50,
            130,
            190,
        ]
        assert [row["role"] for row in result["layers"]] == [
            "面纸",
            "B楞瓦纸",
            "芯纸",
            "A楞瓦纸",
            "里纸",
        ]
        assert result["total_gram_weight"] == 690
        assert result["existing_square_price"] == "2.3400"

        missing_x = client.post(
            "/api/master/materials/compose/preview",
            json={
                "supplier_name": "供应商A",
                "material_code": "J616X",
                "flute_type": "AB",
            },
        )
        assert missing_x.status_code == 200
        assert missing_x.json()["valid"] is False
        assert missing_x.json()["missing_codes"] == ["X"]
        assert client.post(
            "/api/master/materials/compose/save",
            json={
                "supplier_name": "供应商A",
                "layer_count": 5,
                "material_code": "J616X",
                "quote_price": 2.5,
                "parsed_supplier_name": "供应商A",
                "parsed_layer_count": 5,
                "parsed_material_code": "J616X",
                "price_source": "manual",
            },
        ).status_code == 400

        three_layer = client.post(
            "/api/master/materials/compose/preview",
            json={
                "supplier_name": "供应商A",
                "layer_count": 3,
                "material_code": "A6A",
                "parsed_supplier_name": "供应商A",
                "parsed_layer_count": 3,
                "parsed_material_code": "A6A",
                "price_source": "manual",
            },
        )
        assert three_layer.status_code == 200
        assert three_layer.json()["layer_count"] == 3
        assert [row["role"] for row in three_layer.json()["layers"]] == [
            "面纸",
            "瓦楞纸",
            "里纸",
        ]
        assert three_layer.json()["existing_square_price"] is None
        manual_preview = client.post(
            "/api/master/materials/compose/preview",
            json={
                "supplier_name": "供应商A",
                "material_code": "A6A",
                "quote_price": 1.56,
            },
        )
        assert manual_preview.status_code == 200
        assert manual_preview.json()["message"] == "已手工填写平方价，可保存为可用材质"

        seven_layer_without_flute = client.post(
            "/api/master/materials/compose/preview",
            json={
                "supplier_name": "供应商A",
                "layer_count": 7,
                "material_code": "JA616AJ",
            },
        )
        assert seven_layer_without_flute.status_code == 422

        seven_layer_with_invalid_flute = client.post(
            "/api/master/materials/compose/preview",
            json={
                "supplier_name": "供应商A",
                "layer_count": 7,
                "material_code": "JA616AJ",
                "usage_flute_type": "AB",
            },
        )
        assert seven_layer_with_invalid_flute.status_code == 422

        seven_layer = client.post(
            "/api/master/materials/compose/preview",
            json={
                "supplier_name": "供应商A",
                "layer_count": 7,
                "material_code": "JA616AJ",
                "usage_flute_type": "ABC",
            },
        )
        assert seven_layer.status_code == 200
        seven_result = seven_layer.json()
        assert seven_result["valid"] is True
        assert seven_result["layer_count"] == 7
        assert [row["code_char"] for row in seven_result["layers"]] == list("JA616AJ")
        assert len(seven_result["layers"]) == 7
        assert seven_result["price_calculation"]["calculable"] is False
        assert seven_result["current_suggested_price"] is None

        seven_layer_no_price = client.post(
            "/api/master/materials/compose/save",
            json={
                "supplier_name": "供应商A",
                "layer_count": 7,
                "material_code": "JA616AJ",
                "usage_flute_type": "ABC",
                "parsed_supplier_name": "供应商A",
                "parsed_layer_count": 7,
                "parsed_material_code": "JA616AJ",
                "price_source": "manual",
            },
        )
        assert seven_layer_no_price.status_code == 400

        seven_layer_saved = client.post(
            "/api/master/materials/compose/save",
            json={
                "supplier_name": "供应商A",
                "layer_count": 7,
                "material_code": "JA616AJ",
                "usage_flute_type": "ABC",
                "quote_price": 3.58,
                "parsed_supplier_name": "供应商A",
                "parsed_layer_count": 7,
                "parsed_material_code": "JA616AJ",
                "price_source": "manual",
            },
        )
        assert seven_layer_saved.status_code == 200
        assert seven_layer_saved.json()["material"]["flute_type"] is None
        assert seven_layer_saved.json()["material"]["quote_price"] == "3.5800"

        for code, weight in [("6", 120), ("1", 45)]:
            assert client.post(
                "/api/master/materials/paper-codes",
                json={
                    "supplier_name": "供应商B",
                    "code_char": code,
                    "paper_name": f"供应商B纸种{code}",
                    "gram_weight": weight,
                },
            ).status_code == 201
        isolated = client.post(
            "/api/master/materials/compose/preview",
            json={
                "supplier_name": "供应商B",
                "material_code": "J616J",
                "flute_type": "AB",
            },
        )
        assert isolated.status_code == 200
        assert isolated.json()["valid"] is False
        assert isolated.json()["missing_codes"] == ["J"]

        no_price = client.post(
            "/api/master/materials/compose/save",
            json={
                "supplier_name": "供应商A",
                "layer_count": 3,
                "material_code": "A6A",
                "parsed_supplier_name": "供应商A",
                "parsed_layer_count": 3,
                "parsed_material_code": "A6A",
                "price_source": "manual",
            },
        )
        assert no_price.status_code == 400
        saved = client.post(
            "/api/master/materials/compose/save",
            json={
                "supplier_name": "供应商A",
                "layer_count": 3,
                "material_code": "A6A",
                "quote_price": 1.56,
                "parsed_supplier_name": "供应商A",
                "parsed_layer_count": 3,
                "parsed_material_code": "A6A",
                "price_source": "manual",
                "remarks": "人工确认平方价",
            },
        )
        assert saved.status_code == 200
        assert saved.json()["created"] is True
        assert saved.json()["total_gram_weight"] == 450
        material = saved.json()["material"]
        assert material["basis_weight_description"] == "160g/130g/160g"
        assert material["quote_price"] == "1.5600"
        selectable = client.get(
            "/api/master/materials",
            params={"supplier_name": "供应商A", "keyword": "A6A"},
        )
        assert selectable.status_code == 200
        assert selectable.json()["total"] == 1

        wrong_length = client.post(
            "/api/master/materials/compose/preview",
            json={
                "supplier_name": "供应商A",
                "material_code": "A6",
                "flute_type": "B",
            },
        )
        assert wrong_length.status_code == 422

    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={
                "username": "composer-workshop",
                "password": "ComposerPass123!",
            },
        ).status_code == 200
        assert client.post(
            "/api/master/materials/compose/preview",
            json={
                "supplier_name": "供应商A",
                "material_code": "J616J",
                "flute_type": "AB",
            },
        ).status_code == 403


def test_supplier_material_composer_frontend_and_migration_chain():
    root = Path(__file__).resolve().parents[1]
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    assert "供应商材质规则维护" in html
    assert "新增/组合材质" in html
    assert "/api/master/materials/paper-codes" in html
    assert "/api/master/materials/compose/preview" in html
    assert "/api/master/materials/compose/save" in html
    assert "系统自动解析和推算建议价" in html
    assert "代码字符（可用 + 等符号）" in html
    assert "基础代码必须是单个可见字符（支持 + 等符号）" in html
    assert "基础代码必须是单个字母或数字" not in html
    assert "!/^[A-Z0-9]$/.test(payload.code_char)" not in html

    migration = (
        root
        / "alembic"
        / "versions"
        / "b19t6u7v8w18_corrugated_material_pricing_rules.py"
    ).read_text(encoding="utf-8")
    assert 'down_revision = "a18t5u6v7w17"' in migration
    assert '"supplier_material_base_prices"' in migration
