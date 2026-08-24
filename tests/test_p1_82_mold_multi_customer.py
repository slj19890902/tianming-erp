from __future__ import annotations

from collections.abc import Generator
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def p182_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.products import router as products_router
    from app.api.warehouse import router as warehouse_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    from app.models.customer import Customer
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "p1-82.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        users = {
            role: User(
                username=role,
                password_hash=hash_password("RolePass123!"),
                role="admin" if role == "admin2" else role,
                real_name=role,
                display_name=role,
                customer_access_mode="selected" if role == "sales" else "all",
                must_change_password=False,
            )
            for role in ("admin", "admin2", "workshop", "sales")
        }
        db.add_all(users.values())
        customers = [
            Customer(
                customer_number=9820 + index,
                customer_code=code,
                name=name,
                chinese_short_name=short_name,
                payment_term_days=30,
                credit_limit=Decimal("100000"),
            )
            for index, (code, name, short_name) in enumerate(
                (
                    ("P182-TH", "苏州天华电子有限公司", "天华"),
                    ("P182-MD", "太仓明达机械有限公司", "明达"),
                    ("P182-RF", "常熟瑞丰食品有限公司", "瑞丰"),
                    ("P182-NS", "昆山未维护简称有限公司", None),
                ),
                start=1,
            )
        ]
        db.add_all(customers)
        db.flush()
        db.add(
            UserPermissionOverride(
                user_id=users["sales"].id,
                permission_code="warehouse.view",
                is_allowed=True,
            )
        )
        db.add(
            UserCustomerScope(
                user_id=users["sales"].id,
                customer_id=customers[0].id,
                assigned_by=users["admin"].id,
            )
        )
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(products_router, prefix="/api/master/products")
    app.include_router(warehouse_router, prefix="/api/warehouse")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    return app, factory


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "RolePass123!"},
    )
    assert response.status_code == 200, response.text


def _formal_payload(*, key: str, empty_short: bool = False) -> dict:
    return {
        "label_name": "92.5*36.5*1.8/2",
        "chinese_short_name": None if empty_short else "24*36中性内盒",
        "customers": [
            {"customer_id": 1, "display_order": 1},
            {"customer_id": 2, "display_order": 2},
            {"customer_id": 3, "display_order": None},
        ],
        "rack_location": "1F-M-R01-L2-G01",
        "remarks": "P1-82 匿名测试",
        "idempotency_key": key,
    }


def _single_customer_payload(*, customer_id: int, key: str, label_name: str) -> dict:
    return {
        "label_name": label_name,
        "chinese_short_name": None,
        "customers": [{"customer_id": customer_id, "display_order": 1}],
        "rack_location": "1F-M-R01-L2-G01",
        "remarks": None,
        "idempotency_key": key,
    }


def _bind_test_product(factory, mold_id: int, customer_id: int = 1) -> None:
    from app.models.product import Product

    with factory() as db:
        db.add(
            Product(
                customer_id=customer_id,
                product_code=f"P182-{customer_id}-PRODUCT",
                customer_material_code=f"P182-{customer_id}-MATERIAL",
                product_name="匿名模切内盒",
                mold_tool_id=mold_id,
                box_category="die_cut",
                production_process="模切",
                length_mm=Decimal("240"),
                width_mm=Decimal("360"),
                height_mm=Decimal("50"),
                report_length_mm=880,
                report_width_mm=425,
                flute_type="AB",
            )
        )
        db.commit()


def test_formal_multi_customer_identity_replays_and_qr_survives_rename(
    p182_app,
) -> None:
    app, factory = p182_app
    payload = _formal_payload(key="p182-create-stable-0001")
    with TestClient(app, base_url="http://testserver:18045") as client:
        _login(client, "admin")
        created = client.post("/api/warehouse/molds", json=payload)
        assert created.status_code == 201, created.text
        first = created.json()
        assert first["display_name"] == "天华/明达 92.5*36.5*1.8/2 24*36中性内盒"
        assert first["identity_status"] == "frozen"
        assert first["mold_code"].startswith("M-")
        assert [row["customer_id"] for row in first["associated_customers"]] == [1, 2, 3]
        assert [row["display_order"] for row in first["primary_customers"]] == [1, 2]

        replay = client.post("/api/warehouse/molds", json=payload)
        assert replay.status_code == 201, replay.text
        assert replay.json()["id"] == first["id"]
        assert replay.json()["idempotent_replay"] is True

        conflict_payload = {**payload, "label_name": "不同标签名称"}
        conflict = client.post("/api/warehouse/molds", json=conflict_payload)
        assert conflict.status_code == 409, conflict.text

        _bind_test_product(factory, first["id"])
        first_label = client.get(f"/api/warehouse/molds/{first['id']}/label")
        assert first_label.status_code == 200, first_label.text
        first_qr_url = first_label.json()["lookup_url"]
        assert first_label.json()["label_identity"] == first["display_name"]

        updated_payload = {
            **payload,
            "customers": [
                {"customer_id": 1, "display_order": 2},
                {"customer_id": 2, "display_order": 1},
                {"customer_id": 3, "display_order": None},
            ],
            "expected_version": first["version"],
            "idempotency_key": "p182-update-stable-0001",
        }
        updated = client.put(
            f"/api/warehouse/molds/{first['id']}", json=updated_payload
        )
        assert updated.status_code == 200, updated.text
        second = updated.json()
        assert second["id"] == first["id"]
        assert second["mold_code"] == first["mold_code"]
        assert second["rack_location"] == first["rack_location"]
        assert second["display_name"].startswith("明达/天华 ")
        assert second["version"] == first["version"] + 1

        second_label = client.get(f"/api/warehouse/molds/{first['id']}/label")
        assert second_label.status_code == 200, second_label.text
        assert second_label.json()["lookup_url"] == first_qr_url
        assert second_label.json()["label_identity"] == second["display_name"]

        late_create_replay = client.post("/api/warehouse/molds", json=payload)
        assert late_create_replay.status_code == 201, late_create_replay.text
        replayed_create = late_create_replay.json()
        assert replayed_create["id"] == first["id"]
        assert replayed_create["display_name"] == first["display_name"]
        assert replayed_create["version"] == first["version"]
        assert replayed_create["product_count"] == 0
        assert replayed_create["idempotent_replay"] is True

        late_update_replay = client.put(
            f"/api/warehouse/molds/{first['id']}", json=updated_payload
        )
        assert late_update_replay.status_code == 200, late_update_replay.text
        replayed_update = late_update_replay.json()
        assert replayed_update["display_name"] == second["display_name"]
        assert replayed_update["version"] == second["version"]
        assert replayed_update["idempotent_replay"] is True

        live = client.get(f"/api/warehouse/molds/live/{first['id']}")
        assert live.status_code == 200, live.text
        assert live.json()["mold"]["display_name"] == second["display_name"]
        assert "mold_code" not in live.json()["mold"]


def test_sequential_single_customer_creates_never_update_or_cross_link(
    p182_app,
) -> None:
    app, _factory = p182_app
    with TestClient(app) as client:
        _login(client, "admin")
        first = client.post(
            "/api/warehouse/molds",
            json=_single_customer_payload(
                customer_id=1,
                key="p182r-sequential-create-0001",
                label_name="连续新增模具 A",
            ),
        )
        second = client.post(
            "/api/warehouse/molds",
            json=_single_customer_payload(
                customer_id=2,
                key="p182r-sequential-create-0002",
                label_name="连续新增模具 B",
            ),
        )
        assert first.status_code == 201, first.text
        assert second.status_code == 201, second.text
        first_row = first.json()
        second_row = second.json()
        assert first_row["id"] != second_row["id"]
        assert [row["customer_id"] for row in first_row["associated_customers"]] == [
            1
        ]
        assert [row["customer_id"] for row in second_row["associated_customers"]] == [
            2
        ]

        first_search = client.get("/api/warehouse/molds", params={"q": "P182-TH"})
        second_search = client.get("/api/warehouse/molds", params={"q": "P182-MD"})
        assert first_search.status_code == 200, first_search.text
        assert second_search.status_code == 200, second_search.text
        assert [row["id"] for row in first_search.json()["items"]] == [first_row["id"]]
        assert [row["id"] for row in second_search.json()["items"]] == [
            second_row["id"]
        ]


def test_third_customer_search_blank_short_and_scoped_projection(p182_app) -> None:
    app, factory = p182_app
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post(
            "/api/warehouse/molds",
            json=_formal_payload(key="p182-create-search-0001", empty_short=True),
        )
        assert created.status_code == 201, created.text
        row = created.json()
        assert row["display_name"] == "天华/明达 92.5*36.5*1.8/2"
        assert "None" not in row["display_name"]
        assert "NULL" not in row["display_name"]
        _bind_test_product(factory, row["id"], customer_id=3)

        for params in (
            {"q": "常熟瑞丰食品有限公司"},
            {"q": "瑞丰"},
            {"q": "92.5*36.5"},
            {"q": "P182-3-PRODUCT"},
            {"q": "rf", "customer_ids": "3"},
        ):
            response = client.get("/api/warehouse/molds", params=params)
            assert response.status_code == 200, response.text
            assert [item["id"] for item in response.json()["items"]] == [row["id"]]

        _login(client, "sales")
        scoped = client.get(f"/api/warehouse/molds/{row['id']}/detail")
        assert scoped.status_code == 200, scoped.text
        scoped_row = scoped.json()
        assert [item["customer_id"] for item in scoped_row["associated_customers"]] == [1]
        assert "明达" not in scoped_row["display_name"]
        assert "瑞丰" not in scoped_row["display_name"]


def test_later_removed_customer_short_name_is_explicitly_marked_in_reads(
    p182_app,
) -> None:
    from app.models.customer import Customer

    app, factory = p182_app
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post(
            "/api/warehouse/molds",
            json=_formal_payload(key="p182-create-short-read-0001"),
        )
        assert created.status_code == 201, created.text
        mold_id = created.json()["id"]
        _bind_test_product(factory, mold_id, customer_id=1)
        with factory() as db:
            customer = db.get(Customer, 2)
            assert customer is not None
            customer.chinese_short_name = None
            db.commit()

        detail = client.get(f"/api/warehouse/molds/{mold_id}/detail")
        assert detail.status_code == 200, detail.text
        assert detail.json()["display_name"].startswith("天华/简称待完善 ")
        label = client.get(f"/api/warehouse/molds/{mold_id}/label")
        assert label.status_code == 200, label.text
        assert label.json()["label_identity"].startswith("天华/简称待完善 ")


def test_invalid_identity_stale_version_actor_and_audit_failure_are_atomic(
    p182_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import warehouse as warehouse_api
    from app.models.mold_tool import MoldMasterMutation, MoldTool, MoldToolCustomer
    from app.models.audit import OperationLog

    app, factory = p182_app

    def counts() -> tuple[int, int, int, int]:
        with factory() as db:
            return (
                int(db.scalar(select(func.count()).select_from(MoldTool)) or 0),
                int(db.scalar(select(func.count()).select_from(MoldToolCustomer)) or 0),
                int(db.scalar(select(func.count()).select_from(MoldMasterMutation)) or 0),
                int(db.scalar(select(func.count()).select_from(OperationLog)) or 0),
            )

    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client, "admin")
        baseline = counts()
        invalid_payloads = (
            {
                **_formal_payload(key="p182-invalid-dup-01"),
                "customers": [
                    {"customer_id": 1, "display_order": 1},
                    {"customer_id": 1, "display_order": 2},
                ],
            },
            {
                **_formal_payload(key="p182-invalid-fake-01"),
                "customers": [{"customer_id": 999999, "display_order": 1}],
            },
            {
                **_formal_payload(key="p182-invalid-short-01"),
                "customers": [{"customer_id": 4, "display_order": 1}],
            },
        )
        for payload in invalid_payloads:
            response = client.post("/api/warehouse/molds", json=payload)
            assert response.status_code in (409, 422), response.text
            assert counts() == baseline

        payload = _formal_payload(key="p182-create-atomic-0001")
        created = client.post("/api/warehouse/molds", json=payload)
        assert created.status_code == 201, created.text
        row = created.json()
        stable_counts = counts()

        stale = client.put(
            f"/api/warehouse/molds/{row['id']}",
            json={
                **payload,
                "expected_version": row["version"] + 1,
                "idempotency_key": "p182-stale-update-0001",
            },
        )
        assert stale.status_code == 409, stale.text
        assert counts() == stable_counts

        _login(client, "admin2")
        actor_baseline = counts()
        actor_conflict = client.post("/api/warehouse/molds", json=payload)
        assert actor_conflict.status_code == 409, actor_conflict.text
        assert counts() == actor_baseline

        _login(client, "admin")
        stable_counts = counts()
        original_display = row["display_name"]

        def fail_audit(*_args, **_kwargs):
            raise RuntimeError("P1-82 audit rollback injection")

        monkeypatch.setattr(warehouse_api, "append_audit_event", fail_audit)
        failed = client.put(
            f"/api/warehouse/molds/{row['id']}",
            json={
                **payload,
                "label_name": "审计失败不得保存",
                "expected_version": row["version"],
                "idempotency_key": "p182-audit-fail-0001",
            },
        )
        assert failed.status_code == 500, failed.text
        assert counts() == stable_counts
        detail = client.get(f"/api/warehouse/molds/{row['id']}/detail")
        assert detail.status_code == 200, detail.text
        assert detail.json()["display_name"] == original_display
        assert detail.json()["version"] == row["version"]


def test_formal_mold_customer_scope_cannot_be_bypassed_by_product_writer(
    p182_app,
) -> None:
    app, factory = p182_app
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post(
            "/api/warehouse/molds",
            json={
                **_formal_payload(key="p182-create-product-scope-01"),
                "customers": [{"customer_id": 1, "display_order": 1}],
            },
        )
        assert created.status_code == 201, created.text
        mold_id = created.json()["id"]

        common_payload = {
            "customer_material_code": "P182-SCOPE-BOX",
            "product_name": "匿名模切客户范围箱",
            "box_category": "die_cut",
            "production_process": "模切",
            "mold_tool_id": mold_id,
        }
        blocked = client.post(
            "/api/master/products",
            json={
                **common_payload,
                "customer_id": 2,
                "product_code": "P182-SCOPE-BLOCKED",
            },
        )
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["detail"]["code"] == "MOLD_CUSTOMER_NOT_ASSOCIATED"

        unbound = client.post(
            "/api/master/products",
            json={
                "customer_id": 2,
                "product_code": "P182-SCOPE-SYNC",
                "customer_material_code": "P182-SCOPE-SYNC",
                "product_name": "匿名同步模具客户范围箱",
                "box_category": "normal",
                "production_process": "开槽",
            },
        )
        assert unbound.status_code == 201, unbound.text
        sync_blocked = client.post(
            f"/api/master/products/{unbound.json()['id']}/sync-fields",
            json={
                "fields": {
                    "production_process": "模切",
                    "mold_tool_id": mold_id,
                },
                "expected_version": unbound.json()["version"],
            },
        )
        assert sync_blocked.status_code == 409, sync_blocked.text
        assert sync_blocked.json()["detail"]["code"] == "MOLD_CUSTOMER_NOT_ASSOCIATED"

        allowed = client.post(
            "/api/master/products",
            json={
                **common_payload,
                "customer_id": 1,
                "product_code": "P182-SCOPE-ALLOWED",
            },
        )
        assert allowed.status_code == 201, allowed.text

    from app.models.product import Product

    with factory() as db:
        rows = db.scalars(
            select(Product)
            .where(Product.product_code.like("P182-SCOPE-%"))
            .order_by(Product.product_code)
        ).all()
        assert [(row.product_code, row.customer_id) for row in rows] == [
            ("P182-SCOPE-ALLOWED", 1),
            ("P182-SCOPE-SYNC", 2),
        ]
        assert rows[1].mold_tool_id is None


def test_warehouse_binding_and_composite_bom_share_formal_customer_guard(
    p182_app,
) -> None:
    from app.models.product import Product
    from app.services.composite_bom import (
        CompositeBOMError,
        _validate_die_cut_mold,
    )

    app, factory = p182_app
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post(
            "/api/warehouse/molds",
            json={
                **_formal_payload(key="p182-create-binding-scope-01"),
                "customers": [{"customer_id": 1, "display_order": 1}],
            },
        )
        assert created.status_code == 201, created.text
        mold_id = created.json()["id"]

        with factory() as db:
            component = Product(
                customer_id=2,
                product_code="P182-SCOPE-COMPONENT",
                customer_material_code="P182-SCOPE-COMPONENT",
                product_name="匿名异客户模切组件",
                box_category="die_cut",
                production_process="模切",
                length_mm=Decimal("240"),
                width_mm=Decimal("360"),
                height_mm=Decimal("50"),
                report_length_mm=880,
                report_width_mm=425,
                flute_type="AB",
            )
            db.add(component)
            db.commit()
            component_id = component.id
            component_version = component.version

        warehouse_bind = client.post(
            f"/api/warehouse/molds/{mold_id}/product-bindings",
            json={
                "items": [
                    {
                        "product_id": component_id,
                        "expected_version": component_version,
                    }
                ]
            },
        )
        assert warehouse_bind.status_code == 409, warehouse_bind.text

        with factory() as db:
            component = db.get(Product, component_id)
            assert component is not None
            with pytest.raises(CompositeBOMError) as error:
                _validate_die_cut_mold(
                    db,
                    component,
                    position=1,
                    is_die_cut=True,
                    mold_tool_id=mold_id,
                )
            assert error.value.status_code == 409
            assert error.value.detail["code"] == "MOLD_CUSTOMER_NOT_ASSOCIATED"
            assert component.mold_tool_id is None
