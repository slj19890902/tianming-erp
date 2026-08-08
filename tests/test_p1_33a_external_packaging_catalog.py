from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def packaging_app(tmp_path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.suppliers import router as suppliers_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.supplier import Supplier, SupplierAlias, SupplierSupplyCategory
    from app.models.user import User
    from app.services.supplier_master import normalize_supplier_identity

    engine = create_sqlite_engine(tmp_path / "external-packaging.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add_all(
            [
                User(
                    username="packaging-admin",
                    password_hash=hash_password("123456"),
                    role="admin",
                    real_name="外购产品管理员",
                    must_change_password=False,
                ),
                User(
                    username="packaging-sales",
                    password_hash=hash_password("123456"),
                    role="sales",
                    real_name="销售",
                    must_change_password=False,
                ),
            ]
        )
        for index, name in enumerate(("护角供应商甲", "护角供应商乙"), start=1):
            supplier = Supplier(
                standard_name=name,
                normalized_name=normalize_supplier_identity(name),
                display_name=f"护角{index}",
                is_active=True,
                sort_order=index * 10,
                version=1,
                aliases=[
                    SupplierAlias(
                        alias_name=f"HJ{index}",
                        normalized_alias=normalize_supplier_identity(f"HJ{index}"),
                    )
                ],
                supply_categories=[
                    SupplierSupplyCategory(category_code="corrugated_board"),
                    SupplierSupplyCategory(category_code="paper_corner_guard"),
                    SupplierSupplyCategory(category_code="epe_cushion"),
                ],
            )
            db.add(supplier)
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(suppliers_router, prefix="/api/master/suppliers")

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    app.state.session_factory = factory
    yield app
    engine.dispose()


def _login(client: TestClient, username: str = "packaging-admin") -> None:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": "123456"}
    )
    assert response.status_code == 200


def _corner_guard_payload(code: str = "HJ-50505") -> dict:
    return {
        "category_code": "paper_corner_guard",
        "supplier_product_code": code,
        "product_name": "L型纸护角",
        "purchase_unit": "根",
        "specification": {
            "shape": "L",
            "side_a_mm": 50,
            "side_b_mm": 50,
            "thickness_mm": 5,
            "length_mm": 1200,
        },
        "lead_time_days": 3,
    }


def test_supplier_categories_are_returned_and_admin_only(packaging_app: FastAPI) -> None:
    with TestClient(packaging_app) as client:
        _login(client, "packaging-sales")
        assert client.get("/api/master/suppliers").status_code == 403

        _login(client)
        rows = client.get("/api/master/suppliers").json()["items"]
        assert rows[0]["supply_categories"] == [
            "corrugated_board",
            "paper_corner_guard",
            "epe_cushion",
        ]


def test_same_supplier_code_is_unique_per_supplier(packaging_app: FastAPI) -> None:
    with TestClient(packaging_app) as client:
        _login(client)
        suppliers = client.get("/api/master/suppliers").json()["items"]
        first, second = suppliers
        created = client.post(
            f"/api/master/suppliers/{first['id']}/packaging-products",
            json=_corner_guard_payload(),
        )
        assert created.status_code == 201, created.text
        assert created.json()["specification_summary"] == "L型 50×50×5mm，长1200mm"

        duplicate = client.post(
            f"/api/master/suppliers/{first['id']}/packaging-products",
            json=_corner_guard_payload(" hj-50505 "),
        )
        assert duplicate.status_code == 409

        other_supplier = client.post(
            f"/api/master/suppliers/{second['id']}/packaging-products",
            json=_corner_guard_payload(),
        )
        assert other_supplier.status_code == 201, other_supplier.text


def test_category_specific_validation_and_version_gate(packaging_app: FastAPI) -> None:
    with TestClient(packaging_app) as client:
        _login(client)
        supplier = client.get("/api/master/suppliers").json()["items"][0]
        invalid = _corner_guard_payload()
        invalid["specification"]["thickness_mm"] = 0
        response = client.post(
            f"/api/master/suppliers/{supplier['id']}/packaging-products",
            json=invalid,
        )
        assert response.status_code == 422
        assert "厚度必须大于 0" in response.json()["detail"]

        created = client.post(
            f"/api/master/suppliers/{supplier['id']}/packaging-products",
            json=_corner_guard_payload("HJ-VERSION"),
        ).json()
        updated = client.put(
            f"/api/master/suppliers/{supplier['id']}/packaging-products/{created['id']}",
            json={**_corner_guard_payload("HJ-VERSION"), "expected_version": 1},
        )
        assert updated.status_code == 200
        assert updated.json()["version"] == 2
        stale = client.put(
            f"/api/master/suppliers/{supplier['id']}/packaging-products/{created['id']}",
            json={**_corner_guard_payload("HJ-VERSION"), "expected_version": 1},
        )
        assert stale.status_code == 409


def test_four_categories_keep_only_meaningful_structured_specs(
    packaging_app: FastAPI,
) -> None:
    with TestClient(packaging_app) as client:
        _login(client)
        supplier = client.get("/api/master/suppliers").json()["items"][0]
        supplier_payload = {
            key: supplier.get(key)
            for key in (
                "standard_name",
                "display_name",
                "business_code",
                "contact_name",
                "phone",
                "remarks",
                "sort_order",
                "aliases",
            )
        }
        supplier_payload.update(
            expected_version=supplier["version"],
            supply_categories=[
                "corrugated_board",
                "paper_corner_guard",
                "coated_board",
                "printed_folding_carton",
                "epe_cushion",
            ],
        )
        assert client.put(
            f"/api/master/suppliers/{supplier['id']}", json=supplier_payload
        ).status_code == 200

        coated = client.post(
            f"/api/master/suppliers/{supplier['id']}/packaging-products",
            json={
                "category_code": "coated_board",
                "supplier_product_code": "WB-350",
                "product_name": "灰底白板",
                "purchase_unit": "张",
                "specification": {
                    "material_type": "灰底白",
                    "basis_weight_gsm": 350,
                    "thickness_mm": 0.52,
                    "sheet_length_mm": 1092,
                    "sheet_width_mm": 787,
                    "irrelevant_flute": "BC",
                },
            },
        )
        assert coated.status_code == 201, coated.text
        assert set(coated.json()["specification"]) == {
            "material_type",
            "basis_weight_gsm",
            "thickness_mm",
            "sheet_length_mm",
            "sheet_width_mm",
        }

        carton = client.post(
            f"/api/master/suppliers/{supplier['id']}/packaging-products",
            json={
                "category_code": "printed_folding_carton",
                "supplier_product_code": "FC-001",
                "product_name": "彩印折叠盒",
                "purchase_unit": "只",
                "specification": {
                    "structure": "插口盒",
                    "finished_length_mm": 180,
                    "finished_width_mm": 90,
                    "finished_height_mm": 45,
                    "unfolded_length_mm": 560,
                    "unfolded_width_mm": 230,
                    "substrate": "350g白卡",
                    "print_color_count": 4,
                    "ordered_processes": "覆膜→烫金→模切",
                },
            },
        )
        assert carton.status_code == 201, carton.text
        assert carton.json()["specification"]["ordered_processes"] == [
            "覆膜",
            "烫金",
            "模切",
        ]

        epe = client.post(
            f"/api/master/suppliers/{supplier['id']}/packaging-products",
            json={
                "category_code": "epe_cushion",
                "supplier_product_code": "EPE-001",
                "product_name": "EPE内衬",
                "purchase_unit": "套",
                "specification": {
                    "shape": "内衬",
                    "length_mm": 300,
                    "width_mm": 200,
                    "thickness_mm": 30,
                    "layers": 2,
                    "density_kg_m3": 28,
                    "performance": "按供应商样品确认",
                },
            },
        )
        assert epe.status_code == 201, epe.text
        assert epe.json()["specification"]["density_kg_m3"] == 28


def test_removing_category_keeps_history_but_blocks_new_or_reenable(
    packaging_app: FastAPI,
) -> None:
    with TestClient(packaging_app) as client:
        _login(client)
        supplier = client.get("/api/master/suppliers").json()["items"][0]
        created = client.post(
            f"/api/master/suppliers/{supplier['id']}/packaging-products",
            json=_corner_guard_payload("HJ-HISTORY"),
        ).json()
        update_supplier = {
            key: supplier.get(key)
            for key in (
                "standard_name",
                "display_name",
                "business_code",
                "contact_name",
                "phone",
                "remarks",
                "sort_order",
                "aliases",
            )
        }
        update_supplier.update(
            expected_version=supplier["version"],
            supply_categories=["corrugated_board", "epe_cushion"],
        )
        changed = client.put(
            f"/api/master/suppliers/{supplier['id']}", json=update_supplier
        )
        assert changed.status_code == 200, changed.text
        assert "paper_corner_guard" not in changed.json()["supply_categories"]

        listed = client.get(
            f"/api/master/suppliers/{supplier['id']}/packaging-products"
        ).json()["items"]
        assert [row["supplier_product_code"] for row in listed] == ["HJ-HISTORY"]

        disabled = client.put(
            f"/api/master/suppliers/{supplier['id']}/packaging-products/{created['id']}/status",
            json={"expected_version": created["version"], "is_active": False},
        )
        assert disabled.status_code == 200
        blocked = client.put(
            f"/api/master/suppliers/{supplier['id']}/packaging-products/{created['id']}/status",
            json={"expected_version": disabled.json()["version"], "is_active": True},
        )
        assert blocked.status_code == 409
        assert "先在供应商主档启用" in blocked.json()["detail"]


def test_external_product_audit_records_are_written(packaging_app: FastAPI) -> None:
    from app.models.audit import OperationLog

    with TestClient(packaging_app) as client:
        _login(client)
        supplier = client.get("/api/master/suppliers").json()["items"][0]
        created = client.post(
            f"/api/master/suppliers/{supplier['id']}/packaging-products",
            json=_corner_guard_payload("HJ-AUDIT"),
        )
        assert created.status_code == 201
    with packaging_app.state.session_factory() as db:
        actions = db.scalars(
            select(OperationLog.action).where(
                OperationLog.resource == "ExternalPackagingProduct"
            )
        ).all()
        assert actions == ["CREATE"]


def test_supplier_catalog_frontend_has_category_and_catalog_workflow() -> None:
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "static/index.html").read_text(
        encoding="utf-8"
    )
    for needle in (
        "供货类别",
        "openSupplierPackagingCatalog(row)",
        "新增外购产品",
        "supplierPackagingPayload()",
        "paper_corner_guard",
        "coated_board",
        "printed_folding_carton",
        "epe_cushion",
        "先选通用或客户专用范围",
    ):
        assert needle in source
