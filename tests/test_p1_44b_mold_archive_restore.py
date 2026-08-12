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
def archive_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.warehouse import router as warehouse_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "p1-44b.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add_all(
            [
                User(
                    username=role,
                    password_hash=hash_password("RolePass123!"),
                    role=role,
                    real_name=role,
                    must_change_password=False,
                )
                for role in ("admin", "boss", "workshop")
            ]
        )
        db.add(
            Customer(
                customer_number=4401,
                customer_code="P144B",
                name="模具封存测试客户",
                payment_term_days=30,
                credit_limit=Decimal("100000"),
            )
        )
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(warehouse_router, prefix="/api/warehouse")

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


def _create_mold(client: TestClient, code: str = "P144B-M-001") -> dict:
    response = client.post(
        "/api/warehouse/molds",
        json={
            "mold_code": code,
            "mold_name": "P1-44B 待复用模具",
            "rack_location": "1F-M-R01-L2-G01",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_archive_restore_is_atomic_idempotent_and_searchable(archive_app) -> None:
    from app.models.audit import OperationLog
    from app.models.mold_tool import MoldLocationMovement, MoldTool

    app, factory = archive_app
    with TestClient(app) as client:
        _login(client, "admin")
        mold = _create_mold(client)
        assert mold["archive_candidate"] == {
            "eligible": True,
            "reason": "unbound",
            "active_product_count": 0,
            "historical_product_count": 0,
        }
        payload = {
            "expected_version": mold["location_version"],
            "idempotency_key": "p144b-archive-0001",
            "reason": "unbound",
            "physical_move_confirmed": True,
        }
        archived = client.post(
            f"/api/warehouse/molds/{mold['id']}/archive", json=payload
        )
        assert archived.status_code == 200, archived.text
        body = archived.json()
        assert body["mold"]["archive_status"] == "archived"
        assert body["mold"]["is_active"] is False
        assert body["mold"]["rack_location"] == "3F-M-ARCHIVE-AB2-N"
        assert body["mold"]["location_guide"]["kind"] == "archive_area"
        assert "区域内待定位" in body["mold"]["location_guide"]["prompt"]
        assert body["mold"]["pre_archive_location"] == "1F-M-R01-L2-G01"
        assert body["movement"]["source"] == "archive"

        replay = client.post(
            f"/api/warehouse/molds/{mold['id']}/archive", json=payload
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True

        listing = client.get("/api/warehouse/molds", params={"q": "P144B-M-001"})
        assert listing.status_code == 200, listing.text
        assert listing.json()["items"][0]["archive_status"] == "archived"
        locator = client.get(
            "/api/warehouse/twin-operations/locate",
            params={"keyword": "P144B-M-001", "search_type": "mold"},
        )
        assert locator.status_code == 200, locator.text
        located = next(
            item for item in locator.json()["resources"] if item["kind"] == "mold"
        )
        assert located["map_status"] == "text_only"
        assert located["feature_codes"] == []
        assert "区域内待定位" in located["prompt"]

        bypass = client.put(f"/api/warehouse/molds/{mold['id']}/enable")
        assert bypass.status_code == 409
        assert "搬回" in bypass.json()["detail"]

        ordinary_disable = client.put(
            f"/api/warehouse/molds/{mold['id']}/disable"
        )
        assert ordinary_disable.status_code == 409
        assert "普通停用已合并" in ordinary_disable.json()["detail"]

        restored = client.post(
            f"/api/warehouse/molds/{mold['id']}/restore",
            json={
                "target_location": "1F-M-R01-L2-G01",
                "expected_version": body["mold"]["location_version"],
                "idempotency_key": "p144b-restore-0001",
                "physical_move_confirmed": True,
            },
        )
        assert restored.status_code == 200, restored.text
        restored_mold = restored.json()["mold"]
        assert restored_mold["archive_status"] == "active"
        assert restored_mold["is_active"] is True
        assert restored_mold["rack_location"] == "1F-M-R01-L2-G01"
        assert restored_mold["archived_at"] is None
        assert restored_mold["restored_at"] is not None

    with factory() as db:
        row = db.get(MoldTool, mold["id"])
        assert row is not None and row.location_version == 3
        assert db.scalar(
            select(func.count(MoldLocationMovement.id)).where(
                MoldLocationMovement.mold_tool_id == mold["id"]
            )
        ) == 2
        assert db.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.entity_type == "mold_tool",
                OperationLog.entity_id == mold["id"],
                OperationLog.action.in_(("ARCHIVE", "RESTORE")),
            )
        ) == 2


def test_candidate_requires_no_active_product_and_preserves_binding_history(
    archive_app,
) -> None:
    from app.models.product import Product

    app, factory = archive_app
    with TestClient(app) as client:
        _login(client, "admin")
        mold = _create_mold(client, "P144B-M-002")
        with factory() as db:
            product = Product(
                customer_id=1,
                product_code="P144B-BOX",
                customer_material_code="P144B-BOX",
                product_name="仍在生产的纸箱",
                mold_tool_id=mold["id"],
                is_active=True,
            )
            db.add(product)
            db.commit()
            product_id = product.id

        blocked = client.post(
            f"/api/warehouse/molds/{mold['id']}/archive",
            json={
                "expected_version": 1,
                "idempotency_key": "p144b-block-0001",
                "reason": "all_products_inactive",
                "physical_move_confirmed": True,
            },
        )
        assert blocked.status_code == 409
        assert "启用中的常用箱" in blocked.json()["detail"]

        with factory() as db:
            product = db.get(Product, product_id)
            product.is_active = False
            db.commit()

        refreshed = client.get(
            "/api/warehouse/molds", params={"q": "P144B-M-002", "include_inactive": True}
        ).json()["items"][0]
        assert refreshed["archive_candidate"]["reason"] == "all_products_inactive"
        archived = client.post(
            f"/api/warehouse/molds/{mold['id']}/archive",
            json={
                "expected_version": 1,
                "idempotency_key": "p144b-archive-0002",
                "reason": "all_products_inactive",
                "physical_move_confirmed": True,
            },
        )
        assert archived.status_code == 200, archived.text
        history = archived.json()["mold"]["binding_history"]
        assert history[0]["product_code"] == "P144B-BOX"
        assert history[0]["is_active"] is False


def test_archive_requires_permission_role_physical_confirmation_and_version(
    archive_app,
) -> None:
    app, _factory = archive_app
    with TestClient(app) as admin:
        _login(admin, "admin")
        mold = _create_mold(admin, "P144B-M-003")
        boss_mold = _create_mold(admin, "P144B-M-004")
        missing_confirmation = admin.post(
            f"/api/warehouse/molds/{mold['id']}/archive",
            json={
                "expected_version": 1,
                "idempotency_key": "p144b-confirm-0001",
                "reason": "unbound",
                "physical_move_confirmed": False,
            },
        )
        assert missing_confirmation.status_code == 422
        stale = admin.post(
            f"/api/warehouse/molds/{mold['id']}/archive",
            json={
                "expected_version": 2,
                "idempotency_key": "p144b-stale-0001",
                "reason": "unbound",
                "physical_move_confirmed": True,
            },
        )
        assert stale.status_code == 409

    with TestClient(app) as boss:
        _login(boss, "boss")
        accepted = boss.post(
            f"/api/warehouse/molds/{boss_mold['id']}/archive",
            json={
                "expected_version": 1,
                "idempotency_key": "p144b-boss-0001",
                "reason": "unbound",
                "physical_move_confirmed": True,
            },
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["mold"]["archived_by"] is not None

    with TestClient(app) as workshop:
        _login(workshop, "workshop")
        forbidden = workshop.post(
            f"/api/warehouse/molds/{mold['id']}/archive",
            json={
                "expected_version": 1,
                "idempotency_key": "p144b-denied-0001",
                "reason": "unbound",
                "physical_move_confirmed": True,
            },
        )
        assert forbidden.status_code == 403


def test_frontend_and_mobile_contract_keep_plate_rack_level_separate() -> None:
    root = Path(__file__).resolve().parents[1]
    warehouse = (root / "static" / "warehouse.html").read_text(encoding="utf-8")
    mobile = (root / "app" / "api" / "mobile_erp.py").read_text(encoding="utf-8")
    assert "三楼 AB2 北侧模具封存区（区域内待定位）" in warehouse
    assert "physical_move_confirmed:true" in warehouse
    assert "1F-PL-R01-L2-P01" in warehouse
    assert "挂板货架" in warehouse and "第 2 层" in warehouse
    assert "模具已封存待复用；必须先搬回一楼正式模具位" in mobile
    assert '"mold_archive_status"' in mobile
