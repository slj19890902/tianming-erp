from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def warehouse_filter_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.warehouse import router as warehouse_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    from app.models.customer import Customer
    from app.models.product import Product
    from app.models.user import User
    from app.models.warehouse_inventory import (
        InventoryPallet,
        InventoryPalletItem,
        SemiFinishedLotAllowedProduct,
        WarehouseLocation,
    )
    from app.services.warehouse_inventory import manual_finished_in, manual_semi_finished_in

    engine = create_sqlite_engine(tmp_path / "warehouse_lot_filters.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        admin = User(username="admin", password_hash=hash_password("RolePass123!"), role="admin", real_name="管理员", display_name="管理员", must_change_password=False)
        scoped = User(username="scoped", password_hash=hash_password("RolePass123!"), role="sales", real_name="范围用户", display_name="范围用户", must_change_password=False, customer_access_mode="selected")
        customer_a = Customer(customer_number=1, customer_code="TH", name="天华超净", payment_term_days=30, credit_limit=Decimal("0"))
        customer_b = Customer(customer_number=2, customer_code="MJ", name="明俊德", payment_term_days=30, credit_limit=Decimal("0"))
        session.add_all([admin, scoped, customer_a, customer_b])
        session.flush()
        session.add_all([
            UserPermissionOverride(user_id=scoped.id, permission_code="warehouse.view", is_allowed=True),
            UserCustomerScope(user_id=scoped.id, customer_id=customer_a.id),
        ])
        product_a = Product(customer_id=customer_a.id, product_code="TH-22000008", customer_material_code="TH-22000008", product_name="天华外箱", box_category="normal", length_mm=400, width_mm=300, height_mm=200)
        product_b = Product(customer_id=customer_b.id, product_code="MJ-001", customer_material_code="MJ-001", product_name="明俊德内盒", box_category="normal", length_mm=500, width_mm=400, height_mm=300)
        session.add_all([product_a, product_b])
        session.flush()
        finished_location = WarehouseLocation(location_code="F1-L01", location_name="一楼成品位", warehouse_type="finished", warehouse_floor=1, area_code="F1")
        semi_location = WarehouseLocation(location_code="S1-L01", location_name="一楼半成品位", warehouse_type="semi_finished", warehouse_floor=1, area_code="S1")
        session.add_all([finished_location, semi_location])
        session.flush()
        first = manual_finished_in(session, customer_id=customer_a.id, product_id=product_a.id, location_id=finished_location.id, quantity=10, stock_date=date(2026, 7, 1), source_type="manual", remarks=None, operator_id=admin.id, idempotency_key="p1-06-finished-a1")
        second = manual_finished_in(session, customer_id=customer_a.id, product_id=product_a.id, location_id=finished_location.id, quantity=5, stock_date=date(2026, 7, 2), source_type="manual", remarks=None, operator_id=admin.id, idempotency_key="p1-06-finished-a2")
        second.status = "frozen"
        semi_a = manual_semi_finished_in(session, location_id=semi_location.id, quantity=15, stock_date=date(2026, 7, 3), source_type="manual", material_code="MP-AB", layer_count=5, flute_type="AB", board_length_mm=575, board_width_mm=550, sheet_type="raw_board", supplier_name="鸣朋", customer_id=customer_a.id, crease_type=None, crease_left_mm=None, crease_middle_mm=None, crease_right_mm=None, cutting_note=None, remarks=None, operator_id=admin.id, idempotency_key="p1-06-semi-a")
        semi_b = manual_semi_finished_in(session, location_id=semi_location.id, quantity=20, stock_date=date(2026, 7, 4), source_type="manual", material_code="JL-BE", layer_count=5, flute_type="BE", board_length_mm=600, board_width_mm=500, sheet_type="raw_board", supplier_name="嘉林亿", customer_id=customer_b.id, crease_type=None, crease_left_mm=None, crease_middle_mm=None, crease_right_mm=None, cutting_note=None, remarks=None, operator_id=admin.id, idempotency_key="p1-06-semi-b")
        session.flush()
        session.add(
            SemiFinishedLotAllowedProduct(
                inventory_lot_id=semi_a.id,
                product_id=product_a.id,
                confirmed_by=admin.id,
                confirmed_at=datetime(2026, 7, 3, 9, 0, 0),
            )
        )
        pallet = InventoryPallet(pallet_code="PLT-TH-A", location_id=finished_location.id, created_by=admin.id)
        session.add(pallet)
        session.flush()
        session.add(InventoryPalletItem(pallet_id=pallet.id, inventory_lot_id=first.id, customer_id=customer_a.id, product_id=product_a.id, item_type="finished", quantity=10, unit="boxes", match_status="matched"))
        base = datetime(2026, 7, 20, 9, 0, 0)
        first.created_at, first.last_movement_at = base, base
        second.created_at, second.last_movement_at = base + timedelta(minutes=1), base + timedelta(minutes=1)
        semi_a.created_at, semi_a.last_movement_at = base + timedelta(minutes=2), base + timedelta(minutes=2)
        semi_b.created_at, semi_b.last_movement_at = base + timedelta(minutes=3), base + timedelta(minutes=3)
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(warehouse_router, prefix="/api/warehouse")

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    return app


def _login(client: TestClient, username: str) -> None:
    response = client.post("/api/auth/login", json={"username": username, "password": "RolePass123!"})
    assert response.status_code == 200, response.text


def _lots(response) -> list[dict]:
    assert response.status_code == 200, response.text
    return response.json()["items"]


def test_lot_filters_combine_finished_and_semi_fields(warehouse_filter_app) -> None:
    with TestClient(warehouse_filter_app) as client:
        _login(client, "admin")
        finished = client.get("/api/warehouse/lots", params={"customer_ids": 1, "inventory_type": "finished", "finished_product_code": "22000008", "finished_spec": "400×300×200", "location_keyword": "成品位", "pallet_keyword": "plt-th"})
        rows = _lots(finished)
        assert len(rows) == 1
        assert rows[0]["detail"]["inventory_code"] == "TH-22000008"
        assert finished.json()["filters"]["customer_ids"] == [1]
        assert finished.json()["sort"] == "last_movement_at_desc"

        semi = client.get("/api/warehouse/lots", params={"customer_ids": 1, "inventory_type": "semi_finished", "semi_supplier": "鸣朋", "semi_material_code": "mp-ab", "semi_flute_type": "ab", "semi_board_length_mm": 575, "semi_board_width_mm": 550, "semi_allowed_product": "22000008"})
        rows = _lots(semi)
        assert len(rows) == 1
        assert rows[0]["detail"]["supplier_name"] == "鸣朋"


def test_lot_pagination_is_stable_and_statuses_compose(warehouse_filter_app) -> None:
    with TestClient(warehouse_filter_app) as client:
        _login(client, "admin")
        first = client.get("/api/warehouse/lots", params={"customer_ids": 1, "page": 1, "page_size": 1})
        second = client.get("/api/warehouse/lots", params={"customer_ids": 1, "page": 2, "page_size": 1})
        assert _lots(first)[0]["inventory_type"] == "semi_finished"
        assert _lots(second)[0]["status"] == "frozen"
        assert first.json()["total"] == second.json()["total"] == 3
        frozen = client.get("/api/warehouse/lots", params=[("customer_ids", 1), ("statuses", "frozen"), ("statuses", "active")])
        assert len(_lots(frozen)) == 3


def test_lot_customer_scope_prevents_filter_based_disclosure(warehouse_filter_app) -> None:
    with TestClient(warehouse_filter_app) as client:
        _login(client, "scoped")
        assert _lots(client.get("/api/warehouse/lots", params={"semi_supplier": "嘉林亿"})) == []
        visible = _lots(client.get("/api/warehouse/lots", params={"customer_ids": 1}))
        assert len(visible) == 3
        forbidden = client.get("/api/warehouse/lots", params={"customer_ids": 2})
        assert forbidden.status_code == 403
