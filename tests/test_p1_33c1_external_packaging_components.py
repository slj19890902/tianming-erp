from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker


def _spec(kind: str, variant: int = 1) -> tuple[str, str]:
    if kind == "paper_corner_guard":
        data = {
            "length_mm": 1200.0 if variant == 1 else 1000.0,
            "shape": "L",
            "side_a_mm": 50.0 if variant == 1 else 60.0,
            "side_b_mm": 50.0,
            "thickness_mm": 5.0,
        }
        summary = "L型 50×50×5mm，长1200mm" if variant == 1 else "L型 60×50×5mm，长1000mm"
    elif kind == "epe_cushion":
        data = {
            "density_kg_m3": None,
            "layers": 1,
            "length_mm": 300.0,
            "performance": None,
            "shape": "内衬",
            "thickness_mm": 30.0,
            "width_mm": 200.0,
        }
        summary = "内衬 300×200×30mm，1层"
    else:
        data = {
            "finished_height_mm": 45.0,
            "finished_length_mm": 180.0,
            "finished_width_mm": 90.0,
            "ordered_processes": ["覆膜", "模切"],
            "print_color_count": 4,
            "structure": "插口盒",
            "substrate": "350g白卡",
            "unfolded_length_mm": 560.0,
            "unfolded_width_mm": 230.0,
        }
        summary = "插口盒 180×90×45mm，展开560×230mm，350g白卡/4色，覆膜→模切"
    return json.dumps(data, ensure_ascii=False, sort_keys=True), summary


@pytest.fixture()
def component_app(tmp_path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.external_packaging_components import router as component_router
    from app.api.suppliers import router as supplier_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.product import Product
    from app.models.supplier import (
        ExternalPackagingProduct,
        Supplier,
        SupplierSupplyCategory,
    )
    from app.models.user import User
    from app.services.supplier_master import normalize_supplier_identity

    engine = create_sqlite_engine(tmp_path / "external-components.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add_all(
            [
                User(username="component-admin", password_hash=hash_password("123456"), role="admin", real_name="管理员", must_change_password=False),
                User(username="component-sales", password_hash=hash_password("123456"), role="sales", real_name="销售", must_change_password=False),
            ]
        )
        customer_a = Customer(name="匿名客户甲", customer_code="UAT-A")
        customer_b = Customer(name="匿名客户乙", customer_code="UAT-B")
        db.add_all([customer_a, customer_b])
        db.flush()
        product_a = Product(customer_id=customer_a.id, product_code="BOX-A", customer_material_code="BOX-A", product_name="匿名外箱甲", unit="只")
        product_b = Product(customer_id=customer_b.id, product_code="BOX-B", customer_material_code="BOX-B", product_name="匿名外箱乙", unit="只")
        db.add_all([product_a, product_b])
        db.flush()

        definitions = (
            ("匿名供应商甲", "G-CORNER-1", "paper_corner_guard", None, 1, "根"),
            ("匿名供应商乙", "A-CORNER-2", "paper_corner_guard", customer_a.id, 1, "根"),
            ("匿名供应商丙", "B-CORNER-3", "paper_corner_guard", customer_b.id, 1, "根"),
            ("匿名供应商丁", "G-CORNER-DIFF", "paper_corner_guard", None, 2, "根"),
            ("匿名供应商戊", "G-EPE", "epe_cushion", None, 1, "套"),
            ("匿名供应商己", "G-CARTON", "printed_folding_carton", None, 1, "只"),
        )
        ids = {}
        for index, (supplier_name, code, category, scope, variant, unit) in enumerate(definitions, start=1):
            specification, summary = _spec(category, variant)
            supplier = Supplier(
                standard_name=supplier_name,
                normalized_name=normalize_supplier_identity(supplier_name),
                display_name=f"供应商{index}",
                is_active=True,
                sort_order=index,
                version=1,
                supply_categories=[SupplierSupplyCategory(category_code=category)],
            )
            supplier.packaging_products.append(
                ExternalPackagingProduct(
                    category_code=category,
                    supplier_product_code=code,
                    normalized_supplier_product_code=code,
                    product_name=summary,
                    purchase_unit=unit,
                    specification_summary=summary,
                    specification_json=specification,
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
    app.include_router(component_router, prefix="/api/master/products")
    app.include_router(supplier_router, prefix="/api/master/suppliers")

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    app.state.session_factory = factory
    app.state.fixture = {
        "product_a": product_a.id,
        "product_b": product_b.id,
        "customer_a": customer_a.id,
        "customer_b": customer_b.id,
        **ids,
    }
    yield app
    engine.dispose()


def _login(client: TestClient, username: str = "component-admin") -> None:
    response = client.post("/api/auth/login", json={"username": username, "password": "123456"})
    assert response.status_code == 200


def _three_component_payload(ids: dict[str, int], version: int = 0) -> dict:
    return {
        "expected_version": version,
        "components": [
            {
                "purpose": "四角防护",
                "quantity_per_finished_unit": "4",
                "waste_rate": "0.02",
                "consumption_unit": "根",
                "customer_specification": {
                    "shape": "L",
                    "length_mm": 780,
                    "side_a_mm": 50,
                    "side_b_mm": 50,
                    "thickness_mm": 5,
                },
                "is_required": True,
                "candidates": [
                    {"external_product_id": ids["G-CORNER-1"], "is_default": True},
                    {"external_product_id": ids["A-CORNER-2"], "is_default": False},
                ],
            },
            {
                "purpose": "缓冲内衬",
                "quantity_per_finished_unit": "1",
                "waste_rate": "0",
                "consumption_unit": "套",
                "is_required": True,
                "candidates": [{"external_product_id": ids["G-EPE"], "is_default": True}],
            },
            {
                "purpose": "彩印内盒",
                "quantity_per_finished_unit": "1",
                "waste_rate": "0.01",
                "consumption_unit": "只",
                "is_required": True,
                "candidates": [{"external_product_id": ids["G-CARTON"], "is_default": True}],
            },
        ],
    }


def test_catalog_respects_customer_scope_and_admin_gate(component_app: FastAPI) -> None:
    ids = component_app.state.fixture
    with TestClient(component_app) as client:
        _login(client, "component-sales")
        assert client.get(f"/api/master/products/{ids['product_a']}/external-components").status_code == 403
        _login(client)
        items = client.get(f"/api/master/products/{ids['product_a']}/external-component-candidates").json()["items"]
        codes = {item["supplier_product_code"] for item in items}
        assert "G-CORNER-1" in codes
        assert "A-CORNER-2" in codes
        assert "B-CORNER-3" not in codes


def test_supplier_catalog_saves_customer_scope(component_app: FastAPI) -> None:
    ids = component_app.state.fixture
    with TestClient(component_app) as client:
        _login(client)
        supplier = client.get("/api/master/suppliers").json()["items"][0]
        payload = {
            "category_code": "paper_corner_guard",
            "supplier_product_code": "SCOPE-API",
            "product_name": "客户专用护角",
            "purchase_unit": "根",
            "customer_scope_id": ids["customer_a"],
            "specification": {
                "shape": "L",
                "side_a_mm": 40,
                "side_b_mm": 40,
                "thickness_mm": 4,
                "length_mm": 1000,
            },
        }
        created = client.post(
            f"/api/master/suppliers/{supplier['id']}/packaging-products", json=payload
        )
        assert created.status_code == 201, created.text
        assert created.json()["customer_scope_id"] == ids["customer_a"]
        assert created.json()["customer_scope_name"] == "匿名客户甲"
        payload["supplier_product_code"] = "SCOPE-BAD"
        payload["customer_scope_id"] = 999999
        assert client.post(
            f"/api/master/suppliers/{supplier['id']}/packaging-products", json=payload
        ).status_code == 422


def test_three_components_multiple_candidates_version_and_audit(component_app: FastAPI) -> None:
    from app.models.audit import OperationLog
    from app.models.product_bom import ProductBomComponent

    ids = component_app.state.fixture
    payload = _three_component_payload(ids)
    with TestClient(component_app) as client:
        _login(client)
        response = client.put(f"/api/master/products/{ids['product_a']}/external-components", json=payload)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["version"] == 1
        assert [row["purpose"] for row in data["components"]] == ["四角防护", "缓冲内衬", "彩印内盒"]
        assert len(data["components"][0]["candidates"]) == 2
        assert sum(1 for row in data["components"][0]["candidates"] if row["is_default"]) == 1
        stale = client.put(f"/api/master/products/{ids['product_a']}/external-components", json=payload)
        assert stale.status_code == 409
    with component_app.state.session_factory() as db:
        assert db.scalar(select(func.count(ProductBomComponent.id))) == 0
        assert db.scalar(select(func.count(OperationLog.id)).where(OperationLog.resource == "ProductExternalPackagingComponents")) == 1


def test_rejects_other_customer_and_mixed_specifications(component_app: FastAPI) -> None:
    ids = component_app.state.fixture
    with TestClient(component_app) as client:
        _login(client)
        other_customer = _three_component_payload(ids)
        other_customer["components"][0]["candidates"][1]["external_product_id"] = ids["B-CORNER-3"]
        response = client.put(f"/api/master/products/{ids['product_a']}/external-components", json=other_customer)
        assert response.status_code == 409
        assert "其他客户专用" in response.json()["detail"]

        mixed = _three_component_payload(ids)
        mixed["components"][0]["candidates"][1]["external_product_id"] = ids["G-CORNER-DIFF"]
        response = client.put(f"/api/master/products/{ids['product_a']}/external-components", json=mixed)
        assert response.status_code == 422
        assert "截面不兼容" in response.json()["detail"]


def test_inactive_candidate_history_readable_but_cannot_be_resaved(component_app: FastAPI) -> None:
    from app.models.supplier import ExternalPackagingProduct

    ids = component_app.state.fixture
    with TestClient(component_app) as client:
        _login(client)
        created = client.put(
            f"/api/master/products/{ids['product_a']}/external-components",
            json=_three_component_payload(ids),
        )
        assert created.status_code == 200
        with component_app.state.session_factory() as db:
            row = db.get(ExternalPackagingProduct, ids["A-CORNER-2"])
            row.is_active = False
            row.version += 1
            db.commit()
        history = client.get(f"/api/master/products/{ids['product_a']}/external-components").json()
        inactive = next(row for row in history["components"][0]["candidates"] if row["external_product_id"] == ids["A-CORNER-2"])
        assert inactive["currently_available"] is False
        retry = _three_component_payload(ids, version=history["version"])
        blocked = client.put(f"/api/master/products/{ids['product_a']}/external-components", json=retry)
        assert blocked.status_code == 409
        assert "已停用" in blocked.json()["detail"]


def test_unit_conversion_requires_evidence(component_app: FastAPI) -> None:
    ids = component_app.state.fixture
    payload = _three_component_payload(ids)
    payload["components"][1]["consumption_unit"] = "件"
    with TestClient(component_app) as client:
        _login(client)
        blocked = client.put(f"/api/master/products/{ids['product_a']}/external-components", json=payload)
        assert blocked.status_code == 422
        assert "换算依据" in blocked.json()["detail"]
        payload["components"][1]["units_per_purchase_unit"] = "1"
        payload["components"][1]["conversion_basis"] = "供应商规格书：1套=1件"
        assert client.put(f"/api/master/products/{ids['product_a']}/external-components", json=payload).status_code == 200


def test_frontend_exposes_customer_scope_and_external_component_editor() -> None:
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "static/index.html").read_text(encoding="utf-8")
    for needle in (
        "适用客户",
        "customer_scope_id",
        "外购包装组件",
        "loadExternalComponents(productId)",
        "saveExternalComponents()",
        "不会自动生成采购单",
    ):
        assert needle in source
