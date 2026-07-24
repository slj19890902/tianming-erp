from __future__ import annotations

from collections.abc import Generator
import csv
from io import StringIO
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.auth import router as auth_router
from app.api.deps import get_db
from app.api.inventory_onboarding import (
    TEMPLATE_HEADERS,
    router as inventory_onboarding_router,
)
from app.core.database import create_sqlite_engine
from app.core.security import hash_password
from app.models import Base
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.inventory_onboarding_posting import InventoryOnboardingPosting
from app.models.material import Material
from app.models.product import Product
from app.models.user import User
from app.models.warehouse_inventory import (
    InventoryLocationMovement,
    InventoryLot,
    InventoryMovement,
    InventoryPallet,
    InventoryPalletItem,
    InventoryReservation,
    WarehouseLocation,
)


@pytest.fixture()
def posting_api_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    private_root = tmp_path / "private-uploads"
    database = tmp_path / "n081-small-batch-posting-api.sqlite3"
    monkeypatch.setenv("ERP_ENVIRONMENT", "test")
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    monkeypatch.setenv("ERP_FILE_STORAGE_DIR", str(private_root))
    monkeypatch.setenv(
        "ERP_SECRET_KEY",
        "n081-small-batch-posting-api-test-only-secret",
    )

    engine = create_sqlite_engine(database)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="n081-post-admin",
            password_hash=hash_password("123456"),
            role="admin",
            real_name="N081 入账管理员",
            must_change_password=False,
            customer_access_mode="all",
        )
        workshop = User(
            username="n081-post-workshop",
            password_hash=hash_password("123456"),
            role="workshop",
            real_name="N081 仓库员",
            must_change_password=False,
            customer_access_mode="all",
        )
        customer = Customer(
            customer_number=8191,
            customer_code="N081-POST-C",
            name="N081 正式入账测试客户",
            payment_term_days=0,
            credit_limit=0,
        )
        material = Material(
            code="K=A-POST",
            layer_count=5,
            flute_type="AB",
            is_active=True,
        )
        db.add_all([admin, workshop, customer, material])
        db.flush()
        product = Product(
            customer_id=customer.id,
            product_code="N081-POST-P001",
            customer_material_code="N081-POST-P001",
            product_name="N081 正式入账成品纸箱",
            box_category="normal",
            material_id=material.id,
            default_material_code=material.code,
            flute_type="AB",
            layer_count=5,
        )
        location = WarehouseLocation(
            location_code="E1-R91",
            location_name="三楼 E1-R91",
            warehouse_type="finished",
            warehouse_floor=3,
            area_code="E1",
            storage_type="ground",
            placement_status="placed",
            source_version="V11",
        )
        db.add_all([product, location])
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(
        inventory_onboarding_router,
        prefix="/api/warehouse",
    )

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, factory
    finally:
        engine.dispose()


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "123456"},
    )
    assert response.status_code == 200, response.text


def _inventory_csv() -> bytes:
    values = {
        "盘点日期": "2026-07-24",
        "盘点人": "API盘点员",
        "库存类型": "成品",
        "归属类型": "客户专用",
        "楼层": 3,
        "区域": "E1",
        "库位编码": "E1-R91",
        "栈板号": "STK-N081-POST-001",
        "客户编码": "N081-POST-C",
        "客户名称": "N081 正式入账测试客户",
        "存货编码": "N081-POST-P001",
        "产品名称": "N081 正式入账成品纸箱",
        "数量": 24,
        "单位": "boxes",
        "日期可信度": "unknown",
        "入库日期原文": "历史日期不明",
        "备注": "N081 一键正式入账 API 测试",
    }
    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=TEMPLATE_HEADERS)
    writer.writeheader()
    writer.writerow(values)
    return ("\ufeff" + stream.getvalue()).encode("utf-8")


def _prepare_submitted_batch(client: TestClient) -> dict[str, object]:
    imported = client.post(
        "/api/warehouse/inventory-onboarding/batches/import",
        files={
            "file": (
                "N081小批正式入账.csv",
                _inventory_csv(),
                "text/csv",
            )
        },
    )
    assert imported.status_code == 201, imported.text
    batch = imported.json()["batch"]
    assert imported.json()["items"][0]["action_decision"] == "create_new"
    assert imported.json()["items"][0]["match_status"] == "ready"

    dry_run = client.post(
        (
            "/api/warehouse/inventory-onboarding/batches/"
            f"{batch['id']}/dry-run"
        ),
        json={"expected_version": batch["version"]},
    )
    assert dry_run.status_code == 200, dry_run.text
    dry_batch = dry_run.json()["batch"]
    assert dry_batch["dry_run_summary"]["error_count"] == 0

    submitted = client.post(
        (
            "/api/warehouse/inventory-onboarding/batches/"
            f"{batch['id']}/submit"
        ),
        json={
            "expected_version": dry_batch["version"],
            "dry_run_fingerprint": dry_batch["dry_run_fingerprint"],
            "idempotency_key": "n081-small-batch-submit-api",
            "confirmed": True,
        },
    )
    assert submitted.status_code == 200, submitted.text
    submitted_batch = submitted.json()["batch"]
    assert submitted_batch["status"] == "submitted"
    assert submitted_batch["posting"] is None
    assert submitted_batch["postable_summary"] == {
        "line_count": 1,
        "finished_line_count": 1,
        "semi_finished_line_count": 0,
        "pallet_count": 1,
        "floor": 3,
        "area_code": "E1",
        "max_line_count": 500,
        "can_post": True,
    }
    return submitted_batch


def _formal_counts(factory: sessionmaker) -> tuple[int, ...]:
    with factory() as db:
        return tuple(
            int(db.scalar(select(func.count(model.id))) or 0)
            for model in (
                InventoryLot,
                InventoryMovement,
                InventoryPallet,
                InventoryPalletItem,
                InventoryReservation,
                InventoryOnboardingPosting,
            )
        )


def test_post_requires_stocktake_review_permission(posting_api_app) -> None:
    app, factory = posting_api_app
    path = "/api/warehouse/inventory-onboarding/batches/1/post"
    with TestClient(app) as client:
        unauthenticated = client.post(path)
        assert unauthenticated.status_code == 401

        _login(client, "n081-post-workshop")
        forbidden = client.post(path)
        assert forbidden.status_code == 403

    assert _formal_counts(factory) == (0, 0, 0, 0, 0, 0)


def test_bodyless_one_click_post_creates_formal_stock_and_replays(
    posting_api_app,
) -> None:
    app, factory = posting_api_app
    with TestClient(app) as client:
        _login(client, "n081-post-admin")
        batch = _prepare_submitted_batch(client)
        path = (
            "/api/warehouse/inventory-onboarding/batches/"
            f"{batch['id']}/post"
        )

        before = _formal_counts(factory)
        assert before == (0, 0, 0, 0, 0, 0)

        # The production action is intentionally one bodyless click: permission
        # plus the frozen B1 evidence are the server-side authorization.
        posted = client.post(path)
        assert posted.status_code == 200, posted.text
        posted_batch = posted.json()["batch"]
        posting = posted_batch["posting"]
        assert posting["line_count"] == 1
        assert posting["finished_line_count"] == 1
        assert posting["semi_finished_line_count"] == 0
        assert posting["lot_count"] == 1
        assert posting["pallet_count"] == 1
        assert posting["movement_count"] == 1
        assert posting["posted_at"].endswith("Z")
        assert posted_batch["postable_summary"]["can_post"] is False
        assert _formal_counts(factory) == (1, 1, 1, 1, 0, 1)

        replay = client.post(path)
        assert replay.status_code == 200, replay.text
        assert replay.json()["batch"]["posting"]["id"] == posting["id"]
        assert replay.json()["batch"]["posting"]["posting_number"] == (
            posting["posting_number"]
        )
        assert _formal_counts(factory) == (1, 1, 1, 1, 0, 1)

    with factory() as db:
        lot = db.scalar(select(InventoryLot))
        movement = db.scalar(select(InventoryMovement))
        pallet = db.scalar(select(InventoryPallet))
        item = db.scalar(select(InventoryPalletItem))
        assert lot is not None
        assert movement is not None
        assert pallet is not None
        assert item is not None
        assert lot.source_type == "stocktake"
        assert lot.source_ref_type == "inventory_onboarding_line"
        assert lot.source_ref_id is not None
        assert lot.quantity_available == 24
        assert lot.unit == "boxes"
        assert lot.finished_detail is not None
        assert lot.finished_detail.owner_customer_id is not None
        assert lot.finished_detail.is_general is False
        assert movement.inventory_lot_id == lot.id
        assert movement.movement_type == "manual_in"
        assert item.inventory_lot_id == lot.id
        assert item.pallet_id == pallet.id
        assert pallet.pallet_code == "STK-N081-POST-001"
        assert pallet.is_current is True
        assert pallet.status == "active"
        assert (
            db.scalar(select(func.count(InventoryLocationMovement.id))) == 2
        )
        assert (
            db.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.action == "N081_POST"
                )
            )
            == 1
        )
