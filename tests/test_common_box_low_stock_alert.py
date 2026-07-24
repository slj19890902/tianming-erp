from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session, sessionmaker


def _add_finished_lot(
    db: Session,
    *,
    lot_number: str,
    product,
    location,
    quantity_available: int,
    quantity_reserved: int = 0,
    is_general: bool = False,
    status: str = "active",
) -> None:
    from app.models.warehouse_inventory import (
        FinishedGoodsInventoryDetail,
        InventoryLot,
    )

    lot = InventoryLot(
        lot_number=lot_number,
        inventory_type="finished",
        warehouse_location_id=location.id,
        quantity_available=quantity_available,
        quantity_reserved=quantity_reserved,
        quantity_consumed=0,
        quantity_damaged=0,
        quantity_scrapped=0,
        unit="boxes",
        status=status,
        source_type="stocktake",
        stock_date=date.today(),
        stock_date_accuracy="exact",
        last_movement_at=datetime.now(),
        version=1,
    )
    db.add(lot)
    db.flush()
    db.add(
        FinishedGoodsInventoryDetail(
            inventory_lot_id=lot.id,
            owner_customer_id=None if is_general else product.customer_id,
            owner_customer_name_snapshot=None if is_general else product.customer.name,
            is_general=is_general,
            product_id=product.id,
            inventory_code_snapshot=product.product_code,
            product_name_snapshot=product.product_name,
        )
    )


def _seed(factory: sessionmaker[Session]) -> dict[str, int]:
    from app.core.security import hash_password
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    from app.models.customer import Customer
    from app.models.product import Product
    from app.models.stock_replenishment import InventoryStockPolicy
    from app.models.user import User
    from app.models.warehouse_inventory import WarehouseLocation

    with factory() as db:
        admin = User(
            username="admin",
            password_hash=hash_password("123456"),
            role="admin",
            real_name="管理员",
            must_change_password=False,
        )
        scoped = User(
            username="scoped",
            password_hash=hash_password("123456"),
            role="sales",
            real_name="客户A文员",
            customer_access_mode="selected",
            must_change_password=False,
        )
        no_warehouse = User(
            username="no-warehouse",
            password_hash=hash_password("123456"),
            role="sales",
            real_name="无库存权限文员",
            customer_access_mode="selected",
            must_change_password=False,
        )
        customer_a = Customer(
            customer_number=9101,
            customer_code="A",
            name="匿名客户A",
            payment_term_days=0,
            credit_limit=0,
        )
        customer_b = Customer(
            customer_number=9102,
            customer_code="B",
            name="匿名客户B",
            payment_term_days=0,
            credit_limit=0,
        )
        db.add_all([admin, scoped, no_warehouse, customer_a, customer_b])
        db.flush()
        db.add_all(
            [
                UserCustomerScope(user_id=scoped.id, customer_id=customer_a.id),
                UserCustomerScope(
                    user_id=no_warehouse.id,
                    customer_id=customer_a.id,
                ),
                UserPermissionOverride(
                    user_id=scoped.id,
                    permission_code="requisition.view",
                    is_allowed=True,
                ),
                UserPermissionOverride(
                    user_id=scoped.id,
                    permission_code="warehouse.view",
                    is_allowed=True,
                ),
                UserPermissionOverride(
                    user_id=no_warehouse.id,
                    permission_code="requisition.view",
                    is_allowed=True,
                ),
            ]
        )
        product_a = Product(
            customer_id=customer_a.id,
            product_code="A-BOX",
            customer_material_code="A-BOX",
            product_name="匿名常用箱A",
            box_category="normal",
            is_active=True,
        )
        product_b = Product(
            customer_id=customer_b.id,
            product_code="B-BOX",
            customer_material_code="B-BOX",
            product_name="匿名常用箱B",
            box_category="normal",
            is_active=True,
        )
        standard = WarehouseLocation(
            location_code="FG-A01",
            location_name="成品A01",
            warehouse_type="finished",
            is_active=True,
        )
        floor3 = WarehouseLocation(
            location_code="E1-L09",
            location_name="三楼E1-L09",
            warehouse_type="finished",
            warehouse_floor=3,
            source_version="V11",
            placement_status="unplaced",
            is_active=True,
        )
        invalid_v11 = WarehouseLocation(
            location_code="BAD-V11",
            location_name="非三楼旧位置",
            warehouse_type="finished",
            warehouse_floor=2,
            source_version="V11",
            is_active=True,
        )
        db.add_all([product_a, product_b, standard, floor3, invalid_v11])
        db.flush()
        _add_finished_lot(
            db,
            lot_number="LOT-A-DEDICATED",
            product=product_a,
            location=standard,
            quantity_available=50,
            quantity_reserved=10,
        )
        _add_finished_lot(
            db,
            lot_number="LOT-A-GENERAL-F3",
            product=product_a,
            location=floor3,
            quantity_available=20,
            is_general=True,
        )
        _add_finished_lot(
            db,
            lot_number="LOT-A-INVALID-V11",
            product=product_a,
            location=invalid_v11,
            quantity_available=999,
        )
        _add_finished_lot(
            db,
            lot_number="LOT-A-FROZEN",
            product=product_a,
            location=standard,
            quantity_available=500,
            status="frozen",
        )
        policy_a = InventoryStockPolicy(
            policy_name="A成品预警",
            target_inventory_type="finished",
            product_id=product_a.id,
            customer_id=customer_a.id,
            warning_quantity=100,
            target_quantity=150,
            active=True,
            created_by=admin.id,
            updated_by=admin.id,
        )
        policy_b = InventoryStockPolicy(
            policy_name="B成品预警",
            target_inventory_type="finished",
            product_id=product_b.id,
            customer_id=customer_b.id,
            warning_quantity=10,
            target_quantity=20,
            active=True,
            created_by=admin.id,
            updated_by=admin.id,
        )
        db.add_all([policy_a, policy_b])
        db.commit()
        return {
            "admin": admin.id,
            "scoped": scoped.id,
            "no_warehouse": no_warehouse.id,
            "customer_a": customer_a.id,
            "product_a": product_a.id,
            "standard_location": standard.id,
            "policy_a": policy_a.id,
        }


def _factory(tmp_path: Path):
    from app.core.database import create_sqlite_engine
    from app.models import Base

    engine = create_sqlite_engine(tmp_path / "common-box-alert.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return engine, factory, _seed(factory)


def test_finished_stock_warning_uses_net_available_and_valid_floor3(
    tmp_path: Path,
) -> None:
    from app.models.stock_replenishment import InventoryStockPolicy
    from app.models.product import Product
    from app.models.warehouse_inventory import InventoryLot, WarehouseLocation
    from app.services.stock_replenishment import stock_policy_dict

    _engine, factory, ids = _factory(tmp_path)
    with factory() as db:
        policy = db.get(InventoryStockPolicy, ids["policy_a"])
        assert policy is not None
        summary = stock_policy_dict(db, policy)
        assert summary["available_quantity"] == 70
        assert summary["dedicated_available_quantity"] == 50
        assert summary["general_available_quantity"] == 20
        assert summary["reserved_quantity"] == 10
        assert summary["physical_unconsumed_quantity"] == 80
        assert summary["warning_triggered"] is True
        assert summary["suggested_replenishment_quantity"] == 80

        product = db.get(Product, ids["product_a"])
        location = db.get(WarehouseLocation, ids["standard_location"])
        assert product is not None and location is not None
        _add_finished_lot(
            db,
            lot_number="LOT-A-RECEIPT",
            product=product,
            location=location,
            quantity_available=30,
        )
        db.flush()
        assert stock_policy_dict(db, policy)["warning_triggered"] is False

        reserved_lot = db.scalar(
            select(InventoryLot).where(
                InventoryLot.lot_number == "LOT-A-DEDICATED"
            )
        )
        assert reserved_lot is not None
        reserved_lot.quantity_available -= 1
        reserved_lot.quantity_reserved += 1
        db.flush()
        assert stock_policy_dict(db, policy)["warning_triggered"] is True

        reserved_lot.quantity_available += 1
        reserved_lot.quantity_reserved -= 1
        db.flush()
        assert stock_policy_dict(db, policy)["warning_triggered"] is False


def test_dashboard_warning_is_read_only_permissioned_and_customer_scoped(
    tmp_path: Path,
) -> None:
    from app.api.dashboard import dashboard_overview
    from app.models.user import User

    engine, factory, ids = _factory(tmp_path)
    statements: list[str] = []

    def record_sql(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lstrip().lower())

    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        with factory() as db:
            scoped = db.get(User, ids["scoped"])
            overview = dashboard_overview(db, scoped)
        assert len(overview["low_stock_warnings"]) == 1
        assert overview["low_stock_warnings"][0]["product_code"] == "A-BOX"
        assert overview["low_stock_warnings"][0]["available_quantity"] == 70

        with factory() as db:
            no_warehouse = db.get(User, ids["no_warehouse"])
            overview = dashboard_overview(db, no_warehouse)
        assert "low_stock_warnings" not in overview
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)

    assert not any(
        statement.startswith(("insert ", "update ", "delete "))
        for statement in statements
    )


def test_quick_policy_update_only_changes_two_thresholds(tmp_path: Path) -> None:
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.requisition import router as requisition_router
    from app.models.stock_replenishment import (
        InventoryStockPolicy,
        StockReplenishmentOrder,
    )
    from app.models.warehouse_inventory import InventoryLot

    _engine, factory, ids = _factory(tmp_path)
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(requisition_router, prefix="/api/requisition")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    with factory() as db:
        lots_before = {
            row.id: (row.quantity_available, row.quantity_reserved, row.version)
            for row in db.scalars(select(InventoryLot)).all()
        }
        policy_count_before = int(
            db.scalar(select(func.count(InventoryStockPolicy.id))) or 0
        )

    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "123456"},
        )
        assert login.status_code == 200, login.text
        legacy_payload = {
            "policy_name": "旧入口重复保存测试",
            "target_inventory_type": "finished",
            "product_id": ids["product_a"],
            "warning_quantity": 40,
            "target_quantity": 100,
            "active": True,
        }
        first_legacy = client.post(
            "/api/requisition/stock-policies",
            json=legacy_payload,
        )
        second_legacy = client.post(
            "/api/requisition/stock-policies",
            json=legacy_payload,
        )
        assert first_legacy.status_code == 201, first_legacy.text
        assert second_legacy.status_code == 201, second_legacy.text
        assert first_legacy.json()["id"] == ids["policy_a"]
        assert second_legacy.json()["id"] == ids["policy_a"]

        response = client.put(
            f"/api/requisition/stock-policies/finished-products/{ids['product_a']}",
            json={"warning_quantity": 60, "target_quantity": 120},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["warning_quantity"] == 60
        assert body["target_quantity"] == 120
        assert body["available_quantity"] == 70
        assert body["warning_triggered"] is False

        invalid = client.put(
            f"/api/requisition/stock-policies/finished-products/{ids['product_a']}",
            json={"warning_quantity": 100, "target_quantity": 80},
        )
        assert invalid.status_code == 400

    with factory() as db:
        policy = db.get(InventoryStockPolicy, ids["policy_a"])
        assert policy is not None
        assert (policy.warning_quantity, policy.target_quantity) == (60, 120)
        assert int(db.scalar(select(func.count(InventoryStockPolicy.id))) or 0) == (
            policy_count_before
        )
        assert int(db.scalar(select(func.count(StockReplenishmentOrder.id))) or 0) == 0
        lots_after = {
            row.id: (row.quantity_available, row.quantity_reserved, row.version)
            for row in db.scalars(select(InventoryLot)).all()
        }
        assert lots_after == lots_before


def test_frontend_exposes_read_only_alert_and_two_number_setup() -> None:
    source = Path("static/index.html").read_text(encoding="utf-8")

    assert "常用箱低库存" in source
    assert "可用 {{ item.available_quantity }}" in source
    assert "建议补 {{ item.suggested_replenishment_quantity }}" in source
    assert "openProductStockPolicy(item)" in source
    assert "低于下限时首页提醒；这里只设置提醒，不会自动报料或入库。" in source
    assert (
        "/api/requisition/stock-policies/finished-products/${productId}" in source
    )
