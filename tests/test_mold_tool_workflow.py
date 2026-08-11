from __future__ import annotations

from collections.abc import Generator
from datetime import date
from decimal import Decimal
from pathlib import Path
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
            if row["primary_code"] == "PAGE-017"
        )
        assert resource["feature_codes"] == ["ZONE-1F-MOLD-002"]
        assert resource["map_status"] == "mapped"


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

    with TestClient(app) as scoped_client:
        _login(scoped_client, "sales")
        listed = scoped_client.get(
            "/api/warehouse/molds",
            params={"include_inactive": True, "limit": 100},
        )
        assert listed.status_code == 200, listed.text
        by_code = {row["mold_code"]: row for row in listed.json()["items"]}
        assert "SCOPE-ALLOW" in by_code
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

        mapped_area = scoped_client.get(
            "/api/warehouse/molds/by-map-area",
            params={"feature_code": "ZONE-1F-MOLD-002", "page_size": 100},
        )
        assert mapped_area.status_code == 200, mapped_area.text
        assert {row["mold_code"] for row in mapped_area.json()["items"]} == {
            "SCOPE-ALLOW",
            "SCOPE-SHARED",
        }

        allowed_label = scoped_client.get(
            f"/api/warehouse/molds/{allowed_mold_id}/label"
        )
        assert allowed_label.status_code == 200, allowed_label.text
        denied_label = scoped_client.get(
            f"/api/warehouse/molds/{denied_mold_id}/label"
        )
        assert denied_label.status_code == 403, denied_label.text
        assert denied_label.json()["detail"] == "无客户访问权限"

        allowed_batch = scoped_client.get(
            "/api/warehouse/molds/labels",
            params={"mold_ids": f"{shared_mold_id},{allowed_mold_id}"},
        )
        assert allowed_batch.status_code == 200, allowed_batch.text
        assert [row["id"] for row in allowed_batch.json()["items"]] == [
            shared_mold_id,
            allowed_mold_id,
        ]
        assert [
            product["product_code"]
            for product in allowed_batch.json()["items"][0]["products"]
        ] == ["SHARED-ALLOW"]
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


def test_workshop_can_open_structured_location_label_and_qr(
    mold_app, monkeypatch
) -> None:
    app, factory = mold_app
    from app.api import warehouse
    from app.models.mold_tool import MoldTool
    from app.models.product import Product

    monkeypatch.setattr(warehouse, "_lan_ip", lambda: "192.168.3.80")
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
            "http://192.168.3.80:18045/mobile/mold-lookup?mold=MJ-MOBILE-001"
        )
        assert data["qr_data_url"].startswith("data:image/png;base64,")
        assert data["products"][0]["product_code"] == "MOBILE-P001"


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
        assert plan["formal_state"]["archivable_legacy_area_count"] == 1
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
        assert confirmed.json()["archived_legacy_area_count"] == 1
        with factory() as db:
            old_location = db.scalar(
                select(WarehouseLocation).where(
                    WarehouseLocation.location_code == "1F-DISPATCH-L001"
                )
            )
            assert old_location is not None and old_location.is_active is False

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
        "模具编号（系统自动生成）",
        "/api/warehouse/molds",
        "已绑定常用箱",
        "/api/warehouse/molds/code-preview",
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
        assert [item["id"] for item in body["items"]] == [
            created[2]["id"],
            created[0]["id"],
        ]
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
    assert "一次最多打印 100 件模具" in Path(
        root / "app" / "api" / "warehouse.py"
    ).read_text(encoding="utf-8")
