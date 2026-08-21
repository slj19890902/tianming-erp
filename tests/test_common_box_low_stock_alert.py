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
    from app.models.material import Material
    from app.models.product import Product
    from app.models.stock_replenishment import InventoryStockPolicy
    from app.models.supplier import Supplier
    from app.models.user import User
    from app.models.warehouse_inventory import WarehouseLocation
    from app.services.supplier_master import normalize_supplier_identity

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
        other_customer_operator = User(
            username="other-customer",
            password_hash=hash_password("123456"),
            role="sales",
            real_name="客户B文员",
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
        db.add_all(
            [
                admin,
                scoped,
                no_warehouse,
                other_customer_operator,
                customer_a,
                customer_b,
                Supplier(
                    standard_name="匿名纸板供应商",
                    normalized_name=normalize_supplier_identity("匿名纸板供应商"),
                    display_name="匿名纸板供应商",
                    sort_order=10,
                    is_active=True,
                    version=1,
                ),
            ]
        )
        db.flush()
        db.add_all(
            [
                UserCustomerScope(user_id=scoped.id, customer_id=customer_a.id),
                UserCustomerScope(
                    user_id=no_warehouse.id,
                    customer_id=customer_a.id,
                ),
                UserCustomerScope(
                    user_id=other_customer_operator.id,
                    customer_id=customer_b.id,
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
                UserPermissionOverride(
                    user_id=no_warehouse.id,
                    permission_code="requisition.execute",
                    is_allowed=True,
                ),
                UserPermissionOverride(
                    user_id=other_customer_operator.id,
                    permission_code="requisition.view",
                    is_allowed=True,
                ),
                UserPermissionOverride(
                    user_id=other_customer_operator.id,
                    permission_code="requisition.execute",
                    is_allowed=True,
                ),
            ]
        )
        material_a = Material(
            code="A/K/B",
            supplier_name="匿名纸板供应商",
            layer_count=3,
            flute_type="B",
            is_active=True,
        )
        db.add(material_a)
        db.flush()
        product_a = Product(
            customer_id=customer_a.id,
            product_code="A-BOX",
            customer_material_code="A-BOX",
            product_name="匿名常用箱A",
            box_style="模切内盒",
            material_id=material_a.id,
            layer_count=3,
            flute_type="B",
            report_length_mm=600,
            report_width_mm=470,
            crease_type="净料",
            default_cutting_mode="一开二",
            manual_modified=True,
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
            "other_customer_operator": other_customer_operator.id,
            "customer_a": customer_a.id,
            "customer_b": customer_b.id,
            "product_a": product_a.id,
            "product_b": product_b.id,
            "material_a": material_a.id,
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


def test_finished_stock_warning_uses_current_warehouse_total_and_valid_floor3(
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
        assert summary["available_quantity"] == 80
        assert summary["allocatable_available_quantity"] == 70
        assert summary["dedicated_available_quantity"] == 60
        assert summary["general_available_quantity"] == 20
        assert summary["reserved_quantity"] == 10
        assert summary["physical_unconsumed_quantity"] == 80
        assert summary["warning_triggered"] is True
        assert summary["suggested_replenishment_quantity"] == 70

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
        assert stock_policy_dict(db, policy)["warning_triggered"] is False
        assert stock_policy_dict(db, policy)["available_quantity"] == 110
        assert stock_policy_dict(db, policy)["allocatable_available_quantity"] == 99

        reserved_lot.quantity_available += 1
        reserved_lot.quantity_reserved -= 1
        db.flush()
        assert stock_policy_dict(db, policy)["warning_triggered"] is False


def test_finished_stock_warning_aggregates_same_customer_inventory_code(
    tmp_path: Path,
) -> None:
    from app.models.product import Product
    from app.models.warehouse_inventory import WarehouseLocation
    from app.services.stock_replenishment import finished_product_quantity_summary

    _engine, factory, ids = _factory(tmp_path)
    with factory() as db:
        primary = db.get(Product, ids["product_a"])
        other_customer_product = db.get(Product, ids["product_b"])
        location = db.get(WarehouseLocation, ids["standard_location"])
        assert primary is not None and other_customer_product is not None
        assert location is not None
        duplicate = Product(
            customer_id=primary.customer_id,
            product_code=primary.product_code,
            customer_material_code=primary.customer_material_code,
            product_name="same-code historical master",
            box_category="normal",
            is_active=False,
        )
        other_customer_product.product_code = primary.product_code
        other_customer_product.customer_material_code = primary.product_code
        db.add(duplicate)
        db.flush()
        _add_finished_lot(
            db,
            lot_number="LOT-A-SAME-CODE-RESERVED",
            product=duplicate,
            location=location,
            quantity_available=0,
            quantity_reserved=1300,
        )
        _add_finished_lot(
            db,
            lot_number="LOT-B-SAME-CODE-OTHER-CUSTOMER",
            product=other_customer_product,
            location=location,
            quantity_available=900,
        )
        db.flush()

        summary = finished_product_quantity_summary(
            db,
            product_id=primary.id,
            customer_id=primary.customer_id,
        )
        assert summary["available_quantity"] == 1380
        assert summary["allocatable_available_quantity"] == 70
        assert summary["reserved_quantity"] == 1310
        assert summary["physical_unconsumed_quantity"] == 1380


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
        assert overview["low_stock_warnings"][0]["available_quantity"] == 80
        assert overview["low_stock_warnings"][0][
            "allocatable_available_quantity"
        ] == 70
        assert overview["low_stock_warnings"][0]["draft_ready"] is True
        assert overview["low_stock_warnings"][0][
            "theoretical_requisition_quantity"
        ] == 35
        assert overview["low_stock_warnings"][0][
            "suggested_new_requisition_sheet_quantity"
        ] == 35
        assert overview["low_stock_warnings"][0][
            "customer_board_preparation_available_sheet_quantity"
        ] == 0

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
        assert body["available_quantity"] == 80
        assert body["allocatable_available_quantity"] == 70
        assert body["warning_triggered"] is False

        zero_stock = client.put(
            f"/api/requisition/stock-policies/finished-products/{ids['product_b']}",
            json={"warning_quantity": 10, "target_quantity": 50},
        )
        assert zero_stock.status_code == 200, zero_stock.text
        zero_stock_body = zero_stock.json()
        assert zero_stock_body["available_quantity"] == 0
        assert zero_stock_body["warning_quantity"] == 10
        assert zero_stock_body["target_quantity"] == 50
        assert zero_stock_body["warning_triggered"] is True
        assert zero_stock_body["suggested_replenishment_quantity"] == 50

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


def test_warning_draft_prefills_customer_board_preparation_and_never_adds_finished_stock(
    tmp_path: Path,
) -> None:
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.incoming import router as incoming_router
    from app.api.orders import router as orders_router
    from app.api.production import router as production_router
    from app.api.requisition import router as requisition_router
    from app.api.warehouse import router as warehouse_router
    from app.models.customer import Customer
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.stock_replenishment import (
        InventoryStockPolicy,
        StockReplenishmentOrder,
        StockReplenishmentOrderItem,
    )
    from app.models.user import User
    from app.models.warehouse_inventory import InventoryLot
    from app.models.warehouse_inventory import (
        FinishedGoodsInventoryDetail,
        InventoryMovement,
        SemiFinishedLotAllowedProduct,
        WarehouseArea,
        WarehouseFloor,
        WarehouseLocation,
    )

    _engine, factory, ids = _factory(tmp_path)
    with factory() as db:
        customer = db.get(Customer, ids["customer_a"])
        admin = db.get(User, ids["admin"])
        assert customer is not None and admin is not None
        companion = Product(
            customer_id=customer.id,
            product_code="A-BOX-PRINT-B",
            customer_material_code="A-BOX-PRINT-B",
            product_name="匿名常用箱A另一印刷款",
            material_id=ids["material_a"],
            layer_count=3,
            flute_type="B",
            report_length_mm=600,
            report_width_mm=470,
            crease_type="净料",
            default_cutting_mode="一开二",
            manual_modified=True,
            box_category="normal",
            is_active=True,
        )
        db.add(companion)
        db.flush()
        companion_product_id = companion.id
        other_customer_companion = Product(
            customer_id=ids["customer_b"],
            product_code="B-BOX-SAME-BOARD",
            customer_material_code="B-BOX-SAME-BOARD",
            product_name="另一客户同规格纸板产品",
            material_id=ids["material_a"],
            layer_count=3,
            flute_type="B",
            report_length_mm=600,
            report_width_mm=470,
            crease_type="净料",
            default_cutting_mode="一开二",
            manual_modified=True,
            box_category="normal",
            is_active=True,
        )
        db.add(other_customer_companion)
        db.flush()
        other_customer_companion_id = other_customer_companion.id
        different_conversion = Product(
            customer_id=customer.id,
            product_code="A-BOX-ONE-UP",
            customer_material_code="A-BOX-ONE-UP",
            product_name="同客户同纸板但一开一",
            material_id=ids["material_a"],
            layer_count=3,
            flute_type="B",
            report_length_mm=600,
            report_width_mm=470,
            crease_type="净料",
            default_cutting_mode="一开一",
            manual_modified=True,
            box_category="normal",
            is_active=True,
        )
        db.add(different_conversion)
        db.flush()
        different_conversion_id = different_conversion.id
        db.add(
            InventoryStockPolicy(
                policy_name="A另一款成品预警",
                target_inventory_type="finished",
                product_id=companion.id,
                customer_id=customer.id,
                warning_quantity=20,
                target_quantity=60,
                active=True,
                created_by=admin.id,
                updated_by=admin.id,
            )
        )
        floor1 = WarehouseFloor(
            floor_code="1F",
            floor_name="Floor 1",
            floor_number=1,
            construction_status="enabled",
        )
        db.add(floor1)
        db.flush()
        db.add(
            WarehouseArea(
                floor_id=floor1.id,
                area_code="A1",
                area_name="A1 raw-material staging",
                construction_status="enabled",
            )
        )
        floor3 = WarehouseFloor(
            floor_code="3F",
            floor_name="Floor 3",
            floor_number=3,
            construction_status="enabled",
        )
        db.add(floor3)
        db.flush()
        db.add(
            WarehouseArea(
                floor_id=floor3.id,
                area_code="E1",
                area_name="E1 finished-goods area",
                construction_status="enabled",
            )
        )
        raw_staging = WarehouseLocation(
            location_code="1FA",
            location_name="Floor 1 A1 raw-material staging",
            warehouse_type="semi_finished",
            warehouse_floor=1,
            area_code="A1",
            storage_type="temporary_aisle",
            placement_status="placed",
            source_version="P1-36L",
            is_active=True,
        )
        semi_location = WarehouseLocation(
            location_code="SF-UAT-01",
            location_name="客户纸板备料位",
            warehouse_type="semi_finished",
            is_active=True,
        )
        production_location = WarehouseLocation(
            location_code="E1-L10",
            location_name="三楼成品E1-L10",
            area_code="E1",
            warehouse_type="finished",
            warehouse_floor=3,
            source_version="V11",
            placement_status="placed",
            is_active=True,
        )
        db.add_all([raw_staging, semi_location, production_location])
        db.commit()
        semi_location_id = semi_location.id
        production_location_id = production_location.id

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(incoming_router, prefix="/api/incoming")
    app.include_router(orders_router, prefix="/api/orders")
    app.include_router(production_router, prefix="/api/production")
    app.include_router(requisition_router, prefix="/api/requisition")
    app.include_router(warehouse_router, prefix="/api/warehouse")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    with factory() as db:
        lots_before = int(db.scalar(select(func.count(InventoryLot.id))) or 0)

    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "123456"},
        ).status_code == 200
        draft_response = client.get(
            f"/api/requisition/stock-policies/{ids['policy_a']}/replenishment-draft"
        )
        assert draft_response.status_code == 200, draft_response.text
        draft = draft_response.json()
        assert draft["stock_now"] is False
        assert draft["supplier_name"] == "匿名纸板供应商"
        assert draft["draft_ready"] is True
        line = draft["items"][0]
        assert line["product_code"] == "A-BOX"
        assert line["material_id"] == ids["material_a"]
        assert line["material_code"] == "A/K/B"
        assert line["layer_count"] == 3
        assert line["flute_type"] == "B"
        assert line["report_length_mm"] == 600
        assert line["report_width_mm"] == 470
        assert line["cutting_mode"] == "一开二"
        assert line["target_inventory_type"] == "semi_finished"
        assert line["suggested_finished_quantity"] == 70
        assert line["quantity"] == 35
        assert line["theoretical_requisition_quantity"] == 35
        assert line["compatible_product_codes"] == [
            "A-BOX",
            "A-BOX-PRINT-B",
        ]
        assert other_customer_companion_id not in line["compatible_product_ids"]
        assert different_conversion_id not in line["compatible_product_ids"]
        assert len(draft["compatible_products"]) == 1
        companion = draft["compatible_products"][0]
        assert companion["product_code"] == "A-BOX-PRINT-B"
        assert companion["warning_triggered"] is True
        assert companion["draft_item"]["quantity"] == 30
        assert companion["draft_item"]["theoretical_requisition_quantity"] == 30

        payload = {
            "source_type": "stock_warning",
            "idempotency_key": "anonymous-warning-board-preparation",
            "supplier_name": draft["supplier_name"],
            "customer_id": draft["customer_id"],
            "stock_now": True,
            "items": [line],
        }
        blocked = client.post(
            "/api/requisition/stock-replenishment/orders",
            json=payload,
        )
        assert blocked.status_code == 400
        assert "不能保存后直接写入库存" in blocked.text

        payload["stock_now"] = False
        bypass = {
            **line,
            "target_inventory_type": "finished",
            "quantity": 80,
            "location_id": ids["standard_location"],
        }
        blocked_finished = client.post(
            "/api/requisition/stock-replenishment/orders",
            json={**payload, "items": [bypass]},
        )
        assert blocked_finished.status_code == 400
        assert "客户专用纸板备料" in blocked_finished.text

        line["quantity"] = 45
        line["location_id"] = semi_location_id
        saved = client.post(
            "/api/requisition/stock-replenishment/orders",
            json=payload,
        )
        assert saved.status_code == 201, saved.text
        assert saved.json()["status"] == "confirmed"
        assert saved.json()["stocked_quantity"] == 0
        repeated_save = client.post(
            "/api/requisition/stock-replenishment/orders",
            json=payload,
        )
        assert repeated_save.status_code == 201, repeated_save.text
        assert repeated_save.json()["id"] == saved.json()["id"]
        with factory() as db:
            from app.services.stock_replenishment import stock_policy_dict

            assert int(
                db.scalar(select(func.count(StockReplenishmentOrder.id))) or 0
            ) == 1
            incoming_summary = stock_policy_dict(
                db,
                db.get(InventoryStockPolicy, ids["policy_a"]),
            )
            assert incoming_summary[
                "incoming_board_preparation_sheet_quantity"
            ] == 45
            assert incoming_summary[
                "customer_board_preparation_available_sheet_quantity"
            ] == 0
            assert incoming_summary[
                "suggested_new_requisition_sheet_quantity"
            ] == 0
            assert incoming_summary["replenishment_state"] == "already_ordered"
            companion_policy = db.scalar(
                select(InventoryStockPolicy).where(
                    InventoryStockPolicy.product_id == companion_product_id
                )
            )
            companion_incoming = stock_policy_dict(db, companion_policy)
            assert companion_incoming[
                "incoming_board_preparation_sheet_quantity"
            ] == 45
            assert companion_incoming[
                "incoming_board_preparation_finished_capacity"
            ] == 90
            assert companion_incoming[
                "incoming_board_preparation_auto_cover_capacity"
            ] == 0
            assert companion_incoming[
                "suggested_new_requisition_sheet_quantity"
            ] == 30
        direct_stock = client.post(
            f"/api/requisition/stock-replenishment/orders/{saved.json()['id']}/stock"
        )
        assert direct_stock.status_code == 409, direct_stock.text
        assert "来料入库" in direct_stock.text
        replenishment_item_id = saved.json()["items"][0]["id"]
        pending_incoming = client.get("/api/incoming/pending")
        assert pending_incoming.status_code == 200, pending_incoming.text
        assert any(
            row["item_id"] == f"sr{replenishment_item_id}"
            and row["source_type"] == "stock_replenishment"
            and row["incoming_quantity"] == 45
            for row in pending_incoming.json()["items"]
        )
        received = client.put(
            f"/api/incoming/receive/sr{replenishment_item_id}",
            json={
                "received_quantity": 45,
                "idempotency_key": "uat-warning-board-preparation-incoming",
            },
        )
        assert received.status_code == 200, received.text
        assert received.json()["received_inventory_lot_id"] is not None
        with factory() as db:
            from app.services.stock_replenishment import replenishment_item_dict

            stocked_item = replenishment_item_dict(
                db.get(StockReplenishmentOrderItem, replenishment_item_id)
            )
        assert stocked_item["inventory_lot"]["display_name"] == "客户专用纸板备料"
        assert stocked_item["inventory_lot"]["quantity_available"] == 45
        assert [
            row["product_code"]
            for row in stocked_item["inventory_lot"]["allowed_products"]
        ] == ["A-BOX", "A-BOX-PRINT-B"]
        with factory() as db:
            stocked_summary = stock_policy_dict(
                db,
                db.get(InventoryStockPolicy, ids["policy_a"]),
            )
            assert stocked_summary[
                "customer_board_preparation_available_sheet_quantity"
            ] == 45
            assert stocked_summary[
                "incoming_board_preparation_sheet_quantity"
            ] == 0
            assert stocked_summary[
                "suggested_new_requisition_sheet_quantity"
            ] == 0
            assert (
                stocked_summary["replenishment_state"]
                == "board_preparation_ready"
            )
            companion_policy = db.scalar(
                select(InventoryStockPolicy).where(
                    InventoryStockPolicy.product_id == companion_product_id
                )
            )
            companion_stocked = stock_policy_dict(db, companion_policy)
            assert companion_stocked[
                "customer_board_preparation_available_sheet_quantity"
            ] == 45
            assert companion_stocked[
                "customer_board_preparation_finished_capacity"
            ] == 90
            assert companion_stocked[
                "customer_board_preparation_auto_cover_capacity"
            ] == 0
            assert companion_stocked[
                "suggested_new_requisition_sheet_quantity"
            ] == 30
        searched = client.get(
            "/api/warehouse/lots",
            params={
                "inventory_type": "semi_finished",
                "keyword": "A-BOX-PRINT-B",
            },
        )
        assert searched.status_code == 200, searched.text
        searched_payload = searched.json()
        assert searched_payload["total"] == 1
        searched_lot = searched_payload["items"][0]
        assert searched_lot["detail"]["inventory_display_name"] == "客户专用纸板备料"
        assert [
            row["product_code"]
            for row in searched_lot["detail"]["allowed_products"]
        ] == ["A-BOX", "A-BOX-PRINT-B"]

        with factory() as db:
            lot_count_before_repeat = int(
                db.scalar(select(func.count(InventoryLot.id))) or 0
            )
            movement_count_before_repeat = int(
                db.scalar(select(func.count(InventoryMovement.id))) or 0
            )
            binding_count_before_repeat = int(
                db.scalar(select(func.count(SemiFinishedLotAllowedProduct.id))) or 0
            )
        repeated_stock = client.put(
            f"/api/incoming/receive/sr{replenishment_item_id}",
            json={
                "received_quantity": 45,
                "idempotency_key": "uat-warning-board-preparation-incoming",
            },
        )
        assert repeated_stock.status_code == 200, repeated_stock.text
        assert repeated_stock.json()["received_inventory_lot_id"] == (
            stocked_item["inventory_lot"]["id"]
        )
        with factory() as db:
            assert int(db.scalar(select(func.count(InventoryLot.id))) or 0) == (
                lot_count_before_repeat
            )
            assert int(
                db.scalar(select(func.count(InventoryMovement.id))) or 0
            ) == movement_count_before_repeat
            assert int(
                db.scalar(select(func.count(SemiFinishedLotAllowedProduct.id))) or 0
            ) == binding_count_before_repeat

        created_order = client.post(
            "/api/orders",
            json={
                "customer_id": ids["customer_a"],
                "customer_po": "UAT-客户备料闭环",
                "order_date": "2026-07-25",
                "import_integrity_status": "ok",
                "items": [
                    {
                        "client_line_id": "UAT-BOARD-PREP-LINE-1",
                        "product_id": ids["product_a"],
                        "quantity": 80,
                        "unit_price": "1.00",
                        "special_process": "一开二",
                    }
                ],
            },
        )
        assert created_order.status_code == 201, created_order.text
        order_item_id = created_order.json()["items"][0]["id"]

        pending_before = client.get("/api/requisition/pending")
        assert pending_before.status_code == 200, pending_before.text
        pending_row = next(
            row
            for row in pending_before.json()["items"]
            if row["item_id"] == order_item_id
        )
        assert pending_row["cutting_mode"] == "一开二"
        assert pending_row["required_piece_qty"] == 80
        assert pending_row["requisition_qty"] == 40
        assert pending_row["can_auto_use_customer_board_preparation"] is True
        assert pending_row["customer_board_preparation_available_sheet_qty"] == 40

        with TestClient(app) as restricted_client:
            assert restricted_client.post(
                "/api/auth/login",
                json={"username": "no-warehouse", "password": "123456"},
            ).status_code == 200
            forbidden = restricted_client.post(
                f"/api/requisition/pending/{order_item_id}/"
                "auto-use-customer-board-preparation",
                json={"idempotency_key": "no-warehouse-must-not-reserve"},
            )
            assert forbidden.status_code == 403

        with factory() as db:
            order_item = db.get(OrderItem, order_item_id)
            semi_lot = db.get(
                InventoryLot,
                stocked_item["inventory_lot"]["id"],
            )
            assert order_item is not None and semi_lot is not None
            detail = semi_lot.semi_finished_detail
            original_supplier = detail.supplier_name
            detail.supplier_name = "错误供应商"
            unsafe_policy_summary = stock_policy_dict(
                db,
                db.get(InventoryStockPolicy, ids["policy_a"]),
            )
            assert unsafe_policy_summary[
                "customer_board_preparation_available_sheet_quantity"
            ] == 0
            assert unsafe_policy_summary[
                "suggested_new_requisition_sheet_quantity"
            ] == 35
            db.commit()
        unsafe_supplier = client.get("/api/requisition/pending").json()["items"]
        assert next(
            row for row in unsafe_supplier if row["item_id"] == order_item_id
        )["can_auto_use_customer_board_preparation"] is False
        with factory() as db:
            detail = db.get(
                InventoryLot,
                stocked_item["inventory_lot"]["id"],
            ).semi_finished_detail
            detail.supplier_name = original_supplier
            order_item = db.get(OrderItem, order_item_id)
            order_item.layer_count = 5
            db.commit()
        unsafe_layer = client.get("/api/requisition/pending").json()["items"]
        assert next(
            row for row in unsafe_layer if row["item_id"] == order_item_id
        )["can_auto_use_customer_board_preparation"] is False
        with factory() as db:
            order_item = db.get(OrderItem, order_item_id)
            order_item.layer_count = 3
            detail = db.get(
                InventoryLot,
                stocked_item["inventory_lot"]["id"],
            ).semi_finished_detail
            detail.sheet_type = "raw_board"
            detail.crease_type = "毛片"
            db.commit()
        unsafe_sheet = client.get("/api/requisition/pending").json()["items"]
        assert next(
            row for row in unsafe_sheet if row["item_id"] == order_item_id
        )["can_auto_use_customer_board_preparation"] is False
        with factory() as db:
            detail = db.get(
                InventoryLot,
                stocked_item["inventory_lot"]["id"],
            ).semi_finished_detail
            detail.sheet_type = "net_sheet"
            detail.crease_type = "净料"
            db.commit()

        auto_used = client.post(
            f"/api/requisition/pending/{order_item_id}/"
            "auto-use-customer-board-preparation",
            json={"idempotency_key": "uat-board-prep-auto-use"},
        )
        assert auto_used.status_code == 200, auto_used.text
        assert auto_used.json()["allocated_sheet_quantity"] == 40
        assert auto_used.json()["allocated_piece_quantity"] == 80
        assert auto_used.json()["remaining_requirement_quantity"] == 0
        assert auto_used.json()["requisition_qty"] == 0

        blocked_finished_after_board_reservation = client.post(
            f"/api/requisition/pending/{order_item_id}/"
            "auto-use-finished-inventory",
            json={"idempotency_key": "board-reservation-must-not-be-released"},
        )
        assert blocked_finished_after_board_reservation.status_code == 409
        assert "已有半成品或客户专用纸板备料预占" in (
            blocked_finished_after_board_reservation.text
        )

        repeated_auto_use = client.post(
            f"/api/requisition/pending/{order_item_id}/"
            "auto-use-customer-board-preparation",
            json={"idempotency_key": "uat-board-prep-auto-use"},
        )
        assert repeated_auto_use.status_code == 200, repeated_auto_use.text
        assert repeated_auto_use.json()["allocated_sheet_quantity"] == 0
        assert repeated_auto_use.json()["allocated_piece_quantity"] == 0

        tasks = client.get("/api/production/tasks")
        assert tasks.status_code == 200, tasks.text
        task = next(
            row
            for row in tasks.json()["items"]
            if row["order_item_id"] == order_item_id
        )
        assert task["status"] == "pending"
        assert task["customer_board_preparation_sources"] == [
            {
                "reservation_id": task["customer_board_preparation_sources"][0][
                    "reservation_id"
                ],
                "inventory_lot_id": stocked_item["inventory_lot"]["id"],
                "lot_number": task["customer_board_preparation_sources"][0][
                    "lot_number"
                ],
                "source_ref_type": "stock_replenishment_receipt",
                "source_ref_id": received.json()["receipt_item_id"],
                "location_code": "1FA",
                "location_name": "Floor 1 A1 raw-material staging",
                "remaining_sheet_quantity": 40,
                "remaining_product_quantity": 80,
                "stock_yield_per_sheet": 2,
                "display_name": "客户专用纸板备料",
            }
        ]

        completion_payload = {
            "idempotency_key": "uat-board-prep-production-complete",
            "items": [
                {
                    "task_id": task["id"],
                    "expected_version": task["version"],
                    "disposition": "stock",
                    "material_input_quantity": 40,
                    "actual_output_quantity": 70,
                    "defective_quantity": 10,
                    "location_id": production_location_id,
                }
            ],
        }
        completed = client.post(
            "/api/production/completion-batches",
            json=completion_payload,
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["replayed"] is False
        completion = completed.json()["items"][0]
        assert completion["actual_output_quantity"] == 70
        assert completion["stock_quantity"] == 70
        assert completion["inventory_lot_id"] is not None

        repeated_completion = client.post(
            "/api/production/completion-batches",
            json=completion_payload,
        )
        assert repeated_completion.status_code == 200, repeated_completion.text
        assert repeated_completion.json()["replayed"] is True

    with factory() as db:
        order = db.scalar(select(StockReplenishmentOrder))
        item = db.scalar(select(StockReplenishmentOrderItem))
        assert order is not None and item is not None
        assert order.source_type == "stock_warning"
        assert order.status == "stocked"
        assert item.target_inventory_type == "semi_finished"
        assert item.quantity == 45
        assert item.stocked_quantity == 45
        assert item.material_id == ids["material_a"]
        assert item.report_length_mm == 600
        assert item.report_width_mm == 470
        assert int(
            db.scalar(
                select(func.count(FinishedGoodsInventoryDetail.inventory_lot_id))
            )
            or 0
        ) == lots_before + 1
        semi_lot = db.scalar(
            select(InventoryLot).where(
                InventoryLot.inventory_type == "semi_finished"
            )
        )
        assert semi_lot is not None
        assert semi_lot.quantity_available == 5
        assert semi_lot.quantity_reserved == 0
        assert semi_lot.quantity_consumed == 40
        assert semi_lot.unit == "sheets"
        assert semi_lot.semi_finished_detail.owner_customer_id == ids["customer_a"]
        assert set(
            db.scalars(
                select(SemiFinishedLotAllowedProduct.product_id).where(
                    SemiFinishedLotAllowedProduct.inventory_lot_id == semi_lot.id
                )
            ).all()
        ) == {ids["product_a"], companion_product_id}


def test_pending_order_uses_later_customer_finished_stock_before_requisition(
    tmp_path: Path,
) -> None:
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.orders import router as orders_router
    from app.api.requisition import router as requisition_router
    from app.models.product import Product
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryReservation,
        WarehouseLocation,
    )

    _engine, factory, ids = _factory(tmp_path)
    with factory() as db:
        product = db.get(Product, ids["product_a"])
        location = db.get(WarehouseLocation, ids["standard_location"])
        assert product is not None and location is not None
        # The order must exist before any safe exact finished stock appears.
        for lot_number in ("LOT-A-DEDICATED", "LOT-A-INVALID-V11"):
            seeded_lot = db.scalar(
                select(InventoryLot).where(
                    InventoryLot.lot_number == lot_number
                )
            )
            assert seeded_lot is not None
            seeded_lot.status = "frozen"
        _add_finished_lot(
            db,
            lot_number="LOT-A-WRONG-CODE",
            product=product,
            location=location,
            quantity_available=777,
        )
        wrong_code = db.scalar(
            select(InventoryLot).where(
                InventoryLot.lot_number == "LOT-A-WRONG-CODE"
            )
        )
        assert wrong_code is not None and wrong_code.finished_detail is not None
        wrong_code.finished_detail.inventory_code_snapshot = "OTHER-CODE"
        _add_finished_lot(
            db,
            lot_number="LOT-A-CROSS-CUSTOMER",
            product=product,
            location=location,
            quantity_available=333,
        )
        cross_customer = db.scalar(
            select(InventoryLot).where(
                InventoryLot.lot_number == "LOT-A-CROSS-CUSTOMER"
            )
        )
        assert (
            cross_customer is not None
            and cross_customer.finished_detail is not None
        )
        cross_customer.finished_detail.owner_customer_id = ids["customer_b"]
        cross_customer.finished_detail.owner_customer_name_snapshot = "匿名客户B"
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(orders_router, prefix="/api/orders")
    app.include_router(requisition_router, prefix="/api/requisition")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "123456"},
        ).status_code == 200
        created = client.post(
            "/api/orders",
            json={
                "customer_id": ids["customer_a"],
                "customer_po": "UAT-后来成品优先抵扣",
                "order_date": "2026-07-26",
                "import_integrity_status": "ok",
                "items": [
                    {
                        "client_line_id": "LATE-FINISHED-LINE-1",
                        "product_id": ids["product_a"],
                        "quantity": 1200,
                        "unit_price": "1.00",
                        "special_process": "一开二",
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        order_item_id = created.json()["items"][0]["id"]

        with factory() as db:
            product = db.get(Product, ids["product_a"])
            standard_location = db.get(
                WarehouseLocation, ids["standard_location"]
            )
            alternate_location = db.scalar(
                select(WarehouseLocation).where(
                    WarehouseLocation.location_code == "BAD-V11"
                )
            )
            assert (
                product is not None
                and standard_location is not None
                and alternate_location is not None
            )
            _add_finished_lot(
                db,
                lot_number="LOT-A-LATE-50",
                product=product,
                location=standard_location,
                quantity_available=50,
            )
            _add_finished_lot(
                db,
                lot_number="LOT-A-LATE-999",
                product=product,
                location=alternate_location,
                quantity_available=999,
            )
            db.commit()

        pending = client.get("/api/requisition/pending")
        assert pending.status_code == 200, pending.text
        row = next(
            item
            for item in pending.json()["items"]
            if item["item_id"] == order_item_id
        )
        assert row["late_finished_inventory_available_qty"] == 1049
        assert row["late_finished_inventory_reservable_qty"] == 1049
        assert row["can_auto_use_late_finished_inventory"] is True
        assert {
            (item["location_code"], item["available_quantity"])
            for item in row["late_finished_inventory_locations"]
        } == {("FG-A01", 50), ("BAD-V11", 999)}

        draft = client.post(
            "/api/requisition/supplier-orders/preview-from-pending-selection",
            json={
                "selections": [
                    {
                        "type": "order_item",
                        "order_item_id": order_item_id,
                        "supplier_name": "匿名纸板供应商",
                        "report_length_mm": 600,
                        "report_width_mm": 470,
                        "cutting_mode": "一开二",
                    }
                ]
            },
        )
        assert draft.status_code == 200, draft.text
        with TestClient(app) as other_customer_client:
            assert other_customer_client.post(
                "/api/auth/login",
                json={"username": "other-customer", "password": "123456"},
            ).status_code == 200
            denied_customer_scope = other_customer_client.post(
                "/api/requisition/supplier-orders/from-pending-selection",
                json=draft.json(),
            )
            assert denied_customer_scope.status_code == 403
            assert "当前可抵扣" not in denied_customer_scope.text

        blocked_formal_requisition = client.post(
            "/api/requisition/supplier-orders/from-pending-selection",
            json=draft.json(),
        )
        assert blocked_formal_requisition.status_code == 409
        assert "发现订单保存后新增" in blocked_formal_requisition.text
        blocked_legacy_batch = client.post(
            "/api/requisition/batches",
            json={
                "supplier_name": "匿名纸板供应商",
                "items": [
                    {
                        "order_item_id": order_item_id,
                        "requisition_qty": 600,
                        "cardboard_len": 600,
                        "cardboard_width": 470,
                        "special_process": "一开二",
                    }
                ],
            },
        )
        assert blocked_legacy_batch.status_code == 409
        assert "发现订单保存后新增" in blocked_legacy_batch.text
        blocked_legacy_supplier_order = client.post(
            "/api/requisition/supplier-orders",
            json={
                "supplier_name": "匿名纸板供应商",
                "material_id": ids["material_a"],
                "layer_count": 3,
                "flute_type": "B",
                "report_length_mm": 600,
                "report_width_mm": 470,
                "cutting_mode": "一开二",
                "members": [
                    {
                        "item_id": order_item_id,
                        "quantity": 1200,
                        "cutting_mode": "一开二",
                    }
                ],
            },
        )
        assert blocked_legacy_supplier_order.status_code == 409
        assert "发现订单保存后新增" in blocked_legacy_supplier_order.text

        with TestClient(app) as restricted_client:
            assert restricted_client.post(
                "/api/auth/login",
                json={"username": "no-warehouse", "password": "123456"},
            ).status_code == 200
            denied = restricted_client.post(
                f"/api/requisition/pending/{order_item_id}/"
                "auto-use-finished-inventory",
                json={"idempotency_key": "late-finished-no-permission"},
            )
            assert denied.status_code == 403

        reserved = client.post(
            f"/api/requisition/pending/{order_item_id}/"
            "auto-use-finished-inventory",
            json={"idempotency_key": "late-finished-first-pass"},
        )
        assert reserved.status_code == 200, reserved.text
        body = reserved.json()
        assert body["allocated_quantity"] == 1049
        assert body["production_required_qty"] == 151
        assert body["requisition_qty"] == 76
        assert [item["allocated_quantity"] for item in body["reservations"]] == [
            50,
            999,
        ]

        replay = client.post(
            f"/api/requisition/pending/{order_item_id}/"
            "auto-use-finished-inventory",
            json={"idempotency_key": "late-finished-first-pass"},
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["allocated_quantity"] == 1049

        with factory() as db:
            product = db.get(Product, ids["product_a"])
            location = db.get(WarehouseLocation, ids["standard_location"])
            assert product is not None and location is not None
            _add_finished_lot(
                db,
                lot_number="LOT-A-LATER-100",
                product=product,
                location=location,
                quantity_available=100,
            )
            db.commit()
        replay_after_new_stock = client.post(
            f"/api/requisition/pending/{order_item_id}/"
            "auto-use-finished-inventory",
            json={"idempotency_key": "late-finished-first-pass"},
        )
        assert replay_after_new_stock.status_code == 200
        assert replay_after_new_stock.json()["allocated_quantity"] == 1049

        second_pass = client.post(
            f"/api/requisition/pending/{order_item_id}/"
            "auto-use-finished-inventory",
            json={"idempotency_key": "late-finished-second-pass"},
        )
        assert second_pass.status_code == 200, second_pass.text
        assert second_pass.json()["allocated_quantity"] == 100
        assert second_pass.json()["production_required_qty"] == 51
        assert second_pass.json()["requisition_qty"] == 26

        with factory() as db:
            product = db.get(Product, ids["product_a"])
            location = db.get(WarehouseLocation, ids["standard_location"])
            assert product is not None and location is not None
            _add_finished_lot(
                db,
                lot_number="LOT-A-FINAL-51",
                product=product,
                location=location,
                quantity_available=51,
            )
            db.commit()
        final_pass = client.post(
            f"/api/requisition/pending/{order_item_id}/"
            "auto-use-finished-inventory",
            json={"idempotency_key": "late-finished-final-pass"},
        )
        assert final_pass.status_code == 200, final_pass.text
        assert final_pass.json()["allocated_quantity"] == 51
        assert final_pass.json()["fully_covered_by_finished_inventory"] is True
        final_replay = client.post(
            f"/api/requisition/pending/{order_item_id}/"
            "auto-use-finished-inventory",
            json={"idempotency_key": "late-finished-final-pass"},
        )
        assert final_replay.status_code == 200, final_replay.text
        assert final_replay.json()["idempotent_replay"] is True
        assert final_replay.json()["allocated_quantity"] == 51
        pending_after = client.get("/api/requisition/pending")
        assert pending_after.status_code == 200
        assert order_item_id not in {
            item["item_id"]
            for item in pending_after.json()["items"]
            if not item.get("is_merge_group")
        }

    with factory() as db:
        rows = db.scalars(
            select(InventoryReservation)
            .where(InventoryReservation.order_item_id == order_item_id)
            .order_by(InventoryReservation.id)
        ).all()
        assert sum(int(item.reserved_stock_quantity) for item in rows) == 1200
        assert len(rows) == 4


def test_frontend_exposes_read_only_alert_and_two_number_setup() -> None:
    source = Path("static/index.html").read_text(encoding="utf-8")

    assert "常用箱低库存" in source
    assert "lowStockCustomerGroups" in source
    assert "库存不足 {{ group.items.length }} 款" in source
    assert "仓库总数 <strong>{{ item.available_quantity }}</strong>" in source
    assert "<label>仓库当前总数</label>" in source
    assert "同一客户、同一存货编码汇总可用与已预占的实物" in source
    assert "quickStockPolicyForm.allocatable_available_quantity" in source
    assert "quickStockPolicyForm.reserved_quantity" in source
    assert "距离目标还差 <strong>{{ item.suggested_replenishment_quantity }}</strong> 个成品" in source
    assert "已有客户备料" in source
    assert "已报料待到" in source
    assert "已计入另一款，不重复计算" in source
    assert "本次新报 <strong>{{ item.suggested_new_requisition_sheet_quantity }}</strong> 张" in source
    assert "openLowStockReplenishment(item)" in source
    assert "openLowStockLocations(item)" in source
    assert "同客户、同材质和同报料尺寸的其他款" in source
    assert "stockWarningTheoreticalSheets(line)" in source
    assert "客户专用纸板备料" in source
    assert "stockWarningExtraSheets(line)" in source
    assert "本次报料张数（可多报）" in source
    assert "使用备料，只报差额" in source
    assert "autoUseCustomerBoardPreparation(row)" in source
    assert "canWarehouseReserve" in source
    assert "客户备料已预占" in source
    assert "productionBoardPreparationSummary(row)" in source
    assert "有备料 ${sheets} 张" in source
    assert "auto-use-customer-board-preparation" in source
    assert "后来可用 {{ row.late_finished_inventory_reservable_qty || 0 }} 个" in source
    assert "使用成品，剩余再报" in source
    assert "supplierDraftLateFinishedSources()" in source
    assert "confirmDraftLateFinishedInventory(source)" in source
    assert "后来入库的客户专用成品" in source
    assert "autoUseLateFinishedInventory(row)" in source
    assert "auto-use-finished-inventory" in source
    late_finished_method = source.split(
        "async autoUseLateFinishedInventory(row)", 1
    )[1].split("async autoUseCustomerBoardPreparation(row)", 1)[0]
    assert "await this.loadRequisition()" in late_finished_method
    assert "confirm(" not in late_finished_method
    assert "prompt(" not in late_finished_method
    draft_finished_method = source.split(
        "async confirmDraftLateFinishedInventory(source)"
    )[1].split("async confirmDraftSemiInventory(option)")[0]
    assert "confirm(" not in draft_finished_method
    assert "prompt(" not in draft_finished_method
    assert "refreshSupplierRequisitionDraftAfterInventoryReservation()" in (
        draft_finished_method
    )
    assert "openProductStockPolicy(item)" in source
    assert "低于多少预警" in source
    assert "建议补到多少" in source
    assert "两个预警设置都可以高于当前库存" in source
    assert '@input="syncQuickStockTargetToWarning"' in source
    assert "this.syncQuickStockTargetToWarning();" in source
    assert "建议补到数量不能小于库存下限" not in source
    assert (
        "/api/requisition/stock-policies/finished-products/${productId}" in source
    )

    warehouse_source = Path("static/warehouse.html").read_text(encoding="utf-8")
    assert 'params.get("keyword")' in warehouse_source
    assert "已按 ${keyword} 筛选库存位置" in warehouse_source
    assert '可供：${h(allowedProductCodes.join("、"))}' in warehouse_source
