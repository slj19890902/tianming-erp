from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def resin_reuse_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.warehouse import router as warehouse_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "p1-44c.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="admin",
            password_hash=hash_password("RolePass123!"),
            role="admin",
            real_name="管理员",
            must_change_password=False,
        )
        workshop = User(
            username="workshop",
            password_hash=hash_password("RolePass123!"),
            role="workshop",
            real_name="生产",
            must_change_password=False,
            customer_access_mode="selected",
        )
        customer_a = Customer(
            customer_number=44031,
            customer_code="P144C-A",
            name="挂板旧版客户",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        customer_b = Customer(
            customer_number=44032,
            customer_code="P144C-B",
            name="挂板新版客户",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        db.add_all([admin, workshop, customer_a, customer_b])
        db.flush()
        db.add(
            UserCustomerScope(
                user_id=workshop.id,
                customer_id=customer_a.id,
                assigned_by=admin.id,
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
    yield app, factory
    engine.dispose()


def _login(client: TestClient, role: str = "admin") -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": role, "password": "RolePass123!"},
    )
    assert response.status_code == 200, response.text


def _create_plate(
    client: TestClient,
    *,
    position: int,
    level: int = 2,
    customer_id: int = 1,
) -> dict:
    response = client.post(
        "/api/warehouse/printing-plates",
        json={
            "customer_id": customer_id,
            "plate_name": f"旧版挂板 {position}",
            "color_name": "红色",
            "rack_location": f"1F-PL-R01-L{level}-P{position:02d}",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _preview_payload() -> dict:
    return {
        "target_customer_id": 2,
        "target_plate_name": "新版客户 220001 蓝色版",
        "target_color_name": "蓝色",
    }


def _confirm_payload(version: int, key: str = "p144c-reuse-0001") -> dict:
    return {
        **_preview_payload(),
        "expected_version": version,
        "idempotency_key": key,
        "old_resin_removed": True,
        "new_resin_mounted": True,
    }


def _create_frozen_task(factory, plate: dict) -> int:
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.production import ProductionTask

    with factory() as db:
        product = Product(
            customer_id=1,
            product_code="P144C-BOX",
            customer_material_code="P144C-MAT",
            product_name="历史生产任务纸箱",
            box_category="normal",
            printing_plate_mode="no_plate",
        )
        db.add(product)
        db.flush()
        order = Order(
            order_number="P144C-ORDER-001",
            customer_id=1,
            order_date=date(2026, 8, 12),
            status="pending_production",
            total_amount=Decimal("100.00"),
        )
        db.add(order)
        db.flush()
        item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=100,
            unit_price=Decimal("1.0000"),
            subtotal=Decimal("100.00"),
            material_status="received",
            snapshot_product_name=product.product_name,
            snapshot_product_code=product.product_code,
            supply_mode_snapshot="corrugated_production",
        )
        db.add(item)
        db.flush()
        task = ProductionTask(
            order_item_id=item.id,
            status="pending",
            planned_quantity=100,
            ordered_quantity_snapshot=100,
            printing_plate_mode_snapshot="plate",
            print_content_snapshot="红色印刷",
            printing_plate_codes_snapshot=json.dumps([plate["plate_code"]]),
            printing_plate_details_snapshot=json.dumps(
                [{"plate_code": plate["plate_code"], "color_name": "红色"}],
                ensure_ascii=False,
            ),
        )
        db.add(task)
        db.commit()
        return task.id


def test_resin_reuse_is_versioned_idempotent_and_preserves_frozen_tasks(
    resin_reuse_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.printing_plate import PrintingPlate, PrintingPlateResinReuse
    from app.models.production import ProductionTask

    app, factory = resin_reuse_app
    with TestClient(app) as client:
        _login(client)
        plate = _create_plate(client, position=1)
        task_id = _create_frozen_task(factory, plate)
        preview = client.post(
            f"/api/warehouse/printing-plates/{plate['id']}/resin-reuse/preview",
            json=_preview_payload(),
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["eligible"] is True
        assert preview.json()["binding_count"] == 0

        changed = client.post(
            f"/api/warehouse/printing-plates/{plate['id']}/resin-reuse/confirm",
            json=_confirm_payload(preview.json()["expected_version"]),
        )
        assert changed.status_code == 200, changed.text
        body = changed.json()
        assert body["idempotent_replay"] is False
        assert body["plate"]["plate_code"] == plate["plate_code"]
        assert body["plate"]["rack_location"] == plate["rack_location"]
        assert body["plate"]["location_version"] == plate["location_version"]
        assert body["plate"]["customer_id"] == 2
        assert body["plate"]["plate_name"] == "新版客户 220001 蓝色版"
        assert body["plate"]["color_name"] == "蓝色"
        assert body["plate"]["version"] == plate["version"] + 1
        assert body["reuse"]["from_customer_name"] == "挂板旧版客户"
        assert body["reuse"]["to_customer_name"] == "挂板新版客户"

        replay = client.post(
            f"/api/warehouse/printing-plates/{plate['id']}/resin-reuse/confirm",
            json=_confirm_payload(preview.json()["expected_version"]),
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True
        conflict_payload = _confirm_payload(preview.json()["expected_version"])
        conflict_payload["target_color_name"] = "绿色"
        conflict = client.post(
            f"/api/warehouse/printing-plates/{plate['id']}/resin-reuse/confirm",
            json=conflict_payload,
        )
        assert conflict.status_code == 409

        history = client.get(
            f"/api/warehouse/printing-plates/{plate['id']}/resin-reuses"
        )
        assert history.status_code == 200, history.text
        assert history.json()["total"] == 1
        searched = client.get(
            "/api/warehouse/printing-plates", params={"q": "挂板旧版客户"}
        )
        assert searched.status_code == 200, searched.text
        assert [row["id"] for row in searched.json()["items"]] == [plate["id"]]

    with factory() as db:
        current = db.get(PrintingPlate, plate["id"])
        task = db.get(ProductionTask, task_id)
        assert current is not None and current.customer_id == 2
        assert json.loads(task.printing_plate_codes_snapshot) == [plate["plate_code"]]
        assert json.loads(task.printing_plate_details_snapshot) == [
            {"plate_code": plate["plate_code"], "color_name": "红色"}
        ]
        assert db.scalar(select(func.count(PrintingPlateResinReuse.id))) == 1
        assert db.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.resource
                == f"warehouse/printing-plates/{plate['id']}/resin-reuse"
            )
        ) == 1


def test_reuse_requires_level_two_active_unbound_plate_and_admin(
    resin_reuse_app,
) -> None:
    from app.models.product import Product

    app, factory = resin_reuse_app
    with TestClient(app) as admin:
        _login(admin)
        bound = _create_plate(admin, position=2)
        with factory() as db:
            product = Product(
                customer_id=1,
                product_code="P144C-INACTIVE",
                customer_material_code="P144C-INACTIVE",
                product_name="已停用但仍绑定挂板",
                box_category="normal",
                printing_plate_mode="plate",
                printing_plate_1_id=bound["id"],
                is_active=False,
                deleted_at=datetime(2026, 8, 12),
            )
            db.add(product)
            db.commit()
        blocked = admin.post(
            f"/api/warehouse/printing-plates/{bound['id']}/resin-reuse/preview",
            json=_preview_payload(),
        )
        assert blocked.status_code == 200, blocked.text
        assert blocked.json()["eligible"] is False
        assert blocked.json()["binding_count"] == 1
        assert "仍有 1 个常用箱绑定" in "；".join(blocked.json()["blockers"])

        lower = _create_plate(admin, position=3, level=1)
        lower_preview = admin.post(
            f"/api/warehouse/printing-plates/{lower['id']}/resin-reuse/preview",
            json=_preview_payload(),
        )
        assert lower_preview.status_code == 200, lower_preview.text
        assert lower_preview.json()["eligible"] is False
        assert "货架第 2 层" in "；".join(lower_preview.json()["blockers"])
        lower_confirm = admin.post(
            f"/api/warehouse/printing-plates/{lower['id']}/resin-reuse/confirm",
            json=_confirm_payload(lower["version"], "p144c-lower-0001"),
        )
        assert lower_confirm.status_code == 409

        damaged = _create_plate(admin, position=4)
        changed = admin.put(
            f"/api/warehouse/printing-plates/{damaged['id']}/status",
            json={"expected_version": damaged["version"], "status": "damaged"},
        )
        assert changed.status_code == 200, changed.text
        damaged_preview = admin.post(
            f"/api/warehouse/printing-plates/{damaged['id']}/resin-reuse/preview",
            json=_preview_payload(),
        )
        assert damaged_preview.status_code == 200, damaged_preview.text
        assert damaged_preview.json()["eligible"] is False

    with TestClient(app) as workshop:
        _login(workshop, "workshop")
        cross_scope = workshop.post(
            f"/api/warehouse/printing-plates/{lower['id']}/resin-reuse/preview",
            json=_preview_payload(),
        )
        assert cross_scope.status_code == 403
        denied = workshop.post(
            f"/api/warehouse/printing-plates/{lower['id']}/resin-reuse/confirm",
            json=_confirm_payload(lower["version"], "p144c-denied-0001"),
        )
        assert denied.status_code == 403


def test_warehouse_page_explains_plate_rack_reuse_without_archive_semantics() -> None:
    source = (Path(__file__).resolve().parents[1] / "static" / "warehouse.html").read_text(
        encoding="utf-8"
    )
    section = source.split('<section id="printingPlateSection"', 1)[1].split(
        '<section id="stocktakeReviewSection"', 1
    )[0]
    assert "第 2 层专指挂板区货架层" in section
    assert "现场已撕除旧树脂版" in section
    assert "现场已贴好新树脂版" in section
    assert "换版历史" in source
    assert "/resin-reuse/preview" in source
    assert "/resin-reuse/confirm" in source
    assert "/resin-reuses?page=1&page_size=100" in source
    assert "废弃挂板" not in section
    assert "3F-M-ARCHIVE" not in section
