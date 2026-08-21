from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import base64
from io import BytesIO
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
    from app.middleware.mold_private import MoldPrivateNoStoreMiddleware

    app.add_middleware(MoldPrivateNoStoreMiddleware)

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


def test_mold_private_no_store_covers_validation_and_unhandled_errors() -> None:
    from app.middleware.mold_private import MoldPrivateNoStoreMiddleware

    app = FastAPI()

    @app.get("/api/warehouse/molds/{mold_id}/label")
    def label(mold_id: int) -> dict:
        return {"mold_id": mold_id}

    @app.get("/api/warehouse/molds/live/{mold_id}")
    def live(mold_id: int) -> dict:
        raise RuntimeError(f"fault injection for mold {mold_id}")

    app.add_middleware(MoldPrivateNoStoreMiddleware)
    with TestClient(app, raise_server_exceptions=False) as client:
        responses = (
            client.get("/api/warehouse/molds/not-int/label"),
            client.get("/api/warehouse/molds/live/1"),
        )

    assert [response.status_code for response in responses] == [422, 500]
    for response in responses:
        assert response.headers["cache-control"] == "private, no-store, max-age=0"
        assert response.headers["pragma"] == "no-cache"
        assert response.headers["vary"] == "Cookie"
        assert response.headers["x-robots-tag"] == "noindex, nofollow"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-content-type-options"] == "nosniff"


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


def test_mold_code_is_generated_from_customer_initials_and_inventory_code(
    mold_app,
) -> None:
    app, _factory = mold_app
    with TestClient(app) as client:
        _login(client, "admin")
        preview = client.get(
            "/api/warehouse/molds/code-preview",
            params={
                "mold_name": "仪元 Z.001.000093",
                "customer_initials": "YY",
            },
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["mold_code"] == "YY-Z.001.000093"

        first = client.post(
            "/api/warehouse/molds",
            json={
                "mold_name": "仪元 Z.001.000093",
                "customer_initials": "YY",
                "rack_location": "1F-M-R01-L2-G01",
            },
        )
        assert first.status_code == 201, first.text
        assert first.json()["mold_code"] == "YY-Z.001.000093"

        second = client.post(
            "/api/warehouse/molds",
            json={
                "mold_name": "仪元 Z.001.000093",
                "customer_initials": "YY",
                "rack_location": "1F-M-R01-L2-G01",
            },
        )
        assert second.status_code == 201, second.text
        assert second.json()["mold_code"] == "YY-Z.001.000093-02"


def test_mold_reverse_binding_supports_multiple_customers_and_one_mold_per_product(
    mold_app,
) -> None:
    from app.models.customer import Customer

    app, factory = mold_app
    with factory() as db:
        second_customer = Customer(
            customer_number=9902,
            customer_code="MOLD-X",
            name="跨客户模具测试",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        db.add(second_customer)
        db.commit()
        second_customer_id = second_customer.id

    with TestClient(app) as client:
        _login(client, "admin")
        first_mold = client.post(
            "/api/warehouse/molds",
            json={
                "mold_name": "仪元 Z.001.000093",
                "customer_initials": "YY",
                "rack_location": "1F-M-R01-L2-G01",
            },
        ).json()
        second_mold = client.post(
            "/api/warehouse/molds",
            json={
                "mold_name": "跨客 C80010095",
                "customer_initials": "KK",
                "rack_location": "1F-M-R02-L2-G01",
            },
        ).json()

        product_payloads = (
            (1, "Z.001.000093", "仪元组合箱", "粘贴"),
            (second_customer_id, "C80010095", "跨客户外箱", ""),
            (second_customer_id, "LOCKED-001", "已绑定其他模具", "粘贴"),
        )
        products = []
        for customer_id, code, name, process in product_payloads:
            created = client.post(
                "/api/master/products",
                json={
                    "customer_id": customer_id,
                    "product_code": code,
                    "customer_material_code": code,
                    "product_name": name,
                    "box_category": "normal",
                    "production_process": process,
                },
            )
            assert created.status_code == 201, created.text
            products.append(created.json())

        initial_lock = client.post(
            f"/api/warehouse/molds/{second_mold['id']}/product-bindings",
            json={
                "items": [
                    {
                        "product_id": products[2]["id"],
                        "expected_version": products[2]["version"],
                    }
                ]
            },
        )
        assert initial_lock.status_code == 200, initial_lock.text

        bound = client.post(
            f"/api/warehouse/molds/{first_mold['id']}/product-bindings",
            json={
                "items": [
                    {
                        "product_id": product["id"],
                        "expected_version": product["version"],
                    }
                    for product in products[:2]
                ]
            },
        )
        assert bound.status_code == 200, bound.text
        assert bound.json()["bound_count"] == 2
        assert {
            item["customer_id"] for item in bound.json()["mold"]["products"]
        } == {1, second_customer_id}
        assert all(
            "模切" in item["production_process"]
            for item in bound.json()["mold"]["products"]
        )

        search = client.get(
            "/api/warehouse/molds/binding-products",
            params={"customer_id": second_customer_id, "q": "C80010095"},
        )
        assert search.status_code == 200, search.text
        assert search.json()["items"][0]["mold_code"] == first_mold["mold_code"]

        conflict = client.post(
            f"/api/warehouse/molds/{first_mold['id']}/product-bindings",
            json={
                "items": [
                    {
                        "product_id": products[2]["id"],
                        "expected_version": products[2]["version"] + 1,
                    }
                ]
            },
        )
        assert conflict.status_code == 409, conflict.text
        assert "一个存货编码只能绑定一块模具" in conflict.json()["detail"]

    from sqlalchemy import func, select

    from app.models.master_data_object_version import MasterDataObjectVersion
    from app.models.product import Product

    with factory() as db:
        bound_products = db.scalars(
            select(Product).where(Product.id.in_([row["id"] for row in products[:2]]))
        ).all()
        assert len(bound_products) == 2
        assert all(row.mold_tool_id == first_mold["id"] for row in bound_products)
        assert all("模切" in (row.production_process or "") for row in bound_products)
        assert all(row.version == 2 for row in bound_products)
        assert db.scalar(
            select(func.count(MasterDataObjectVersion.id)).where(
                MasterDataObjectVersion.object_type == "product",
                MasterDataObjectVersion.object_id.in_(
                    [row["id"] for row in products[:2]]
                ),
                MasterDataObjectVersion.action == "mold_binding",
            )
        ) == 2


def test_mold_binding_can_be_removed_with_version_guard_and_audit(mold_app) -> None:
    app, factory = mold_app
    with TestClient(app) as client:
        _login(client, "admin")
        mold = client.post(
            "/api/warehouse/molds",
            json={
                "mold_code": "JSD-61494052",
                "mold_name": "聚晟达 61494052",
                "rack_location": "1F-M-R01-L2-G01",
            },
        ).json()
        product = client.post(
            "/api/master/products",
            json={
                "customer_id": 1,
                "product_code": "61494052R1F",
                "customer_material_code": "61494052R1F",
                "product_name": "350*110",
                "box_category": "normal",
                "production_process": "粘贴",
            },
        ).json()
        bound = client.post(
            f"/api/warehouse/molds/{mold['id']}/product-bindings",
            json={
                "items": [
                    {
                        "product_id": product["id"],
                        "expected_version": product["version"],
                    }
                ]
            },
        )
        assert bound.status_code == 200, bound.text

        stale = client.delete(
            f"/api/warehouse/molds/{mold['id']}/product-bindings/{product['id']}",
            params={"expected_version": product["version"]},
        )
        assert stale.status_code == 409, stale.text

        still_bound = client.get(f"/api/master/products/{product['id']}").json()
        assert still_bound["mold_tool_id"] == mold["id"]
        assert "模切" in still_bound["production_process"]

        removed = client.delete(
            f"/api/warehouse/molds/{mold['id']}/product-bindings/{product['id']}",
            params={"expected_version": still_bound["version"]},
        )
        assert removed.status_code == 200, removed.text
        assert removed.json()["unbound_product"]["mold_tool_id"] is None
        assert removed.json()["unbound_product"]["version"] == 3

        refreshed = client.get(f"/api/master/products/{product['id']}").json()
        assert refreshed["mold_tool_id"] is None
        assert refreshed["production_process"] == "粘贴"
        assert refreshed["version"] == 3

    from sqlalchemy import func, select

    from app.models.audit import OperationLog
    from app.models.master_data_object_version import MasterDataObjectVersion

    with factory() as db:
        assert db.scalar(
            select(func.count(MasterDataObjectVersion.id)).where(
                MasterDataObjectVersion.object_type == "product",
                MasterDataObjectVersion.object_id == product["id"],
                MasterDataObjectVersion.action == "mold_unbinding",
            )
        ) == 1
        assert db.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.action == "UNBIND_PRODUCT",
                OperationLog.resource == "MOLD_TOOL",
                OperationLog.entity_id == mold["id"],
            )
        ) == 1


def test_mold_list_supports_server_pagination_and_preserves_search(mold_app) -> None:
    app, factory = mold_app
    from app.models.mold_tool import MoldTool

    with factory() as db:
        db.add_all(
            [
                MoldTool(
                    mold_code=f"PAGE-{number:03d}",
                    mold_name=(
                        "聚晟达分页目标" if number == 17 else f"分页模具 {number:03d}"
                    ),
                    rack_location="1F-M-R01-L2-G01",
                    created_by=1,
                )
                for number in range(1, 26)
            ]
        )
        db.commit()

    with TestClient(app) as client:
        _login(client, "admin")
        second_page = client.get(
            "/api/warehouse/molds",
            params={"page": 2, "page_size": 10},
        )
        assert second_page.status_code == 200, second_page.text
        assert second_page.json()["total"] == 25
        assert second_page.json()["page"] == 2
        assert second_page.json()["page_size"] == 10
        assert len(second_page.json()["items"]) == 10
        assert second_page.json()["items"][0]["mold_code"] == "PAGE-011"

        searched = client.get(
            "/api/warehouse/molds",
            params={"q": "聚晟达", "page": 1, "page_size": 10},
        )
        assert searched.status_code == 200, searched.text
        assert searched.json()["total"] == 1
        assert searched.json()["items"][0]["mold_code"] == "PAGE-017"
        target_mold_id = searched.json()["items"][0]["id"]

        mapped_area = client.get(
            "/api/warehouse/molds/by-map-area",
            params={
                "floor_code": "1F",
                "feature_code": "ZONE-1F-MOLD-002",
                "page": 2,
                "page_size": 10,
            },
        )
        assert mapped_area.status_code == 200, mapped_area.text
        assert mapped_area.json()["rack_codes"] == ["R01", "R02"]
        assert mapped_area.json()["total"] == 25
        assert mapped_area.json()["page"] == 2
        assert mapped_area.json()["items"][0]["mold_code"] == "PAGE-011"

        located = client.get(
            "/api/warehouse/twin-operations/locate",
            params={"search_type": "mold", "keyword": "PAGE-017"},
        )
        assert located.status_code == 200, located.text
        resource = next(
            row
            for row in located.json()["resources"]
            if row["resource_id"] == f"mold:{target_mold_id}"
        )
        assert resource["primary_code"] is None
        assert resource["feature_codes"] == ["ZONE-1F-MOLD-002"]
        assert resource["map_status"] == "mapped"


def test_mold_rack_front_view_reads_live_molds_from_published_rack(
    mold_app,
    monkeypatch,
) -> None:
    app, factory = mold_app
    from app.api import warehouse
    from app.models.mold_tool import MoldTool

    with factory() as db:
        db.add_all(
            [
                MoldTool(
                    mold_code="RACK-LIVE-001",
                    mold_name="同格模具一",
                    rack_location="1F-M-R01-L2-G01",
                    created_by=1,
                ),
                MoldTool(
                    mold_code="RACK-LIVE-002",
                    mold_name="同格模具二",
                    rack_location="1F-M-R01-L2-G01",
                    created_by=1,
                ),
                MoldTool(
                    mold_code="RACK-OTHER-001",
                    mold_name="其他货架模具",
                    rack_location="1F-M-R02-L2-G01",
                    created_by=1,
                ),
            ]
        )
        db.commit()

    monkeypatch.setattr(
        warehouse,
        "load_warehouse_twin_floor",
        lambda floor_code: {
            "floor_code": floor_code,
            "racks": [
                {
                    "id": "rack-r01",
                    "rack_code": "RACK-1F-MOLD-R01",
                    "mold_rack_code": "R01",
                    "name": "一号模具架",
                    "area_code": "ZONE-1F-MOLD-002",
                    "levels": 3,
                    "bays": 1,
                    "level_cell_counts": [0, 3, 2],
                }
            ],
        },
    )

    with TestClient(app) as client:
        _login(client, "admin")
        response = client.get(
            "/api/warehouse/molds/by-map-rack",
            params={"floor_code": "1F", "rack_id": "rack-r01"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["rack"]["mold_rack_code"] == "R01"
        assert payload["rack"]["level_cell_counts"] == [0, 3, 2]
        assert payload["rack"]["blocked_levels"] == [1]
        assert payload["total"] == 2
        assert [row["mold_code"] for row in payload["items"]] == [
            "RACK-LIVE-001",
            "RACK-LIVE-002",
        ]
        assert all(row["location_guide"]["grid"] == 1 for row in payload["items"])


def test_scoped_account_only_reads_allowed_mold_products_and_labels(mold_app) -> None:
    app, factory = mold_app
    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer
    from app.models.user import User

    with factory() as db:
        denied_customer = Customer(
            customer_number=9902,
            customer_code="MOLD-DENIED",
            name="模具越权隔离客户",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        db.add(denied_customer)
        db.flush()
        denied_customer_id = denied_customer.id
        sales = db.query(User).filter(User.username == "sales").one()
        sales.customer_access_mode = "selected"
        db.add(UserCustomerScope(user_id=sales.id, customer_id=1))
        db.commit()

    with TestClient(app) as admin_client:
        _login(admin_client, "admin")

        def create_mold(code: str) -> int:
            response = admin_client.post(
                "/api/warehouse/molds",
                json={
                    "mold_code": code,
                    "mold_name": f"{code} 测试模具",
                    "rack_location": "1F-M-R01-L2-G01",
                },
            )
            assert response.status_code == 201, response.text
            return response.json()["id"]

        allowed_mold_id = create_mold("SCOPE-ALLOW")
        denied_mold_id = create_mold("SCOPE-DENY")
        shared_mold_id = create_mold("SCOPE-SHARED")
        products = [
            (1, "ALLOW-001", allowed_mold_id),
            (denied_customer_id, "DENY-001", denied_mold_id),
            (1, "SHARED-ALLOW", shared_mold_id),
            (denied_customer_id, "SHARED-DENY", shared_mold_id),
        ]
        product_ids = {}
        for customer_id, product_code, mold_id in products:
            response = admin_client.post(
                "/api/master/products",
                json={
                    "customer_id": customer_id,
                    "product_code": product_code,
                    "customer_material_code": product_code,
                    "product_name": f"{product_code} 产品",
                    "box_category": "normal",
                    "production_process": "模切",
                    "mold_tool_id": mold_id,
                    "report_length_mm": 600,
                    "report_width_mm": 400,
                },
            )
            assert response.status_code == 201, response.text
            product_ids[product_code] = response.json()["id"]

        with factory() as db:
            from app.models.product import Product

            db.get(Product, product_ids["ALLOW-001"]).is_active = False
            db.get(Product, product_ids["SHARED-DENY"]).is_active = False
            db.commit()

    with TestClient(app) as scoped_client:
        _login(scoped_client, "sales")
        listed = scoped_client.get(
            "/api/warehouse/molds",
            params={"include_inactive": True, "limit": 100},
        )
        assert listed.status_code == 200, listed.text
        by_code = {row["mold_code"]: row for row in listed.json()["items"]}
        assert "SCOPE-ALLOW" not in by_code
        assert "SCOPE-DENY" not in by_code
        assert by_code["SCOPE-SHARED"]["product_count"] == 1
        assert [
            row["product_code"] for row in by_code["SCOPE-SHARED"]["products"]
        ] == ["SHARED-ALLOW"]
        denied_search = scoped_client.get(
            "/api/warehouse/molds",
            params={"q": "SHARED-DENY", "limit": 100},
        )
        assert denied_search.status_code == 200, denied_search.text
        assert denied_search.json()["items"] == []

        allowed_detail = scoped_client.get(
            f"/api/warehouse/molds/{allowed_mold_id}/detail"
        )
        assert allowed_detail.status_code == 200, allowed_detail.text
        assert allowed_detail.json()["time_archive"]["inbound_status"] == "not_recorded"
        assert allowed_detail.json()["time_archive"]["stocktake_status"] == "not_supported"
        assert allowed_detail.json()["product_count"] == 0
        assert [
            row["product_code"]
            for row in allowed_detail.json()["binding_history"]
        ] == ["ALLOW-001"]
        shared_detail = scoped_client.get(
            f"/api/warehouse/molds/{shared_mold_id}/detail"
        )
        assert shared_detail.status_code == 200, shared_detail.text
        assert [
            row["product_code"] for row in shared_detail.json()["binding_history"]
        ] == ["SHARED-ALLOW"]
        denied_detail = scoped_client.get(
            f"/api/warehouse/molds/{denied_mold_id}/detail"
        )
        assert denied_detail.status_code == 403, denied_detail.text
        assert denied_detail.json()["detail"] == "无客户访问权限"

        mapped_area = scoped_client.get(
            "/api/warehouse/molds/by-map-area",
            params={"feature_code": "ZONE-1F-MOLD-002", "page_size": 100},
        )
        assert mapped_area.status_code == 200, mapped_area.text
        assert {row["mold_code"] for row in mapped_area.json()["items"]} == {
            "SCOPE-SHARED",
        }

        allowed_label = scoped_client.get(
            f"/api/warehouse/molds/{allowed_mold_id}/label"
        )
        assert allowed_label.status_code == 403, allowed_label.text
        denied_label = scoped_client.get(
            f"/api/warehouse/molds/{denied_mold_id}/label"
        )
        assert denied_label.status_code == 403, denied_label.text
        assert "全客户范围" in denied_label.json()["detail"]

        allowed_batch = scoped_client.get(
            "/api/warehouse/molds/labels",
            params={"mold_ids": str(shared_mold_id)},
        )
        assert allowed_batch.status_code == 403, allowed_batch.text
        assert "全客户范围" in allowed_batch.json()["detail"]
        denied_batch = scoped_client.get(
            "/api/warehouse/molds/labels",
            params={"mold_ids": f"{allowed_mold_id},{denied_mold_id}"},
        )
        assert denied_batch.status_code == 403, denied_batch.text


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


def test_ordinary_mold_disable_is_retired_and_legacy_restore_is_audited(
    mold_app,
) -> None:
    app, factory = mold_app
    from app.models.audit import OperationLog
    from app.models.mold_tool import MoldTool
    from sqlalchemy import select

    with factory() as db:
        legacy = MoldTool(
            mold_code="LEGACY-DISABLED-001",
            mold_name="历史普通停用模具",
            rack_location="1F-M-R01-L2-G01",
            is_active=False,
        )
        db.add(legacy)
        db.commit()
        legacy_id = legacy.id

    with TestClient(app) as client:
        _login(client, "admin")
        retired = client.put(f"/api/warehouse/molds/{legacy_id}/disable")
        assert retired.status_code == 409, retired.text
        assert "普通停用已合并" in retired.json()["detail"]

        restored = client.put(f"/api/warehouse/molds/{legacy_id}/enable")
        assert restored.status_code == 200, restored.text
        assert restored.json()["is_active"] is True

        detail = client.get(f"/api/warehouse/molds/{legacy_id}/detail")
        assert detail.status_code == 200, detail.text
        restore_event = next(
            event
            for event in detail.json()["timeline"]
            if event["event_type"] == "mold_legacy_disabled_restore"
        )
        assert restore_event["label"] == "历史停用恢复使用"
        assert "不代表实物移位" in restore_event["scope_notice"]

    with factory() as db:
        log = db.scalar(
            select(OperationLog).where(
                OperationLog.entity_type == "mold_tool",
                OperationLog.entity_id == legacy_id,
                OperationLog.action_code == "mold.legacy_disabled.restore",
            )
        )
        assert log is not None
        assert log.description == "历史普通停用模具恢复使用"


def test_workshop_can_open_structured_location_label_and_qr(
    mold_app, monkeypatch
) -> None:
    app, factory = mold_app
    from app.api import warehouse
    from app.models.mold_tool import MoldTool
    from app.models.product import Product

    monkeypatch.setattr(
        warehouse,
        "load_settings",
        lambda: type("Settings", (), {"browser_url": "http://192.168.3.80:8000/"})(),
    )
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
                flute_type="AB",
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
            f"HTTP://192.168.3.80:8000/M/{mold_id}"
        )
        assert label.headers["cache-control"] == "private, no-store, max-age=0"
        assert data["qr_data_url"].startswith("data:image/png;base64,")
        from PIL import Image
        import qrcode

        qr_bytes = base64.b64decode(data["qr_data_url"].split(",", 1)[1])
        with Image.open(BytesIO(qr_bytes)) as qr_image:
            assert qr_image.size == (99, 99)
            try:
                import cv2
                import numpy as np
            except ImportError:
                cv2 = None
            if cv2 is not None:
                decoded, _points, _straight = cv2.QRCodeDetector().detectAndDecode(
                    np.asarray(qr_image.convert("RGB"))
                )
                assert decoded == data["lookup_url"]
        qr_contract = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=3,
            border=4,
        )
        qr_contract.add_data(data["lookup_url"])
        qr_contract.make(fit=True)
        assert qr_contract.version == 2
        assert len(qr_contract.get_matrix()) == 33
        assert data["label_identity"].endswith("MJ-MOBILE-001")
        assert set(data) == {
            "mold_code",
            "rack_location",
            "location_guide",
            "is_active",
            "product_count",
            "label_identity",
            "label_customer_name",
            "label_mold_number",
            "label_product_specification",
            "label_report_specification",
            "label_flute_type",
            "lookup_url",
            "qr_data_url",
        }
        assert "id" not in data
        assert "public_lookup_token" not in data
        assert data["label_flute_type"] == "AB"



        live = client.get(f"/api/warehouse/molds/live/{mold_id}")
        assert live.status_code == 200, live.text
        assert live.headers["cache-control"] == "private, no-store, max-age=0"
        assert live.headers["pragma"] == "no-cache"
        assert live.json()["mode"] == "mold_master"
        assert live.json()["read_only"] is True
        serialized_live = live.text.lower()
        for forbidden in (
            "public_lookup_token",
            "unit_price",
            "subtotal",
            "supplier",
            "remarks",
            "lot_number",
            "remaining_sheet_quantity",
        ):
            assert forbidden not in serialized_live


def test_mold_label_dimensions_are_complete_and_printing_fails_closed(
    mold_app,
) -> None:
    from app.models.mold_tool import MoldTool
    from app.models.product import Product

    app, factory = mold_app
    with factory() as db:
        complete = MoldTool(
            mold_code="MOLD-DIM-COMPLETE",
            mold_name="模具联动测试客户DIM001",
            rack_location="1F-M-R01-L2-G01",
        )
        long_identity = MoldTool(
            mold_code="MOLD-LONG-IDENTITY-001",
            mold_name="未按简称规范维护的旧模具",
            rack_location="1F-M-R01-L2-G02",
        )
        long_location = MoldTool(
            mold_code="MOLD-LONG-LOCATION",
            mold_name="模具联动测试客户LOC001",
            rack_location="这是一条超过二十个字符且无法完整打印的历史自由文本模具位置",
        )
        archived = MoldTool(
            mold_code="MOLD-ARCHIVED-LABEL",
            mold_name="模具联动测试客户ARC001",
            rack_location="3F-M-R01-L1-G01",
            is_active=False,
            archive_status="archived",
            archived_at=datetime.now(),
            archived_by=1,
            archive_reason="测试归档",
            pre_archive_location="3F-M-R01-L1-G01",
        )
        db.add_all([complete, long_identity, long_location, archived])
        db.flush()
        db.add_all(
            [
                Product(
                    customer_id=1,
                    product_code="DIM-001",
                    customer_material_code="DIM-001",
                    product_name="二维模切件",
                    length_mm=Decimal("430.5"),
                    width_mm=Decimal("68"),
                    report_length_mm=880,
                    report_width_mm=425,
                    mold_tool_id=complete.id,
                ),
                Product(
                    customer_id=1,
                    product_code="DIM-002",
                    customer_material_code="DIM-002",
                    product_name="同尺寸模切件",
                    length_mm=Decimal("430.5"),
                    width_mm=Decimal("68"),
                    report_length_mm=880,
                    report_width_mm=None,
                    flute_type="B",
                    mold_tool_id=complete.id,
                ),
                Product(
                    customer_id=1,
                    product_code="LONG-001",
                    customer_material_code="LONG-001",
                    product_name="旧模具长标题",
                    length_mm=430,
                    width_mm=68,
                    report_length_mm=880,
                    report_width_mm=425,
                    mold_tool_id=long_identity.id,
                ),
                Product(
                    customer_id=1,
                    product_code="LOC-001",
                    customer_material_code="LOC-001",
                    product_name="旧位置长文本",
                    length_mm=430,
                    width_mm=68,
                    report_length_mm=880,
                    report_width_mm=425,
                    mold_tool_id=long_location.id,
                ),
                Product(
                    customer_id=1,
                    product_code="ARC-001",
                    customer_material_code="ARC-001",
                    product_name="归档模具",
                    length_mm=430,
                    width_mm=68,
                    report_length_mm=880,
                    report_width_mm=425,
                    mold_tool_id=archived.id,
                ),
            ]
        )
        db.commit()
        complete_id = complete.id
        long_identity_id = long_identity.id
        long_location_id = long_location.id
        archived_id = archived.id

    with TestClient(app) as client:
        _login(client, "workshop")
        label = client.get(f"/api/warehouse/molds/{complete_id}/label")
        assert label.status_code == 200, label.text
        assert label.json()["label_product_specification"] == "430.5 × 68"
        assert label.json()["label_report_specification"] == "多款见扫码"
        assert label.json()["label_flute_type"] == "多款见扫码"

        long_title = client.get(
            f"/api/warehouse/molds/{long_identity_id}/label"
        )
        assert long_title.status_code == 409, long_title.text
        assert "标签内容过长" in long_title.json()["detail"]
        assert "客户简称、标签名称和中文简写" in long_title.json()["detail"]

        manual_location = client.get(
            f"/api/warehouse/molds/{long_location_id}/label"
        )
        assert manual_location.status_code == 200, manual_location.text
        assert manual_location.json()["label_mold_number"] == "LOC001"

        inactive = client.get(f"/api/warehouse/molds/{archived_id}/label")
        assert inactive.status_code == 409, inactive.text
        assert "停用或归档" in inactive.json()["detail"]


def _material_live_context(
    db: Session,
    suffix: str,
    *,
    quantity: int = 10,
    material_status: str = "pending",
    supplier_order_number: str | None = None,
):
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.production import ProductionTask

    product = Product(
        customer_id=1,
        product_code=f"MAT-LIVE-{suffix}",
        customer_material_code=f"MAT-LIVE-{suffix}",
        product_name=f"收料聚合测试-{suffix}",
        box_category="die_cut",
        production_process="模切",
    )
    db.add(product)
    db.flush()
    order = Order(
        order_number=f"SO-MAT-LIVE-{suffix}",
        customer_id=1,
        order_date=date.today(),
        status="pending_production",
        total_amount=Decimal("0"),
    )
    db.add(order)
    db.flush()
    item = OrderItem(
        order_id=order.id,
        product_id=product.id,
        quantity=quantity,
        delivered_quantity=0,
        unit_price=Decimal("0"),
        subtotal=Decimal("0"),
        material_status=material_status,
        requisition_status="已入库" if material_status == "received" else "已报料",
        requisition_qty=quantity,
        supplier_order_number=supplier_order_number,
        snapshot_product_name=product.product_name,
        snapshot_product_code=product.product_code,
    )
    db.add(item)
    db.flush()
    task = ProductionTask(
        order_item_id=item.id,
        status="pending",
        planned_quantity=quantity,
        ordered_quantity_snapshot=quantity,
        material_received_quantity=0,
        material_input_quantity=0,
    )
    db.add(task)
    db.flush()
    return product, order, item, task


def _material_live_requisition(
    db: Session,
    *,
    item,
    suffix: str,
    quantity: int,
    requisition_status: str = "已报料",
    item_status: str = "有效",
):
    from app.models.requisition import Requisition, RequisitionItem

    requisition = Requisition(
        requisition_number=f"MR-MAT-LIVE-{suffix}",
        requisition_date=date.today(),
        status=requisition_status,
    )
    db.add(requisition)
    db.flush()
    row = RequisitionItem(
        requisition_id=requisition.id,
        order_item_id=item.id,
        requisition_qty=quantity,
        cardboard_len=Decimal("880"),
        cardboard_width=Decimal("425"),
        product_name_snapshot=f"收料来源-{suffix}",
        status=item_status,
    )
    db.add(row)
    db.flush()
    return requisition, row


def _material_live_receipt(
    db: Session,
    *,
    order,
    item,
    suffix: str,
    planned: int,
    received: int,
    requisition=None,
    requisition_item=None,
    status: str = "posted",
    parent_status: str | None = None,
    resolution_action: str | None = None,
):
    from app.core.time_contract import utc_now_naive
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem

    receipt = IncomingReceipt(
        receipt_number=f"IR-MAT-LIVE-{suffix}",
        status=parent_status or status,
        received_at=utc_now_naive(),
        idempotency_key=f"ir-mat-live-{suffix}",
    )
    db.add(receipt)
    db.flush()
    fact = IncomingReceiptItem(
        receipt_id=receipt.id,
        order_id=order.id,
        order_item_id=item.id,
        requisition_id=requisition.id if requisition is not None else None,
        requisition_item_id=(
            requisition_item.id if requisition_item is not None else None
        ),
        planned_quantity=planned,
        received_quantity=received,
        cumulative_received_quantity=received,
        variance_quantity=received - planned,
        variance_type=(
            "matched" if received == planned else "short" if received < planned else "over"
        ),
        resolution_status=(
            "resolved"
            if resolution_action == "accept_short"
            else "not_required"
            if received == planned
            else "pending"
        ),
        resolution_action=resolution_action,
        status=status,
    )
    db.add(fact)
    db.flush()
    return receipt, fact


def test_mold_live_status_reads_current_order_receipt_and_material_location(
    mold_app,
) -> None:
    from app.core.time_contract import utc_now_naive
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.models.master_data_object_version import MasterDataObjectVersion
    from app.models.mold_tool import MoldScanEvent, MoldTool
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.production import ProductionTask
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryReservation,
        WarehouseLocation,
    )
    import hashlib
    import json

    app, factory = mold_app
    now = utc_now_naive()
    with factory() as db:
        mold = MoldTool(
            mold_code="JCD-61452621",
            mold_name="模具联动测试客户61452621",
            rack_location="1F-M-R01-L2-G01",
        )
        db.add(mold)
        db.flush()
        product = Product(
            customer_id=1,
            product_code="61452621R1F",
            customer_material_code="61452621R1F",
            product_name="模切内盒",
            box_category="die_cut",
            production_process="模切",
            length_mm=430,
            width_mm=68,
            report_length_mm=880,
            report_width_mm=425,
            flute_type="E",
            mold_tool_id=mold.id,
        )
        db.add(product)
        db.flush()
        snapshot = json.dumps(
            {"mold_tool_id": mold.id},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        db.add(
            MasterDataObjectVersion(
                object_type="product",
                object_id=product.id,
                version=1,
                action="create",
                snapshot_schema_version=1,
                snapshot_json=snapshot,
                snapshot_sha256=hashlib.sha256(snapshot.encode()).hexdigest(),
                changed_fields_json="{}",
                change_set_id="mold-live-test",
                source="test",
                created_at=now,
            )
        )
        order = Order(
            order_number="SO-MOLD-LIVE-001",
            customer_id=1,
            order_date=date.today(),
            status="pending_production",
            total_amount=Decimal("0"),
        )
        db.add(order)
        db.flush()
        item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=1000,
            delivered_quantity=0,
            unit_price=Decimal("0"),
            subtotal=Decimal("0"),
            material_status="pending",
            requisition_qty=1000,
            snapshot_product_name="模切内盒",
            snapshot_product_code="61452621R1F",
            snapshot_spec="430 × 68",
        )
        db.add(item)
        db.flush()
        task = ProductionTask(
            order_item_id=item.id,
            status="pending",
            planned_quantity=1000,
            ordered_quantity_snapshot=1000,
            material_received_quantity=400,
            material_input_quantity=400,
            created_at=now,
        )
        db.add(task)
        location = WarehouseLocation(
            location_code="1F-RAW-01",
            location_name="一楼原料备料区",
            warehouse_type="semi_finished",
            is_active=True,
        )
        db.add(location)
        db.flush()
        lot = InventoryLot(
            lot_number="SF-MOLD-LIVE-001",
            inventory_type="semi_finished",
            warehouse_location_id=location.id,
            quantity_available=0,
            quantity_reserved=400,
            quantity_consumed=0,
            quantity_damaged=0,
            quantity_scrapped=0,
            unit="sheets",
            status="active",
            source_type="manual",
            stock_date=date.today(),
            stock_date_accuracy="exact",
            last_movement_at=now,
        )
        db.add(lot)
        db.flush()
        db.add(
            InventoryReservation(
                reservation_number="RSV-MOLD-LIVE-001",
                inventory_lot_id=lot.id,
                reservation_type="semi_order",
                order_id=order.id,
                order_item_id=item.id,
                reserved_stock_quantity=400,
                credited_requirement_quantity=400,
                consumed_stock_quantity=0,
                released_stock_quantity=0,
                consumed_requirement_quantity=0,
                released_requirement_quantity=0,
                status="active",
                reservation_group_key="MOLD-LIVE-001",
                idempotency_key="mold-live-reservation",
            )
        )
        receipt = IncomingReceipt(
            receipt_number="IN-MOLD-LIVE-001",
            status="posted",
            received_at=now,
            idempotency_key="mold-live-receipt",
        )
        db.add(receipt)
        db.flush()
        db.add(
            IncomingReceiptItem(
                receipt_id=receipt.id,
                order_id=order.id,
                order_item_id=item.id,
                planned_quantity=1000,
                received_quantity=400,
                cumulative_received_quantity=400,
                variance_quantity=-600,
                variance_type="short",
                resolution_status="pending",
                status="posted",
            )
        )
        db.commit()
        mold_id = mold.id
        task_id = task.id

    before = _protected_business_state(factory)
    with TestClient(app) as client:
        _login(client, "workshop")
        response = client.get(
            f"/api/warehouse/molds/live/{mold_id}"
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["mode"] == "current_orders"
        assert data["mold"]["label_identity"] == "模具联动测试客户61452621"
        task = data["current_orders"]["items"][0]
        assert task["production_task_id"] == task_id
        assert task["production_task_version"] == 1
        assert task["order_number"] == "SO-MOLD-LIVE-001"
        assert task["order_quantity"] == 1000
        assert task["product_code"] == "61452621R1F"
        assert task["specification"] == "430×68mm"
        assert task["material"]["state"] == "partially_received"
        assert task["material"]["received_quantity"] == 400
        assert task["material"]["locations"][0]["location_code"] == "1F-RAW-01"

        first_scan = client.post(
            f"/api/warehouse/molds/live/{mold_id}/scan-events",
            json={
                "production_task_id": task_id,
                "expected_mold_location_version": 1,
                "idempotency_key": "mold-live-scan-unique-001",
            },
        )
        assert first_scan.status_code == 200, first_scan.text
        assert first_scan.headers["cache-control"] == "private, no-store, max-age=0"
        assert first_scan.json()["replayed"] is False
        event = first_scan.json()["event"]
        assert event["production_task_id"] == task_id
        assert event["order_number"] == "SO-MOLD-LIVE-001"
        assert event["product_code"] == "61452621R1F"
        assert event["linkage_basis"] == "versioned_current_product_binding"
        assert event["mold_location"] == "1F-M-R01-L2-G01"
        assert event["scanned_by"] == "workshop"

        replay = client.post(
            f"/api/warehouse/molds/live/{mold_id}/scan-events",
            json={
                "production_task_id": task_id,
                "expected_mold_location_version": 1,
                "idempotency_key": "mold-live-scan-unique-001",
            },
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["replayed"] is True
        assert replay.json()["event"]["id"] == event["id"]

        conflicting_reuse = client.post(
            f"/api/warehouse/molds/live/{mold_id}/scan-events",
            json={
                "production_task_id": task_id + 999,
                "expected_mold_location_version": 1,
                "idempotency_key": "mold-live-scan-unique-001",
            },
        )
        assert conflicting_reuse.status_code == 409
        invalid_task = client.post(
            f"/api/warehouse/molds/live/{mold_id}/scan-events",
            json={
                "production_task_id": task_id + 999,
                "expected_mold_location_version": 1,
                "idempotency_key": "mold-live-scan-invalid-001",
            },
        )
        assert invalid_task.status_code == 409
        stale_location = client.post(
            f"/api/warehouse/molds/live/{mold_id}/scan-events",
            json={
                "production_task_id": task_id,
                "expected_mold_location_version": 2,
                "idempotency_key": "mold-live-scan-stale-location-001",
            },
        )
        assert stale_location.status_code == 409

        refreshed = client.get(f"/api/warehouse/molds/live/{mold_id}")
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["scan_history"]["total"] == 1
        assert refreshed.json()["scan_history"]["items"][0]["id"] == event["id"]
    with TestClient(app) as client:
        _login(client, "sales")
        denied = client.post(
            f"/api/warehouse/molds/live/{mold_id}/scan-events",
            json={
                "production_task_id": task_id,
                "expected_mold_location_version": 1,
                "idempotency_key": "mold-live-scan-denied-001",
            },
        )
        assert denied.status_code == 403
    with factory() as db:
        assert db.query(MoldScanEvent).count() == 1
    assert _protected_business_state(factory) == before


def test_mold_live_material_facts_keep_physical_sources_separate_and_accept_short(
    mold_app,
) -> None:
    from app.api.warehouse import _material_facts_for_task
    from app.core.time_contract import utc_now_naive
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.models.mold_tool import MoldTool
    from app.models.product import Product
    from app.models.order import Order, OrderItem
    from app.models.product_bom import (
        RequisitionItemBomSource,
        SalesOrderItemBomComponent,
    )
    from app.models.production import ProductionTask
    from app.models.requisition import Requisition, RequisitionItem

    _app, factory = mold_app
    now = utc_now_naive()
    with factory() as db:
        mold = MoldTool(
            mold_code="BOM-LIVE-MOLD",
            mold_name="组合模切组件模具",
            rack_location="1F-M-R01-L1-G01",
        )
        db.add(mold)
        db.flush()
        product = Product(
            customer_id=1,
            product_code="BOM-LIVE-COMP",
            customer_material_code="BOM-LIVE-COMP",
            product_name="组合模切组件",
            box_category="die_cut",
            production_process="模切",
            mold_tool_id=mold.id,
        )
        db.add(product)
        db.flush()
        order = Order(
            order_number="SO-BOM-LIVE-001",
            customer_id=1,
            order_date=date.today(),
            status="pending_production",
            total_amount=Decimal("0"),
        )
        db.add(order)
        db.flush()
        item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=10,
            delivered_quantity=0,
            unit_price=Decimal("0"),
            subtotal=Decimal("0"),
            material_status="pending",
            snapshot_product_name="组合模切组件",
            snapshot_product_code="BOM-LIVE-COMP",
        )
        db.add(item)
        db.flush()
        snapshot = SalesOrderItemBomComponent(
            sales_order_item_id=item.id,
            component_product_id=product.id,
            order_set_quantity=10,
            quantity_per_set=Decimal("1"),
            required_piece_quantity=Decimal("10"),
            display_order=1,
            internal_component_code="BOM-LIVE-COMP",
            is_die_cut=True,
            snapshot_mold_tool_id=mold.id,
            snapshot_mold_tool_code=mold.mold_code,
            snapshot_mold_tool_name=mold.mold_name,
            spare_sheet_quantity=0,
            display_mode="internal_only",
            is_required=True,
            snapshot_component_product_code=product.product_code,
            snapshot_component_product_name=product.product_name,
            snapshot_component_spec="430 × 68",
            snapshot_component_material="A=A",
            snapshot_component_box_category="die_cut",
        )
        db.add(snapshot)
        db.flush()
        task = ProductionTask(
            order_item_id=item.id,
            sales_order_item_bom_component_id=snapshot.id,
            task_role="component_internal",
            status="pending",
            planned_quantity=10,
            ordered_quantity_snapshot=10,
            material_received_quantity=18,
            material_input_quantity=18,
        )
        db.add(task)
        requisition = Requisition(
            requisition_number="MR-BOM-LIVE-001",
            requisition_date=date.today(),
            status="已报料",
        )
        db.add(requisition)
        db.flush()
        cover = RequisitionItem(
            requisition_id=requisition.id,
            order_item_id=item.id,
            requisition_qty=10,
            cardboard_len=Decimal("880"),
            cardboard_width=Decimal("425"),
            product_name_snapshot="组合模切组件-盖",
            status="有效",
        )
        base = RequisitionItem(
            requisition_id=requisition.id,
            order_item_id=item.id,
            requisition_qty=10,
            cardboard_len=Decimal("870"),
            cardboard_width=Decimal("415"),
            product_name_snapshot="组合模切组件-底",
            status="有效",
        )
        sibling = RequisitionItem(
            requisition_id=requisition.id,
            order_item_id=item.id,
            requisition_qty=99,
            cardboard_len=Decimal("999"),
            cardboard_width=Decimal("999"),
            product_name_snapshot="父产品普通报料",
            status="有效",
        )
        db.add_all([cover, base, sibling])
        db.flush()
        for row, kind in ((cover, "cover"), (base, "base")):
            db.add(
                RequisitionItemBomSource(
                    requisition_item_id=row.id,
                    sales_order_item_bom_component_id=snapshot.id,
                    component_type=kind,
                    order_set_quantity=10,
                    quantity_per_set=Decimal("1"),
                    required_piece_quantity=Decimal("10"),
                    demand_basis="order_sets",
                    spare_sheet_quantity=0,
                    calculated_purchase_quantity=Decimal("10"),
                )
            )
        receipt = IncomingReceipt(
            receipt_number="IR-BOM-LIVE-001",
            status="posted",
            received_at=now,
            idempotency_key="ir-bom-live-001",
        )
        db.add(receipt)
        db.flush()
        db.add_all(
            [
                IncomingReceiptItem(
                    receipt_id=receipt.id,
                    order_id=order.id,
                    order_item_id=item.id,
                    requisition_id=requisition.id,
                    requisition_item_id=cover.id,
                    planned_quantity=10,
                    received_quantity=8,
                    cumulative_received_quantity=8,
                    variance_quantity=-2,
                    variance_type="short",
                    resolution_status="resolved",
                    resolution_action="accept_short",
                    status="posted",
                ),
                IncomingReceiptItem(
                    receipt_id=receipt.id,
                    order_id=order.id,
                    order_item_id=item.id,
                    requisition_id=requisition.id,
                    requisition_item_id=base.id,
                    planned_quantity=10,
                    received_quantity=10,
                    cumulative_received_quantity=10,
                    variance_quantity=0,
                    variance_type="matched",
                    resolution_status="not_required",
                    status="posted",
                ),
                IncomingReceiptItem(
                    receipt_id=receipt.id,
                    order_id=order.id,
                    order_item_id=item.id,
                    requisition_id=requisition.id,
                    requisition_item_id=sibling.id,
                    planned_quantity=99,
                    received_quantity=99,
                    cumulative_received_quantity=99,
                    variance_quantity=0,
                    variance_type="matched",
                    resolution_status="not_required",
                    status="posted",
                ),
            ]
        )
        db.flush()
        material = _material_facts_for_task(db, task=task, item=item)

    assert material["state"] == "received_accept_short"
    assert material["planned_quantity"] == 20
    assert material["received_quantity"] == 18
    assert material["remaining_quantity"] == 0
    assert material["physical_shortage_quantity"] == 2
    assert material["quantity_unit"] == "sheets"


def test_mold_live_material_facts_fail_closed_on_direct_and_requisition_conflict(
    mold_app,
) -> None:
    from app.api.warehouse import _material_facts_for_task

    _app, factory = mold_app
    with factory() as db:
        _product, order, item, task = _material_live_context(
            db, "DIRECT-CONFLICT", quantity=10
        )
        requisition, requisition_item = _material_live_requisition(
            db,
            item=item,
            suffix="DIRECT-CONFLICT",
            quantity=10,
        )
        _material_live_receipt(
            db,
            order=order,
            item=item,
            suffix="DIRECT-CONFLICT-REQ",
            planned=10,
            received=4,
            requisition=requisition,
            requisition_item=requisition_item,
        )
        _material_live_receipt(
            db,
            order=order,
            item=item,
            suffix="DIRECT-CONFLICT-OLD",
            planned=10,
            received=6,
        )

        material = _material_facts_for_task(db, task=task, item=item)

    assert material["state"] == "source_conflict"
    assert material["receipt_source"] == "conflicting_sources"
    assert material["planned_quantity"] is None
    assert material["received_quantity"] is None
    assert material["remaining_quantity"] is None
    assert material["physical_shortage_quantity"] is None
    assert "来源冲突" in material["warning"]


def test_mold_live_material_facts_keep_latest_supplier_source_and_posted_history(
    mold_app,
) -> None:
    from app.api.warehouse import _material_facts_for_task
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    _app, factory = mold_app
    with factory() as db:
        product, order, item, task = _material_live_context(
            db,
            "SUPPLIER-LINEAGE",
            quantity=12,
            supplier_order_number="SRO-MAT-LIVE-001",
        )
        old_req, old_item = _material_live_requisition(
            db,
            item=item,
            suffix="SUPPLIER-OLD",
            quantity=99,
            requisition_status="supplier_requisition_created",
            item_status="supplier_requisition_created",
        )
        current_req, _current_item = _material_live_requisition(
            db,
            item=item,
            suffix="SUPPLIER-CURRENT",
            quantity=12,
            requisition_status="supplier_requisition_created",
            item_status="supplier_requisition_created",
        )
        supplier_order = SupplierRequisitionOrder(
            order_number=item.supplier_order_number,
            status="confirmed",
            total_quantity=12,
            requisition_qty=12,
        )
        db.add(supplier_order)
        db.flush()
        db.add(
            SupplierRequisitionOrderItem(
                supplier_order_id=supplier_order.id,
                order_item_id=item.id,
                product_id=product.id,
                quantity=12,
                requisition_qty=12,
            )
        )
        # A posted historical fact remains authoritative even when its source is
        # no longer the current supplier group.
        _material_live_receipt(
            db,
            order=order,
            item=item,
            suffix="SUPPLIER-OLD-FACT",
            planned=5,
            received=5,
            requisition=old_req,
            requisition_item=old_item,
        )

        material = _material_facts_for_task(db, task=task, item=item)

    assert current_req.id > old_req.id
    assert material["state"] == "partially_received"
    assert material["planned_quantity"] == 17
    assert material["received_quantity"] == 5
    assert material["remaining_quantity"] == 12


def test_mold_live_material_facts_exclude_reversed_receipt(
    mold_app,
) -> None:
    from app.api.warehouse import _material_facts_for_task

    _app, factory = mold_app
    with factory() as db:
        _product, order, item, task = _material_live_context(
            db, "REVERSED", quantity=10
        )
        requisition, requisition_item = _material_live_requisition(
            db,
            item=item,
            suffix="REVERSED",
            quantity=10,
        )
        _material_live_receipt(
            db,
            order=order,
            item=item,
            suffix="REVERSED",
            planned=10,
            received=10,
            requisition=requisition,
            requisition_item=requisition_item,
            status="reversed",
            parent_status="reversed",
        )

        material = _material_facts_for_task(db, task=task, item=item)

    assert material["state"] == "not_received"
    assert material["planned_quantity"] == 10
    assert material["received_quantity"] == 0
    assert material["remaining_quantity"] == 10
    assert material["receipt_source"] == "no_receipt_fact"


def test_mold_live_material_facts_expose_known_legacy_received_quantity(
    mold_app,
) -> None:
    from app.api.warehouse import _material_facts_for_task

    _app, factory = mold_app
    with factory() as db:
        _product, _order, item, task = _material_live_context(
            db, "LEGACY", quantity=10, material_status="received"
        )
        _requisition, requisition_item = _material_live_requisition(
            db,
            item=item,
            suffix="LEGACY",
            quantity=8,
            item_status="已入库",
        )

        material = _material_facts_for_task(db, task=task, item=item)

    assert requisition_item.requisition_qty == 8
    assert material["state"] == "legacy_received"
    assert material["planned_quantity"] is None
    assert material["received_quantity"] == 8
    assert material["remaining_quantity"] is None
    assert material["physical_shortage_quantity"] is None
    assert "历史实收 8 张" in material["state_label"]


def test_mold_live_material_facts_mark_unattributed_parent_legacy_bom(
    mold_app,
) -> None:
    from app.api.warehouse import _material_facts_for_task
    from app.models.product_bom import SalesOrderItemBomComponent

    _app, factory = mold_app
    with factory() as db:
        product, _order, item, task = _material_live_context(
            db, "LEGACY-BOM", quantity=10, material_status="received"
        )
        snapshot = SalesOrderItemBomComponent(
            sales_order_item_id=item.id,
            component_product_id=product.id,
            order_set_quantity=10,
            quantity_per_set=Decimal("1"),
            required_piece_quantity=Decimal("10"),
            display_order=1,
            internal_component_code="LEGACY-BOM",
            is_die_cut=True,
            snapshot_mold_tool_id=1,
            snapshot_mold_tool_code="LEGACY-MOLD",
            snapshot_mold_tool_name="历史模具",
            spare_sheet_quantity=0,
            display_mode="internal_only",
            is_required=True,
            snapshot_component_product_code=product.product_code,
            snapshot_component_product_name=product.product_name,
            snapshot_component_box_category="die_cut",
        )
        db.add(snapshot)
        db.flush()
        task.sales_order_item_bom_component_id = snapshot.id
        task.task_role = "component_internal"
        db.flush()

        material = _material_facts_for_task(db, task=task, item=item)

    assert material["state"] == "legacy_received_unattributed"
    assert material["planned_quantity"] is None
    assert material["received_quantity"] is None
    assert material["remaining_quantity"] is None
    assert material["receipt_source"] == "legacy_status_unattributed"
    assert "无法归属" in material["warning"]


def test_mold_live_bom_ignores_superseded_whole_when_current_sources_are_split(
    mold_app,
) -> None:
    from app.api.warehouse import _material_facts_for_task
    from app.models.product_bom import (
        RequisitionItemBomSource,
        SalesOrderItemBomComponent,
    )

    _app, factory = mold_app
    with factory() as db:
        product, _order, item, task = _material_live_context(
            db, "BOM-SUPERSEDED", quantity=10
        )
        snapshot = SalesOrderItemBomComponent(
            sales_order_item_id=item.id,
            component_product_id=product.id,
            order_set_quantity=10,
            quantity_per_set=Decimal("1"),
            required_piece_quantity=Decimal("10"),
            display_order=1,
            internal_component_code="BOM-SUPERSEDED",
            is_die_cut=True,
            snapshot_mold_tool_id=1,
            snapshot_mold_tool_code="BOM-MOLD",
            snapshot_mold_tool_name="BOM模具",
            spare_sheet_quantity=0,
            display_mode="internal_only",
            is_required=True,
            snapshot_component_product_code=product.product_code,
            snapshot_component_product_name=product.product_name,
            snapshot_component_box_category="die_cut",
        )
        db.add(snapshot)
        db.flush()
        task.sales_order_item_bom_component_id = snapshot.id
        task.task_role = "component_internal"
        old_req, old_item = _material_live_requisition(
            db,
            item=item,
            suffix="BOM-SUPERSEDED-OLD",
            quantity=99,
            item_status="已取消",
        )
        cover_req, cover_item = _material_live_requisition(
            db,
            item=item,
            suffix="BOM-SUPERSEDED-COVER",
            quantity=6,
        )
        base_req, base_item = _material_live_requisition(
            db,
            item=item,
            suffix="BOM-SUPERSEDED-BASE",
            quantity=4,
        )
        for requisition_item, component_type, active_guard, quantity in (
            (old_item, "whole", None, 99),
            (cover_item, "cover", 1, 6),
            (base_item, "base", 1, 4),
        ):
            db.add(
                RequisitionItemBomSource(
                    requisition_item_id=requisition_item.id,
                    sales_order_item_bom_component_id=snapshot.id,
                    component_type=component_type,
                    active_guard=active_guard,
                    order_set_quantity=10,
                    quantity_per_set=Decimal("1"),
                    required_piece_quantity=Decimal("10"),
                    demand_basis="order_sets",
                    spare_sheet_quantity=0,
                    calculated_purchase_quantity=Decimal(quantity),
                )
            )
        db.flush()

        material = _material_facts_for_task(db, task=task, item=item)

    assert cover_req.id > old_req.id
    assert base_req.id > cover_req.id
    assert material["state"] == "not_received"
    assert material["planned_quantity"] == 10
    assert material["received_quantity"] == 0
    assert material["remaining_quantity"] == 10
    assert material["warning"] is None


def test_mold_live_material_facts_do_not_show_exhausted_or_finished_lot(
    mold_app,
) -> None:
    from app.api.warehouse import _material_locations_for_task
    from app.core.time_contract import utc_now_naive
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.production import ProductionTask
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryReservation,
        WarehouseLocation,
    )

    _app, factory = mold_app
    now = utc_now_naive()
    with factory() as db:
        product = Product(
            customer_id=1,
            product_code="LOCATION-GATE",
            customer_material_code="LOCATION-GATE",
            product_name="库位证据门禁",
        )
        db.add(product)
        db.flush()
        order = Order(
            order_number="SO-LOCATION-GATE",
            customer_id=1,
            order_date=date.today(),
            status="pending_production",
            total_amount=Decimal("0"),
        )
        db.add(order)
        db.flush()
        item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=10,
            delivered_quantity=0,
            unit_price=Decimal("0"),
            subtotal=Decimal("0"),
            material_status="pending",
            snapshot_product_name=product.product_name,
            snapshot_product_code=product.product_code,
        )
        db.add(item)
        db.flush()
        task = ProductionTask(
            order_item_id=item.id,
            status="waiting_material",
            planned_quantity=0,
            ordered_quantity_snapshot=10,
            material_received_quantity=0,
            material_input_quantity=0,
        )
        db.add(task)
        location = WarehouseLocation(
            location_code="1F-FIN-01",
            location_name="成品区",
            warehouse_type="finished",
            is_active=True,
        )
        db.add(location)
        db.flush()
        lot = InventoryLot(
            lot_number="FG-NOT-MATERIAL",
            inventory_type="finished",
            warehouse_location_id=location.id,
            quantity_available=0,
            quantity_reserved=5,
            quantity_consumed=0,
            quantity_damaged=0,
            quantity_scrapped=0,
            unit="boxes",
            status="active",
            source_type="manual",
            stock_date=date.today(),
            stock_date_accuracy="exact",
            last_movement_at=now,
        )
        db.add(lot)
        db.flush()
        db.add(
            InventoryReservation(
                reservation_number="RSV-NOT-MATERIAL",
                inventory_lot_id=lot.id,
                reservation_type="semi_order",
                order_id=order.id,
                order_item_id=item.id,
                reserved_stock_quantity=5,
                credited_requirement_quantity=5,
                consumed_stock_quantity=0,
                released_stock_quantity=0,
                consumed_requirement_quantity=5,
                released_requirement_quantity=0,
                status="partial",
                reservation_group_key="NOT-MATERIAL",
                idempotency_key="not-material",
            )
        )
        db.flush()
        assert _material_locations_for_task(db, task=task, item=item) == []


def test_mold_live_status_requires_login_and_customer_scope(mold_app) -> None:
    from app.models.customer import Customer
    from app.models.mold_tool import MoldTool
    from app.models.product import Product
    from app.models.user import User
    from app.models.access_control import UserCustomerScope
    from sqlalchemy import select

    app, factory = mold_app
    with factory() as db:
        denied_customer = Customer(
            customer_number=9915,
            customer_code="DENY-LIVE",
            name="扫码越权客户",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        db.add(denied_customer)
        db.flush()
        mold = MoldTool(
            mold_code="DENY-LIVE-001",
            mold_name="扫码越权客户001",
            rack_location="1F-M-R02-L1-G01",
        )
        db.add(mold)
        db.flush()
        db.add(
            Product(
                customer_id=denied_customer.id,
                product_code="DENY-LIVE-P001",
                customer_material_code="DENY-LIVE-P001",
                product_name="扫码越权产品",
                box_category="die_cut",
                production_process="模切",
                mold_tool_id=mold.id,
            )
        )
        sales = db.scalar(select(User).where(User.username == "sales"))
        sales.customer_access_mode = "selected"
        db.add(UserCustomerScope(user_id=sales.id, customer_id=1))
        db.commit()
        mold_id = mold.id

    with TestClient(app) as client:
        unauthenticated = client.get(
            f"/api/warehouse/molds/live/{mold_id}"
        )
        assert unauthenticated.status_code == 401
        for key, expected in {
            "cache-control": "private, no-store, max-age=0",
            "pragma": "no-cache",
            "x-robots-tag": "noindex, nofollow",
            "referrer-policy": "no-referrer",
        }.items():
            assert unauthenticated.headers[key] == expected
        assert "Cookie" in unauthenticated.headers["vary"]
        _login(client, "sales")
        denied = client.get(
            f"/api/warehouse/molds/live/{mold_id}"
        )
        assert denied.status_code == 404
        assert "DENY-LIVE-P001" not in denied.text
        assert denied.headers["cache-control"] == "private, no-store, max-age=0"
        assert denied.headers["pragma"] == "no-cache"


def test_scoped_customer_can_read_current_bom_snapshot_after_product_unbind(
    mold_app,
) -> None:
    from app.models.order import Order, OrderItem
    from app.models.mold_tool import MoldTool
    from app.models.product import Product
    from app.models.product_bom import SalesOrderItemBomComponent
    from app.models.production import ProductionTask
    from app.models.user import User
    from app.models.access_control import UserCustomerScope
    from sqlalchemy import select

    app, factory = mold_app
    with factory() as db:
        mold = MoldTool(
            mold_code="SNAPSHOT-UNBOUND-MOLD",
            mold_name="模具联动测试客户SNAP001",
            rack_location="1F-M-R01-L2-G01",
        )
        product = Product(
            customer_id=1,
            product_code="SNAPSHOT-UNBOUND-P",
            customer_material_code="SNAPSHOT-UNBOUND-P",
            product_name="已解绑快照组件",
            box_category="die_cut",
            production_process="模切",
        )
        db.add_all([mold, product])
        db.flush()
        order = Order(
            order_number="SO-SNAPSHOT-UNBOUND",
            customer_id=1,
            order_date=date.today(),
            status="pending_production",
            total_amount=Decimal("0"),
        )
        db.add(order)
        db.flush()
        item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=5,
            delivered_quantity=0,
            unit_price=Decimal("0"),
            subtotal=Decimal("0"),
            material_status="pending",
            snapshot_product_name=product.product_name,
            snapshot_product_code=product.product_code,
        )
        db.add(item)
        db.flush()
        snapshot = SalesOrderItemBomComponent(
            sales_order_item_id=item.id,
            component_product_id=product.id,
            order_set_quantity=5,
            quantity_per_set=Decimal("1"),
            required_piece_quantity=Decimal("5"),
            display_order=1,
            internal_component_code=product.product_code,
            is_die_cut=True,
            snapshot_mold_tool_id=mold.id,
            snapshot_mold_tool_code=mold.mold_code,
            snapshot_mold_tool_name=mold.mold_name,
            spare_sheet_quantity=0,
            display_mode="internal_only",
            is_required=True,
            snapshot_component_product_code=product.product_code,
            snapshot_component_product_name=product.product_name,
            snapshot_component_box_category="die_cut",
        )
        db.add(snapshot)
        db.flush()
        db.add(
            ProductionTask(
                order_item_id=item.id,
                sales_order_item_bom_component_id=snapshot.id,
                task_role="component_internal",
                status="pending",
                planned_quantity=5,
                ordered_quantity_snapshot=5,
                material_received_quantity=0,
                material_input_quantity=0,
            )
        )
        sales = db.scalar(select(User).where(User.username == "sales"))
        sales.customer_access_mode = "selected"
        db.add(UserCustomerScope(user_id=sales.id, customer_id=1))
        db.commit()
        mold_id = mold.id

    with TestClient(app) as client:
        _login(client, "sales")
        response = client.get(
            f"/api/warehouse/molds/live/{mold_id}"
        )
        assert response.status_code == 200, response.text
        assert response.json()["mode"] == "restricted"
        assert response.json()["task_visibility"] == "hidden_by_permission"
        assert response.json()["bindings"]["items"] == []


@pytest.mark.parametrize(
    ("location", "kind", "expected"),
    [
        ("3F-M-R02-L2-D03-P08", "flat", "前往三楼模具区，第2号货架，第2层、第3排"),
        ("3F-M-R01-L1-V-P12", "vertical", "前往三楼模具区，第1号货架，底层（第1层）竖放区"),
        ("二楼模具架 B-12", "manual", "前往“二楼模具架 B-12”"),
    ],
)
def test_mold_location_prompt_is_immediately_readable(
    location: str, kind: str, expected: str
) -> None:
    from app.services.mold_location import describe_mold_location

    result = describe_mold_location(location)
    assert result["kind"] == kind
    assert result["prompt"] == expected
    assert "原档案" not in result["prompt"]
    assert "核对模具编号和存货编码" not in result["prompt"]


@pytest.mark.parametrize(
    "location",
    [
        "1F-M-R01-L2-D01-P01",
        "1F-M-R01-L3-D01-P02",
        "1F-M-R02-L2-D01-P03",
        "1F-M-R03-L1-V-P04",
        "1F-M-R03-L2-D01-P05",
        "1F-M-R03-L3-D01-P06",
        "1F-M-R04-L1-V-P07",
    ],
)
def test_confirmed_one_floor_mold_locations_are_accepted(location: str) -> None:
    from app.services.mold_location import (
        describe_mold_location,
        normalize_mold_location_code,
    )

    normalized = normalize_mold_location_code(location.lower())
    guide = describe_mold_location(normalized)
    assert normalized == location
    assert "一楼模具区" in guide["prompt"]
    assert f"R{guide['rack']:02d}" in guide["prompt"]
    assert f"第{guide['level']}层" in guide["prompt"]
    assert "P" not in guide["prompt"]
    assert "不再代表从左到右固定顺序" not in guide["prompt"]


@pytest.mark.parametrize(
    "location",
    [
        "1F-M-R01-L1-D01-P01",
        "1F-M-R01-L1-V-P01",
        "1F-M-R02-L1-D01-P01",
        "1F-M-R02-L3-D01-P01",
        "1F-M-R03-L1-D01-P01",
        "1F-M-R04-L1-D01-P01",
        "1F-M-R04-L2-V-P01",
        "1F-M-R01-L2-D02-P01",
        "1F-M-R05-L1-V-P01",
    ],
)
def test_unconfirmed_one_floor_mold_locations_fail_closed(location: str) -> None:
    from app.services.mold_location import MoldLocationError, normalize_mold_location_code

    with pytest.raises(MoldLocationError):
        normalize_mold_location_code(location)


def test_one_floor_mold_location_options_are_read_only_and_employee_friendly(
    mold_app,
) -> None:
    app, _factory = mold_app
    with TestClient(app) as client:
        _login(client, "sales")
        response = client.get("/api/warehouse/molds/location-options")
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["position_order"] is None
        assert data["position_numbers_are_dynamic"] is False
        assert data["storage_rule"] == "rack_level_grid"
        assert [rack["rack_code"] for rack in data["racks"]] == [
            "R01",
            "R02",
            "R03",
            "R04",
        ]
        assert data["racks"][0]["location_depth"] == "grid"
        assert data["racks"][0]["levels"] == [
            {"level": 2, "kind": "flat", "grid_count": 1, "grids": [1]},
            {"level": 3, "kind": "flat", "grid_count": 1, "grids": [1]},
        ]
        assert data["racks"][3]["location_depth"] == "rack"
        assert data["racks"][3]["levels"] == []


def test_admin_can_preview_then_once_confirm_floor1_formal_candidates(
    mold_app,
) -> None:
    app, factory = mold_app
    from sqlalchemy import func, select

    from app.models.warehouse_inventory import (
        WarehouseArea,
        WarehouseFloor,
        WarehouseLocation,
    )

    with factory() as db:
        floor = WarehouseFloor(
            floor_code="1F",
            floor_name="一楼",
            floor_number=1,
            construction_status="enabled",
            planning_reference_pallet_capacity=34,
        )
        db.add(floor)
        db.flush()
        legacy = WarehouseArea(
            floor_id=floor.id,
            area_code="DISPATCH",
            area_name="未映射旧待发区",
            planned_location_count=1,
            planned_pallet_capacity=1,
            construction_status="enabled",
            capacity_review_status="pending",
            capacity_eligible=False,
        )
        db.add(legacy)
        db.add(
            WarehouseLocation(
                location_code="1F-DISPATCH-L001",
                location_name="旧待发区 001 号位",
                warehouse_type="finished",
                warehouse_floor=1,
                area_code="DISPATCH",
                storage_type="ground",
                sort_order=1,
                source_version="TWIN_V1",
                placement_status="placed",
            )
        )
        db.commit()

    with TestClient(app) as client:
        _login(client, "sales")
        denied = client.get("/api/warehouse/twin-layout/floors/1F/formal-candidates")
        assert denied.status_code == 403

    with TestClient(app) as client:
        _login(client, "admin")
        preview = client.get("/api/warehouse/twin-layout/floors/1F/formal-candidates")
        assert preview.status_code == 200, preview.text
        plan = preview.json()
        assert plan["candidate_count"] == 19
        assert plan["excluded_out_of_bounds_count"] == 3
        assert plan["long_term_pallet_capacity"] == 70
        assert plan["formal_location_count"] == 45
        assert plan["formal_state"]["archivable_legacy_area_count"] == 0
        dispatch_state = next(
            row
            for row in plan["formal_state"]["legacy_areas"]
            if row["area_code"] == "DISPATCH"
        )
        assert dispatch_state["action"] == "preserve_business_anchor"
        with factory() as db:
            assert db.scalar(select(func.count(WarehouseArea.id))) == 1
            assert db.scalar(select(func.count(WarehouseLocation.id))) == 1

        payload = {
            "expected_map_revision": plan["map_revision"],
            "expected_plan_fingerprint": plan["plan_fingerprint"],
            "expected_formal_state_fingerprint": plan["formal_state"]["fingerprint"],
            "operation_key": "api-floor1-candidates-confirm-0001",
            "confirmed": True,
        }
        confirmed = client.post(
            "/api/warehouse/twin-layout/floors/1F/formal-candidates/confirm",
            json=payload,
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["applied"] is True
        assert confirmed.json()["created_location_count"] == 45
        assert confirmed.json()["archived_legacy_area_count"] == 0
        with factory() as db:
            old_location = db.scalar(
                select(WarehouseLocation).where(
                    WarehouseLocation.location_code == "1F-DISPATCH-L001"
                )
            )
            assert old_location is not None and old_location.is_active is True

        replayed = client.post(
            "/api/warehouse/twin-layout/floors/1F/formal-candidates/confirm",
            json={**payload, "operation_key": "api-floor1-candidates-confirm-0002"},
        )
        assert replayed.status_code == 200, replayed.text
        assert replayed.json()["applied"] is False

        overlaid = client.get("/api/warehouse/twin-layout/floors/1F")
        assert overlaid.status_code == 200, overlaid.text
        by_code = {
            row["feature_code"]: row
            for row in overlaid.json()["features"]
            if row.get("feature_kind") == "zone"
        }
        assert by_code["ZONE-1F-FIN-001"]["erp_area_code"] == "FIN-001"
        assert by_code["ZONE-1F-FIN-001"]["formal_binding_status"] == "published"


@pytest.mark.parametrize(
    "location",
    [
        "1F-M-R01-L2-G01",
        "1F-M-R01-L3-G01",
        "1F-M-R02-L2-G01",
        "1F-M-R03-L1-G01",
        "1F-M-R03-L3-G01",
        "1F-M-R04",
    ],
)
def test_new_one_floor_mold_locations_do_not_record_left_to_right_order(
    location: str,
) -> None:
    from app.services.mold_location import (
        describe_mold_location,
        normalize_mold_location_code,
    )

    normalized = normalize_mold_location_code(location.lower())
    guide = describe_mold_location(normalized)
    assert normalized == location
    assert guide["position"] is None
    if guide["kind"] == "storage_grid":
        assert guide["prompt"].endswith(f"第{guide['level']}层、第{guide['grid']}排")
    assert "左右顺序" not in guide["prompt"]
    assert "拿取前" not in guide["prompt"]


@pytest.mark.parametrize(
    ("location", "feature_codes"),
    [
        ("1F-M-R01-L2-G01", ["ZONE-1F-MOLD-002"]),
        ("1F-M-R02-L2-G01", ["ZONE-1F-MOLD-002"]),
        ("1F-M-R03-L2-G01", ["ZONE-1F-MOLD-001"]),
        ("3F-M-R01-L1-D01-P01", []),
        ("旧模具架 A-03", []),
    ],
)
def test_mold_location_resolves_to_measured_map_area(
    location: str,
    feature_codes: list[str],
) -> None:
    from app.services.mold_location import mold_location_feature_codes

    assert mold_location_feature_codes(location) == feature_codes


def test_mold_location_falls_back_from_grid_to_level_then_rack(monkeypatch) -> None:
    from app.services import mold_location

    options = mold_location.one_floor_mold_location_options(
        {
            "racks": [
                {
                    "mold_rack_code": "R01",
                    "name": "无格号测试架",
                    "area_code": "ZONE-1F-MOLD-002",
                    "levels": 3,
                    "bays": 0,
                }
            ]
        }
    )
    rack1 = next(row for row in options if row["rack_code"] == "R01")
    rack4 = next(row for row in options if row["rack_code"] == "R04")
    assert rack1["location_depth"] == "level"
    assert [row["level"] for row in rack1["levels"]] == [2, 3]
    assert rack4["location_depth"] == "rack"

    monkeypatch.setattr(
        mold_location,
        "one_floor_mold_location_options",
        lambda: [rack1, rack4],
    )
    assert mold_location.normalize_mold_location_code("1f-m-r01-l2") == (
        "1F-M-R01-L2"
    )
    assert mold_location.normalize_mold_location_code("1f-m-r04") == "1F-M-R04"
    with pytest.raises(mold_location.MoldLocationError, match="未配置格数"):
        mold_location.normalize_mold_location_code("1f-m-r01-l2-g01")


def test_mold_location_options_use_each_published_level_cell_count(monkeypatch) -> None:
    from app.services import mold_location

    options = mold_location.one_floor_mold_location_options(
        {
            "racks": [
                {
                    "mold_rack_code": "R01",
                    "name": "逐层分格测试架",
                    "area_code": "ZONE-1F-MOLD-002",
                    "levels": 3,
                    "bays": 1,
                    "level_cell_counts": [9, 3, 0],
                }
            ]
        }
    )
    rack1 = next(row for row in options if row["rack_code"] == "R01")
    assert rack1["grid_count"] == 3
    assert rack1["levels"] == [
        {"level": 2, "kind": "flat", "grid_count": 3, "grids": [1, 2, 3]},
        {"level": 3, "kind": "flat", "grid_count": 0, "grids": []},
    ]

    monkeypatch.setattr(
        mold_location,
        "one_floor_mold_location_options",
        lambda: [rack1],
    )
    assert mold_location.normalize_mold_location_code("1f-m-r01-l2-g03") == (
        "1F-M-R01-L2-G03"
    )
    assert mold_location.normalize_mold_location_code("1f-m-r01-l3") == "1F-M-R01-L3"
    with pytest.raises(mold_location.MoldLocationError, match="该层已配置格数"):
        mold_location.normalize_mold_location_code("1f-m-r01-l2")
    with pytest.raises(mold_location.MoldLocationError, match="该层未配置格数"):
        mold_location.normalize_mold_location_code("1f-m-r01-l3-g01")


def test_mold_rack_structure_change_plans_invalid_positions_to_first_grid(
    mold_app,
) -> None:
    _app, factory = mold_app
    from app.models.mold_tool import MoldTool
    from app.services.mold_location import (
        mold_rack_layout_relocation_warnings,
        mold_rack_layout_usage_blockers,
        plan_mold_rack_layout_relocations,
    )

    with factory() as db:
        db.add_all(
            [
                MoldTool(
                    mold_code="GRID-USED-003",
                    mold_name="使用第三格的模具",
                    rack_location="1F-M-R01-L2-G03",
                    created_by=1,
                ),
                MoldTool(
                    mold_code="LEVEL-ONLY-001",
                    mold_name="只定位到层的历史模具",
                    rack_location="1F-M-R01-L3",
                    created_by=1,
                ),
                MoldTool(
                    mold_code="R04-RACK-ONLY-001",
                    mold_name="靠墙特大模具区模具",
                    rack_location="1F-M-R04",
                    created_by=1,
                ),
            ]
        )
        db.commit()

        reduced_layout = {
            "racks": [
                {
                    "id": "rack-r01",
                    "mold_rack_code": "R01",
                    "levels": 3,
                    "level_cell_counts": [0, 2, 1],
                }
            ]
        }
        relocations = plan_mold_rack_layout_relocations(
            db,
            reduced_layout,
        )
        assert len(relocations) == 1
        assert relocations[0].mold_code == "GRID-USED-003"
        assert relocations[0].from_location == "1F-M-R01-L2-G03"
        assert relocations[0].to_location == "1F-M-R01-L2-G01"
        assert "第2层只剩2格" in relocations[0].reason
        assert mold_rack_layout_usage_blockers(db, reduced_layout) == []
        assert mold_rack_layout_relocation_warnings(relocations) == [
            "R01 有1件模具的现位置将在发布后失效；"
            "发布时自动归入R01 第2层第1格，之后可逐件手动调整"
        ]

        expanded = plan_mold_rack_layout_relocations(
            db,
            {
                "racks": [
                    {
                        "id": "rack-r01",
                        "mold_rack_code": "R01",
                        "levels": 3,
                        "level_cell_counts": [0, 3, 4],
                    }
                ]
            },
        )
        assert expanded == []


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
        "搜索正式客户",
        "第一主标签客户",
        "模具标签名称",
        "中文简写（可不填）",
        "/api/warehouse/molds",
        "已绑定常用箱",
        "/api/warehouse/molds/binding-products",
        "moldBindingPanel",
        "pinyin-pro-3.26.0.js",
    ):
        assert marker in warehouse
    assert "moldPositionNumber" not in warehouse
    assert 'v-model="productForm.mold_tool_id"' in index
    assert "productUsesMold(productForm)" in index
    assert "productMoldError" in index
    assert "moldLocationText(item)" in index
    assert "模具：{{ moldLocationText(item) }}" in index


def test_desktop_mold_form_builds_confirmed_one_floor_location_codes() -> None:
    warehouse = Path("static/warehouse.html").read_text(encoding="utf-8")
    for marker in (
        'id="moldRackSelect"',
        'id="moldLevelSelect"',
        'id="moldGridSelect"',
        "/api/warehouse/molds/location-options",
        "function buildMoldRackLocation()",
        "1F-M-${rack.rack_code}-L${level.level}-G${positionCode(grid)}",
        "1F-M-${rack.rack_code}`",
        "左右顺序不记录",
    ):
        assert marker in warehouse
    assert 'id="moldRackLocation" required readonly' in warehouse
    assert "从左到右第几位" not in warehouse.split(
        '<section id="printingPlateSection"', 1
    )[0].split('<section id="moldSection"', 1)[1]


def test_mold_edit_can_atomically_move_to_another_published_level(mold_app) -> None:
    app, factory = mold_app
    from app.models.audit import OperationLog
    from app.models.mold_tool import MoldLocationMovement, MoldTool
    from sqlalchemy import func, select

    with factory() as db:
        mold = MoldTool(
            mold_code="MJ-EDIT-MOVE-001",
            mold_name="编辑移位测试模",
            rack_location="1F-M-R01-L2-G01",
            remarks="移动前",
        )
        db.add(mold)
        db.commit()
        mold_id = mold.id

    payload = {
        "mold_code": "MJ-EDIT-MOVE-001",
        "mold_name": "编辑移位测试模（已核对）",
        "rack_location": "1F-M-R01-L3-G01",
        "remarks": "从二层搬到三层",
        "expected_location_version": 1,
        "location_idempotency_key": "p1-57-edit-move-001",
        "physical_move_confirmed": True,
        "location_note": "模具档案编辑中确认实物移位",
    }
    with TestClient(app) as client:
        _login(client, "admin")
        metadata_only = client.put(
            f"/api/warehouse/molds/{mold_id}",
            json={
                "mold_code": "MJ-EDIT-MOVE-001",
                "mold_name": "编辑移位测试模（只改资料）",
                "rack_location": "1F-M-R01-L2-G01",
                "remarks": "只改资料不移位",
            },
        )
        assert metadata_only.status_code == 200, metadata_only.text
        assert metadata_only.json()["location_version"] == 1
        with factory() as db:
            assert db.scalar(select(func.count(MoldLocationMovement.id))) == 0

        moved = client.put(f"/api/warehouse/molds/{mold_id}", json=payload)
        assert moved.status_code == 200, moved.text
        assert moved.json()["rack_location"] == "1F-M-R01-L3-G01"
        assert moved.json()["location_version"] == 2
        assert moved.json()["mold_name"] == "编辑移位测试模（已核对）"
        assert moved.json()["remarks"] == "从二层搬到三层"

        replayed = client.put(f"/api/warehouse/molds/{mold_id}", json=payload)
        assert replayed.status_code == 200, replayed.text
        assert replayed.json()["location_version"] == 2

    with factory() as db:
        movement = db.scalar(select(MoldLocationMovement))
        assert movement is not None
        assert movement.from_location == "1F-M-R01-L2-G01"
        assert movement.to_location == "1F-M-R01-L3-G01"
        assert movement.expected_version == 1
        assert movement.resulting_version == 2
        assert db.scalar(select(func.count(MoldLocationMovement.id))) == 1
        assert db.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.description == "模具编辑确认位置移动"
            )
        ) == 1


def test_mold_edit_location_change_requires_confirmation_and_current_version(
    mold_app,
) -> None:
    app, factory = mold_app
    from app.models.mold_tool import MoldLocationMovement, MoldTool
    from sqlalchemy import func, select

    with factory() as db:
        mold = MoldTool(
            mold_code="MJ-EDIT-GUARD-001",
            mold_name="编辑移位门禁模",
            rack_location="1F-M-R01-L2-G01",
            remarks="原备注",
        )
        db.add(mold)
        db.commit()
        mold_id = mold.id

    base = {
        "mold_code": "MJ-EDIT-GUARD-001",
        "mold_name": "不应保存的新名称",
        "rack_location": "1F-M-R01-L3-G01",
        "remarks": "不应保存的新备注",
        "location_idempotency_key": "p1-57-edit-guard-001",
    }
    with TestClient(app) as client:
        _login(client, "admin")
        unconfirmed = client.put(
            f"/api/warehouse/molds/{mold_id}",
            json={**base, "expected_location_version": 1},
        )
        assert unconfirmed.status_code == 409
        assert "确认模具实物" in unconfirmed.json()["detail"]

        missing_version = client.put(
            f"/api/warehouse/molds/{mold_id}",
            json={**base, "physical_move_confirmed": True},
        )
        assert missing_version.status_code == 409
        assert "位置版本缺失" in missing_version.json()["detail"]

        stale = client.put(
            f"/api/warehouse/molds/{mold_id}",
            json={
                **base,
                "physical_move_confirmed": True,
                "expected_location_version": 2,
            },
        )
        assert stale.status_code == 409
        assert "位置版本已变化" in stale.json()["detail"]

    with factory() as db:
        mold = db.get(MoldTool, mold_id)
        assert mold is not None
        assert mold.rack_location == "1F-M-R01-L2-G01"
        assert mold.location_version == 1
        assert mold.mold_name == "编辑移位门禁模"
        assert mold.remarks == "原备注"
        assert db.scalar(select(func.count(MoldLocationMovement.id))) == 0


def test_mold_edit_rolls_back_location_and_metadata_when_audit_fails(
    mold_app,
    monkeypatch,
) -> None:
    app, factory = mold_app
    from app.api import warehouse
    from app.models.mold_tool import MoldLocationMovement, MoldTool
    from sqlalchemy import func, select

    with factory() as db:
        mold = MoldTool(
            mold_code="MJ-EDIT-ATOMIC-001",
            mold_name="原子保存测试模",
            rack_location="1F-M-R01-L2-G01",
            remarks="原备注",
        )
        db.add(mold)
        db.commit()
        mold_id = mold.id

    def fail_audit(*_args, **_kwargs) -> None:
        raise RuntimeError("fault injection after movement flush")

    monkeypatch.setattr(warehouse, "_append_mold_location_move_log", fail_audit)
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client, "admin")
        response = client.put(
            f"/api/warehouse/molds/{mold_id}",
            json={
                "mold_code": "MJ-EDIT-ATOMIC-001",
                "mold_name": "不应落库的新名称",
                "rack_location": "1F-M-R01-L3-G01",
                "remarks": "不应落库的新备注",
                "expected_location_version": 1,
                "location_idempotency_key": "p1-57-edit-atomic-001",
                "physical_move_confirmed": True,
            },
        )
        assert response.status_code == 500

    with factory() as db:
        mold = db.get(MoldTool, mold_id)
        assert mold is not None
        assert mold.rack_location == "1F-M-R01-L2-G01"
        assert mold.location_version == 1
        assert mold.mold_name == "原子保存测试模"
        assert mold.remarks == "原备注"
        assert db.scalar(select(func.count(MoldLocationMovement.id))) == 0


def test_desktop_mold_edit_location_requires_preview_confirm_and_reuses_attempt() -> None:
    warehouse = Path("static/warehouse.html").read_text(encoding="utf-8")
    for marker in (
        'id="moldEditLocationNotice"',
        "state.moldEditLocation",
        "markMoldLocationSelectionChanged",
        'setMoldLocationBuilderDisabled(false)',
        'api("/api/warehouse/molds/location-movement/preview"',
        "原位置：${state.moldEditLocation.originalLocation}",
        "新位置：${preview.target_location}",
        "physical_move_confirmed:true",
        "location_idempotency_key:createIdempotencyKey()",
        "attempt.requestPayload",
        "复用同一凭证核对",
        "已保存，但列表刷新失败",
    ):
        assert marker in warehouse


@pytest.mark.parametrize(
    ("target_location", "expected_kind", "expected_floor"),
    [
        ("3f-m-r02-l2-d03-p08", "flat", "3F"),
        ("3F-M-R01-L1-V-P12", "vertical", "3F"),
        ("1f-m-r01-l2-d01-p01", "flat", "1F"),
        ("1F-M-R03-L1-V-P04", "vertical", "1F"),
        ("1F-M-R04-L1-V-P07", "vertical", "1F"),
    ],
)
def test_double_code_move_is_versioned_idempotent_and_does_not_touch_business_data(
    mold_app,
    monkeypatch,
    target_location: str,
    expected_kind: str,
    expected_floor: str,
) -> None:
    app, factory = mold_app
    from app.api import warehouse
    from app.core.time_contract import utc_now_naive
    from app.models.mold_tool import MoldLocationMovement, MoldTool
    from app.models.order import Order
    from app.models.product import Product
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
                Product(
                    customer_id=1,
                    product_code="MOVE-LABEL-P001",
                    customer_material_code="MOVE-LABEL-P001",
                    product_name="双码移动标签测试产品",
                    length_mm=430,
                    width_mm=68,
                    report_length_mm=880,
                    report_width_mm=425,
                    mold_tool_id=mold.id,
                ),
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
        assert preview_data["target_location"].startswith(f"{expected_floor}-M-")
        assert preview_data["expected_version"] == 1
        assert preview_data["can_confirm"] is True

        confirmation = {
            **payload,
            "expected_version": preview_data["expected_version"],
            "idempotency_key": f"p116d2-{expected_floor}-{expected_kind}-{target_location[-3:]}",
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
            json={**confirmation, "idempotency_key": f"stale-{expected_floor}-{expected_kind}-0002"},
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


def test_move_allows_same_storage_category_but_rejects_invalid_stale_and_reused_requests(
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
        assert conflict_preview.json()["can_confirm"] is True
        assert conflict_preview.json()["occupancy_conflict"] is None
        assert conflict_preview.json()["co_located_count"] == 1
        assert conflict_preview.json()["co_located_molds"][0]["mold_code"] == "MJ-MOVE-B"
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
        assert occupied.status_code == 200, occupied.text
        assert occupied.json()["mold"]["rack_location"] == "3F-M-R02-L1-D01-P01"

        stale = client.post(
            "/api/warehouse/molds/location-movement/confirm",
            json={
                "mold_code": "MJ-MOVE-A",
                "target_location": "3F-M-R03-L1-D01-P01",
                "expected_version": 3,
                "idempotency_key": "stale-version-001",
                "source": "api",
            },
        )
        assert stale.status_code == 409

        same = client.post(
            "/api/warehouse/molds/location-movement/confirm",
            json={
                "mold_code": "MJ-MOVE-A",
                "target_location": "3f-m-r02-l1-d01-p01",
                "expected_version": 2,
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
            "expected_version": 2,
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
            {"expected_version": 3},
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
                expected_version=3,
                idempotency_key="        ",
                actor_id=None,
                source="api",
                note=None,
            )
        assert db.scalar(select(func.count(MoldLocationMovement.id))) == 2


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
        "/api/warehouse/molds/location-options",
        "oneFloorRack",
        "oneFloorLevel",
        "oneFloorGrid",
        "1F-M-${rack.rack_code}-L${level.level}-G${positionCode(grid)}",
        "左右顺序不记录",
        "warehouse.execute",
        "idempotency_key",
        "/mold-label.html?mold_id=",
        "/api/warehouse/molds?q=",
    ):
        assert marker in mobile
    assert "oneFloorPosition" not in mobile


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


def test_mold_management_frontend_has_live_refresh_search_unbind_and_paging(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    warehouse = (root / "static" / "warehouse.html").read_text(encoding="utf-8")
    index = (root / "static" / "index.html").read_text(encoding="utf-8")

    for marker in (
        'id="moldBindingCustomerKeyword"',
        'id="moldBindingCustomerCandidates"',
        'id="moldPrevPage"',
        'id="moldNextPage"',
        'id="moldPageLabel"',
        "removeMoldBinding(",
        "expected_version=${row.version}",
        "page_size=${state.moldPageSize}",
        "moldLocationOptionsLoadedAt",
        "loadMoldLocationOptions(true)",
        "Date.now()-Number(state.moldLocationOptionsLoadedAt||0)<15000",
    ):
        assert marker in warehouse
    assert "min-width:320px" not in warehouse
    assert "await this.ensureProductEditorOptions({refreshMolds:true})" in index
    assert '@search="searchMoldTools"' in index
    assert "params:{ q:keyword, limit:100 }" in index

    node = shutil.which("node")
    assert node, "Node.js is required for frontend syntax validation"
    scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>", warehouse, flags=re.DOTALL
        )
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "warehouse-inline.js"
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
    assert "1F-M-R01-L2-G01" in warehouse
    assert "打印标签" in warehouse
    assert "<script src=" not in mobile
    assert "print-recovery.js" in label


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


def test_batch_mold_labels_preserve_selection_order_and_are_read_only(
    mold_app,
    monkeypatch,
) -> None:
    from sqlalchemy import func, select

    from app.api import warehouse as warehouse_api
    from app.models.mold_tool import MoldTool
    from app.models.product import Product

    app, factory = mold_app
    monkeypatch.setattr(warehouse_api, "_lan_ip", lambda: "192.168.3.80")
    with TestClient(app) as client:
        _login(client, "admin")
        created = []
        for index in range(1, 4):
            response = client.post(
                "/api/warehouse/molds",
                json={
                    "mold_code": f"BATCH-{index:03d}",
                    "mold_name": f"批量标签模具 {index}",
                    "rack_location": f"3F-M-R01-L1-P{index:02d}",
                },
            )
            assert response.status_code == 201, response.text
            created.append(response.json())

        with factory() as db:
            for index, mold in enumerate(created, start=1):
                db.add(
                    Product(
                        customer_id=1,
                        product_code=f"BATCH-P-{index:03d}",
                        customer_material_code=f"BATCH-P-{index:03d}",
                        product_name=f"批量标签产品 {index}",
                        length_mm=430,
                        width_mm=68,
                        report_length_mm=880,
                        report_width_mm=425,
                        mold_tool_id=mold["id"],
                    )
                )
            db.commit()

        before = _protected_business_state(factory)
        response = client.get(
            "/api/warehouse/molds/labels",
            params={
                "mold_ids": (
                    f"{created[2]['id']},{created[0]['id']},{created[2]['id']}"
                )
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["count"] == 2
        assert [item["mold_code"] for item in body["items"]] == [
            created[2]["mold_code"],
            created[0]["mold_code"],
        ]
        assert response.headers["cache-control"] == "private, no-store, max-age=0"
        assert all(item["qr_data_url"].startswith("data:image/png;base64,") for item in body["items"])
        assert _protected_business_state(factory) == before
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(MoldTool)) == 3


def test_batch_mold_labels_fail_closed_for_changed_or_oversized_selection(
    mold_app,
) -> None:
    app, _factory = mold_app
    with TestClient(app) as client:
        _login(client, "admin")
        missing = client.get(
            "/api/warehouse/molds/labels",
            params={"mold_ids": "999999"},
        )
        assert missing.status_code == 404
        assert "重新选择" in missing.json()["detail"]

        oversized = client.get(
            "/api/warehouse/molds/labels",
            params={"mold_ids": ",".join(str(value) for value in range(1, 102))},
        )
        assert oversized.status_code == 422
        assert "最多打印 100 件" in oversized.json()["detail"]


def test_batch_mold_label_frontend_has_selection_sort_and_copy_controls() -> None:
    root = Path(__file__).resolve().parents[1]
    warehouse = (root / "static" / "warehouse.html").read_text(encoding="utf-8")
    label = (root / "static" / "mold-label.html").read_text(encoding="utf-8")

    assert 'id="moldSelectAll"' in warehouse
    assert 'id="moldBatchPrint"' in warehouse
    assert "openMoldBatchLabels" in warehouse
    assert "/mold-label.html?mold_ids=" in warehouse
    assert 'id="batchSort"' in label
    assert 'id="copyCount"' in label
    assert 'value="location"' in label
    assert "@page{size:40mm 30mm;margin:0}" in label
    assert "40×30 标签样式" in warehouse
    assert "一次最多打印 100 件模具" in Path(
        root / "app" / "api" / "warehouse.py"
    ).read_text(encoding="utf-8")
