from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def p1_40a_app(tmp_path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.products import router as products_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.supplier import ExternalPackagingProduct, Supplier, SupplierSupplyCategory
    from app.models.user import User
    from app.services.supplier_master import normalize_supplier_identity

    engine = create_sqlite_engine(tmp_path / "p1-40a.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="p1-40a-admin",
            password_hash=hash_password("123456"),
            role="admin",
            real_name="管理员",
            must_change_password=False,
        )
        customer_a = Customer(name="匿名客户甲", customer_code="P140-A")
        customer_b = Customer(name="匿名客户乙", customer_code="P140-B")
        db.add_all([admin, customer_a, customer_b])
        db.flush()

        specification = json.dumps(
            {
                "length_mm": 870.0,
                "shape": "L",
                "side_a_mm": 50.0,
                "side_b_mm": 50.0,
                "thickness_mm": 5.0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        ids: dict[str, int] = {"customer_a": customer_a.id, "customer_b": customer_b.id}
        definitions = (
            ("匿名护角供应商一", "CG-870-A", customer_a.id, specification, "L型 50×50×5mm，长870mm"),
            ("匿名护角供应商二", "CG-870-G", None, specification, "L型 50×50×5mm，长870mm"),
            ("匿名护角供应商三", "CG-1000", None, specification.replace("870.0", "1000.0"), "L型 50×50×5mm，长1000mm"),
            ("匿名护角供应商四", "CG-B", customer_b.id, specification, "L型 50×50×5mm，长870mm"),
        )
        for index, (name, code, scope, spec, summary) in enumerate(definitions, start=1):
            supplier = Supplier(
                standard_name=name,
                normalized_name=normalize_supplier_identity(name),
                display_name=f"护角供应商{index}",
                is_active=True,
                sort_order=index,
                version=1,
                supply_categories=[SupplierSupplyCategory(category_code="paper_corner_guard")],
            )
            supplier.packaging_products.append(
                ExternalPackagingProduct(
                    category_code="paper_corner_guard",
                    supplier_product_code=code,
                    normalized_supplier_product_code=code,
                    product_name=summary,
                    purchase_unit="根",
                    specification_summary=summary,
                    specification_json=spec,
                    customer_scope_id=scope,
                    is_active=True,
                    version=1,
                )
            )
            db.add(supplier)
            db.flush()
            ids[code] = supplier.packaging_products[0].id
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(products_router, prefix="/api/master/products")

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    app.state.factory = factory
    app.state.fixture = ids
    yield app
    engine.dispose()


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "p1-40a-admin", "password": "123456"},
    )
    assert response.status_code == 200


def _external_payload(ids: dict[str, int], candidates: list[dict] | None = None) -> dict:
    return {
        "customer_id": ids["customer_a"],
        "product_code": "EXT-CG-870",
        "customer_material_code": "EXT-CG-870",
        "product_name": "匿名客户护角",
        "box_category": "normal",
        "box_style": "其他",
        "supply_mode": "external_purchase",
        "material_id": None,
        "length_mm": 500,
        "width_mm": 400,
        "height_mm": 300,
        "die_cut_path": "legacy-paper-path",
        "layer_count": 5,
        "flute_type": "AB",
        "report_length_mm": 999,
        "report_width_mm": 888,
        "production_process": "印刷,打钉",
        "print_content": "双色印刷",
        "production_label_enabled": True,
        "production_label_units_per_label": 50,
        "external_supply": {
            "customer_specification": {
                "shape": "L",
                "length_mm": 870,
                "side_a_mm": 50,
                "side_b_mm": 50,
                "thickness_mm": 5,
            },
            "candidates": candidates
            or [{"external_product_id": ids["CG-870-A"], "is_default": False}]
        },
    }


def test_external_product_candidates_scope_and_single_default(p1_40a_app: FastAPI) -> None:
    ids = p1_40a_app.state.fixture
    with TestClient(p1_40a_app) as client:
        _login(client)
        response = client.get(
            "/api/master/products/external-supply-candidates",
            params={"customer_id": ids["customer_a"], "category_code": "paper_corner_guard"},
        )
        assert response.status_code == 200
        codes = {item["supplier_product_code"] for item in response.json()["items"]}
        assert codes == {"CG-870-A", "CG-870-G", "CG-1000"}

        created = client.post("/api/master/products", json=_external_payload(ids))
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["supply_mode"] == "external_purchase"
        assert body["material_id"] is None
        assert body["length_mm"] is None
        assert body["width_mm"] is None
        assert body["height_mm"] is None
        assert body["die_cut_path"] is None
        assert body["layer_count"] is None
        assert body["flute_type"] is None
        assert body["report_length_mm"] is None
        assert body["production_process"] is None
        assert body["print_content"] == "无印刷"
        assert body["production_label_enabled"] is False
        assert body["readiness"]["ready"] is True
        assert body["external_supply"]["specification"]["length_mm"] == 870.0
        assert body["external_supply"]["candidates"][0]["is_default"] is True

        summary = client.get(
            "/api/master/products",
            params={"customer_id": ids["customer_a"], "response_mode": "summary"},
        )
        assert summary.status_code == 200
        summary_item = summary.json()["items"][0]
        assert summary_item["supply_mode"] == "external_purchase"
        assert summary_item["external_packaging_specification_summary"] == "870×50×50×5mm"
        assert summary_item["external_packaging_purchase_unit"] == "根"

        blocked_sync = client.post(
            f"/api/master/products/{body['id']}/sync-fields",
            json={"expected_version": body["version"], "fields": {"length_mm": 600}},
        )
        assert blocked_sync.status_code == 409
        assert "不能从订单同步纸板" in blocked_sync.text


def test_multiple_candidates_require_one_default_and_old_client_preserves(p1_40a_app: FastAPI) -> None:
    ids = p1_40a_app.state.fixture
    with TestClient(p1_40a_app) as client:
        _login(client)
        missing_default = _external_payload(
            ids,
            [
                {"external_product_id": ids["CG-870-A"], "is_default": False},
                {"external_product_id": ids["CG-870-G"], "is_default": False},
            ],
        )
        assert client.post("/api/master/products", json=missing_default).status_code == 422
        mismatched = _external_payload(
            ids,
            [
                {"external_product_id": ids["CG-870-A"], "is_default": True},
                {"external_product_id": ids["CG-1000"], "is_default": False},
            ],
        )
        mismatched["product_code"] = "EXT-CG-MULTI-LENGTH"
        mismatched["customer_material_code"] = "EXT-CG-MULTI-LENGTH"
        accepted = client.post("/api/master/products", json=mismatched)
        assert accepted.status_code == 201, accepted.text
        assert accepted.json()["external_supply"]["specification"]["length_mm"] == 870

        payload = _external_payload(
            ids,
            [
                {"external_product_id": ids["CG-870-A"], "is_default": True},
                {"external_product_id": ids["CG-870-G"], "is_default": False},
            ],
        )
        created = client.post("/api/master/products", json=payload)
        assert created.status_code == 201, created.text
        before = created.json()
        legacy = {key: value for key, value in before.items() if key in {
            "customer_id", "product_code", "customer_material_code", "product_name",
            "material_id", "mold_tool_id", "legacy_material_text", "length_mm", "width_mm",
            "height_mm", "box_category", "box_style", "print_content", "printing_colors",
            "printing_plate_mode", "printing_plate_1_id", "printing_plate_2_id",
            "printing_plate_3_id", "plate_alignment_value_mm", "plate_mount_value_mm",
            "machine_set_length_mm", "machine_set_width_mm", "machine_set_height_mm",
            "production_process", "unit", "sale_unit_price", "sale_unit_price_no_tax",
            "cost_unit_price", "board_price", "suggested_price", "die_cut_path", "remark",
            "is_active", "flute_type", "layer_count", "surface_paper_type", "report_length_mm",
            "report_width_mm", "crease_type", "crease_left_mm", "crease_middle_mm",
            "crease_right_mm", "report_notes", "base_report_length_mm", "base_report_width_mm",
            "base_crease_type", "base_crease_left_mm", "base_crease_middle_mm",
            "base_crease_right_mm", "base_report_notes", "splice_mode", "pieces_per_box",
            "default_cutting_mode", "production_label_enabled",
            "production_label_units_per_label", "flap_mm", "combination_mode",
        }}
        legacy["expected_version"] = before["version"]
        legacy["product_name"] = "旧客户端改名"
        legacy["length_mm"] = 999
        legacy["report_length_mm"] = 777
        legacy["production_process"] = "打钉"
        updated = client.put(f"/api/master/products/{before['id']}", json=legacy)
        assert updated.status_code == 200, updated.text
        after = updated.json()
        assert after["supply_mode"] == "external_purchase"
        assert after["length_mm"] is None
        assert after["report_length_mm"] is None
        assert after["production_process"] is None
        assert [row["external_product_id"] for row in after["external_supply"]["candidates"]] == [
            ids["CG-870-A"], ids["CG-870-G"]
        ]


def test_corrugated_product_rejects_external_candidates(p1_40a_app: FastAPI) -> None:
    ids = p1_40a_app.state.fixture
    with TestClient(p1_40a_app) as client:
        _login(client)
        payload = _external_payload(ids)
        payload["supply_mode"] = "corrugated_production"
        response = client.post("/api/master/products", json=payload)
        assert response.status_code == 422
        assert "不能绑定" in response.text



def test_hollow_board_specification_is_structured_and_validated() -> None:
    from fastapi import HTTPException

    from app.api.suppliers import (
        ExternalPackagingProductPayload,
        _clean_external_product_payload,
    )

    cleaned = _clean_external_product_payload(
        ExternalPackagingProductPayload(
            category_code="hollow_board",
            supplier_product_code="HB-1200",
            product_name="匿名中空板",
            purchase_unit="张",
            specification={
                "length_mm": 1200,
                "width_mm": 800,
                "thickness_mm": 5,
                "color": "蓝色",
                "basis_weight_gsm": 900,
            },
        )
    )
    specification = json.loads(cleaned["specification_json"])
    assert specification == {
        "basis_weight_gsm": 900.0,
        "color": "蓝色",
        "density_kg_m3": None,
        "length_mm": 1200.0,
        "thickness_mm": 5.0,
        "width_mm": 800.0,
    }
    assert cleaned["specification_summary"] == "蓝色 1200×800×5mm，900g/㎡"

    with pytest.raises(HTTPException, match="中空板颜色"):
        _clean_external_product_payload(
            ExternalPackagingProductPayload(
                category_code="hollow_board",
                supplier_product_code="HB-NO-COLOR",
                product_name="匿名中空板",
                purchase_unit="张",
                specification={
                    "length_mm": 1200,
                    "width_mm": 800,
                    "thickness_mm": 5,
                },
            )
        )
