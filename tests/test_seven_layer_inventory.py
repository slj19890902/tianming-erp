from __future__ import annotations

import sqlite3
from collections.abc import Generator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "av49v8x9y0r39"
REVISION = "aw50v8x9y0s40"


@pytest.fixture()
def inventory_app(tmp_path: Path, seed_supplier_master):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.incoming import router as incoming_router
    from app.api.requisition import router as requisition_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.material import Material
    from app.models.user import User
    from app.models.warehouse_inventory import (
        WarehouseArea,
        WarehouseFloor,
        WarehouseLocation,
    )

    engine = create_sqlite_engine(tmp_path / "seven-layer-inventory.sqlite3")
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    seed_supplier_master(session_factory, "七层测试纸板厂", "SEVEN-INV")
    seed_supplier_master(session_factory, "三五层回归纸板厂", "L35-INV")
    with session_factory() as db:
        user = User(
            username="seven-layer-admin",
            password_hash=hash_password("SevenLayerPass123!"),
            role="admin",
            real_name="七层库存管理员",
            display_name="七层库存管理员",
            must_change_password=False,
        )
        material = Material(
            code="JA616AJ",
            layer_count=7,
            flute_type=None,
            supplier_name="七层测试纸板厂",
            quote_price=Decimal("4.2500"),
            price_unit="元/㎡",
            is_active=True,
        )
        floor = WarehouseFloor(
            floor_code="F1",
            floor_name="一楼",
            floor_number=1,
            construction_status="enabled",
        )
        db.add(floor)
        db.flush()
        area = WarehouseArea(
            floor_id=floor.id,
            area_code="A1",
            area_name="A1原料暂存区",
            construction_status="enabled",
        )
        location = WarehouseLocation(
            location_code="SI-7L-01",
            location_name="七层半成品测试库位",
            warehouse_type="semi_finished",
            warehouse_floor=1,
            area_code="A1",
            storage_type="ground",
            placement_status="placed",
            is_active=True,
        )
        db.add_all([user, material, area, location])
        db.commit()
        ids = {
            "material_id": material.id,
            "location_id": location.id,
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(incoming_router, prefix="/api/incoming")
    app.include_router(requisition_router, prefix="/api/requisition")

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    yield app, session_factory, ids
    engine.dispose()


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={
            "username": "seven-layer-admin",
            "password": "SevenLayerPass123!",
        },
    )
    assert response.status_code == 200, response.text


def test_seven_layer_policy_order_save_and_semi_finished_stock(
    inventory_app,
) -> None:
    app, session_factory, ids = inventory_app
    with TestClient(app) as client:
        _login(client)
        policy_response = client.post(
            "/api/requisition/stock-policies",
            json={
                "policy_name": "七层共享纸板 2100x1200",
                "target_inventory_type": "semi_finished",
                "material_code": "JA616AJ",
                "layer_count": 7,
                "flute_type": "aaa",
                "report_length_mm": 2100,
                "report_width_mm": 1200,
                "sheet_type": "raw_board",
                "component_type": "whole",
                "pieces_per_box": 1,
                "stock_yield_per_sheet": 1,
                "warning_quantity": 5,
                "target_quantity": 24,
                "default_location_id": ids["location_id"],
                "supplier_name": "七层测试纸板厂",
                "active": True,
            },
        )
        assert policy_response.status_code == 201, policy_response.text
        policy = policy_response.json()
        assert policy["layer_count"] == 7
        assert policy["flute_type"] == "AAA"
        assert policy["suggested_replenishment_quantity"] == 24

        draft_response = client.get(
            f"/api/requisition/stock-policies/{policy['id']}/replenishment-draft"
        )
        assert draft_response.status_code == 200, draft_response.text
        assert draft_response.json()["items"][0]["flute_type"] == "AAA"

        order_response = client.post(
            "/api/requisition/stock-replenishment/orders",
            json={
                "source_type": "customer_request",
                "supplier_name": "七层测试纸板厂",
                "stock_now": False,
                "items": [
                    {
                        "stock_policy_id": policy["id"],
                        "target_inventory_type": "semi_finished",
                        "material_id": ids["material_id"],
                        "material_code": "WRONG-CODE-IS-IGNORED",
                        "layer_count": 7,
                        "flute_type": "AAA",
                        "report_length_mm": 2100,
                        "report_width_mm": 1200,
                        "quantity": 24,
                        "location_id": ids["location_id"],
                    }
                ],
            },
        )
        assert order_response.status_code == 201, order_response.text
        order = order_response.json()
        assert order["status"] == "confirmed"
        assert order["supplier_name"] == "七层测试纸板厂"
        assert order["items"][0]["material_code"] == "JA616AJ"
        assert order["items"][0]["layer_count"] == 7
        assert order["items"][0]["flute_type"] == "AAA"
        assert order["items"][0]["inventory_lot"] is None

        stock_response = client.post(
            f"/api/requisition/stock-replenishment/orders/{order['id']}/stock"
        )
        assert stock_response.status_code == 409, stock_response.text
        stocked_response = client.put(
            f"/api/incoming/receive/sr{order['items'][0]['id']}",
            json={
                "received_quantity": 24,
                "idempotency_key": "seven-layer-incoming-receipt",
            },
        )
        assert stocked_response.status_code == 200, stocked_response.text
        stocked = client.get(
            f"/api/requisition/stock-replenishment/orders/{order['id']}"
        ).json()
        assert stocked["status"] == "stocked"
        assert stocked["stocked_quantity"] == 24
        assert stocked["items"][0]["inventory_lot"]["quantity_available"] == 24

    from app.models.material import Material
    from app.models.warehouse_inventory import (
        InventoryLot,
        SemiFinishedInventoryDetail,
    )

    with session_factory() as db:
        detail = db.scalar(select(SemiFinishedInventoryDetail))
        assert detail is not None
        assert detail.material_code_snapshot == "JA616AJ"
        assert detail.normalized_material_code == "JA616AJ"
        assert detail.layer_count == 7
        assert detail.flute_type == "AAA"
        lot = db.get(InventoryLot, detail.inventory_lot_id)
        assert lot is not None
        assert lot.source_type == "replenishment"
        assert lot.quantity_available == 24
        material = db.get(Material, ids["material_id"])
        assert material is not None
        assert material.flute_type is None


def test_seven_layer_invalid_flute_is_rejected_by_api_and_inventory_service(
    inventory_app,
) -> None:
    app, session_factory, ids = inventory_app
    with TestClient(app) as client:
        _login(client)
        response = client.post(
            "/api/requisition/stock-replenishment/orders",
            json={
                "source_type": "customer_request",
                "stock_now": False,
                "items": [
                    {
                        "target_inventory_type": "semi_finished",
                        "material_id": ids["material_id"],
                        "layer_count": 7,
                        "flute_type": "AB",
                        "report_length_mm": 2100,
                        "report_width_mm": 1200,
                        "quantity": 1,
                        "location_id": ids["location_id"],
                    }
                ],
            },
        )
        assert response.status_code == 400, response.text
        assert "AAA" in response.json()["detail"]
        assert "ABC" in response.json()["detail"]

    from app.models.stock_replenishment import StockReplenishmentOrder
    from app.models.warehouse_inventory import InventoryLot
    from app.services.warehouse_inventory import (
        WarehouseInventoryError,
        manual_semi_finished_in,
    )

    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(StockReplenishmentOrder)) == 0
        assert db.scalar(select(func.count()).select_from(InventoryLot)) == 0
        with pytest.raises(WarehouseInventoryError, match="七层仅支持AAA/ABC"):
            manual_semi_finished_in(
                db,
                location_id=ids["location_id"],
                quantity=1,
                stock_date=date.today(),
                source_type="manual",
                material_code="JA616AJ",
                layer_count=7,
                flute_type="AAC",
                board_length_mm=2100,
                board_width_mm=1200,
                sheet_type="raw_board",
                supplier_name="七层测试纸板厂",
                customer_id=None,
                crease_type=None,
                crease_left_mm=None,
                crease_middle_mm=None,
                crease_right_mm=None,
                cutting_note=None,
                remarks=None,
                operator_id=None,
                idempotency_key="invalid-seven-layer-flute",
            )


def test_seven_character_code_cannot_be_saved_as_three_layer_inventory_or_policy(
    inventory_app,
) -> None:
    app, session_factory, ids = inventory_app
    with TestClient(app) as client:
        _login(client)
        response = client.post(
            "/api/requisition/stock-policies",
            json={
                "policy_name": "伪装三层的七位代码",
                "target_inventory_type": "semi_finished",
                "material_code": "JA616AJ",
                "layer_count": 3,
                "flute_type": "B",
                "report_length_mm": 2100,
                "report_width_mm": 1200,
                "sheet_type": "raw_board",
                "component_type": "whole",
                "pieces_per_box": 1,
                "stock_yield_per_sheet": 1,
                "warning_quantity": 1,
                "target_quantity": 10,
                "default_location_id": ids["location_id"],
                "active": True,
            },
        )
        assert response.status_code == 400, response.text
        assert "7位材质代码必须按七层保存" in response.json()["detail"]

    from app.services.warehouse_inventory import (
        WarehouseInventoryError,
        manual_semi_finished_in,
    )

    with session_factory() as db:
        with pytest.raises(
            WarehouseInventoryError,
            match="7位材质代码必须按七层保存",
        ):
            manual_semi_finished_in(
                db,
                location_id=ids["location_id"],
                quantity=1,
                stock_date=date.today(),
                source_type="manual",
                material_code="JA616AJ",
                layer_count=3,
                flute_type="B",
                board_length_mm=2100,
                board_width_mm=1200,
                sheet_type="raw_board",
                supplier_name="七层测试纸板厂",
                customer_id=None,
                crease_type=None,
                crease_left_mm=None,
                crease_middle_mm=None,
                crease_right_mm=None,
                cutting_note=None,
                remarks=None,
                operator_id=None,
                idempotency_key="seven-code-disguised-as-three",
            )


@pytest.mark.parametrize(
    ("material_code", "layer_count", "flute_type"),
    [("A4B", 3, "A"), ("A414B", 5, "AB")],
)
def test_three_and_five_layer_replenishment_regression(
    inventory_app,
    material_code: str,
    layer_count: int,
    flute_type: str,
) -> None:
    app, session_factory, ids = inventory_app
    with TestClient(app) as client:
        _login(client)
        response = client.post(
            "/api/requisition/stock-replenishment/orders",
            json={
                "source_type": "customer_request",
                "supplier_name": "三五层回归纸板厂",
                "stock_now": False,
                "items": [
                    {
                        "target_inventory_type": "semi_finished",
                        "material_code": material_code,
                        "layer_count": layer_count,
                        "flute_type": flute_type,
                        "report_length_mm": 1000 + layer_count,
                        "report_width_mm": 700 + layer_count,
                        "quantity": layer_count,
                        "location_id": ids["location_id"],
                    }
                ],
            },
        )
        assert response.status_code == 201, response.text
        order = response.json()
        assert order["status"] == "confirmed"
        stocked = client.post(
            f"/api/requisition/stock-replenishment/orders/{order['id']}/stock"
        )
        assert stocked.status_code == 409, stocked.text
        received = client.put(
            f"/api/incoming/receive/sr{order['items'][0]['id']}",
            json={
                "received_quantity": layer_count,
                "idempotency_key": f"layer-{layer_count}-incoming-receipt",
            },
        )
        assert received.status_code == 200, received.text

    from app.models.warehouse_inventory import SemiFinishedInventoryDetail

    with session_factory() as db:
        detail = db.scalar(select(SemiFinishedInventoryDetail))
        assert detail is not None
        assert detail.layer_count == layer_count
        assert detail.flute_type == flute_type


def test_high_confidence_mapping_validates_seven_layer_code_and_clears_flute(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    from app.api.system import preview_high_confidence_mapping
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.material_mapping import MaterialCodeMappingCandidate
    from app.models.product import Product
    from app.services.material_mapping import (
        apply_high_confidence_material_mapping,
        classify_confidence,
    )

    assert (
        classify_confidence(
            "7层板近似(差0g)", "OLD-CREATE", "JA616AJ", False
        )
        == "高可信"
    )
    assert (
        classify_confidence("7层板近似(差0g)", "OLD-BAD", "A414B", False)
        == "低可信"
    )

    engine = create_sqlite_engine(tmp_path / "seven-layer-material-mapping.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        customer = Customer(
            name="七层材质映射客户",
            payment_term_days=30,
            credit_limit=0,
            delivery_method="自提",
            default_tax_rate=0,
            status="active",
            is_active=True,
        )
        existing_material = Material(
            code="KB616BK",
            supplier_name="嘉林亿",
            layer_count=7,
            flute_type="ABC",
            is_active=True,
        )
        db.add_all([customer, existing_material])
        db.flush()

        products = [
            Product(
                customer_id=customer.id,
                product_code=f"MAP7-{index}",
                customer_material_code=f"MAP7-{index}",
                product_name=f"七层映射产品{index}",
                legacy_material_text=old_code,
            )
            for index, old_code in enumerate(
                ["OLD-CREATE", "OLD-REUSE", "OLD-SHORT", "OLD-SYMBOL"],
                start=1,
            )
        ]
        db.add_all(products)
        db.add_all(
            [
                MaterialCodeMappingCandidate(
                    old_code="OLD-CREATE",
                    new_code="ja616aj",
                    layer_count="7层",
                    confidence_label="7层板近似(差0g)",
                    confidence_level="高可信",
                    review_status="pending",
                ),
                MaterialCodeMappingCandidate(
                    old_code="OLD-REUSE",
                    new_code="KB616BK",
                    layer_count="7层",
                    confidence_label="可直接替换",
                    confidence_level="高可信",
                    review_status="pending",
                ),
                MaterialCodeMappingCandidate(
                    old_code="OLD-SHORT",
                    new_code="A414B",
                    layer_count="7层",
                    confidence_label="可直接替换",
                    confidence_level="高可信",
                    review_status="pending",
                ),
                MaterialCodeMappingCandidate(
                    old_code="OLD-SYMBOL",
                    new_code="JA616A!",
                    layer_count="7层",
                    confidence_label="可直接替换",
                    confidence_level="高可信",
                    review_status="pending",
                ),
            ]
        )
        db.commit()

        actor = SimpleNamespace(id=None, username="system", role="admin")
        preview = preview_high_confidence_mapping(db=db, user=actor)
        assert preview["changes"]
        assert set(preview["confirmation_tokens"]) == {
            change["key"] for change in preview["changes"]
        }

        result = apply_high_confidence_material_mapping(
            db,
            tmp_path / "unused-seven-layer.csv",
            user=actor,
            confirmation_tokens=preview["confirmation_tokens"],
            preview_confirmed=True,
        )
        db.commit()

        assert result.products_updated == 3
        assert result.materials_created == 2
        assert result.materials_reused == 1
        assert result.skipped_no_match == 1
        created = db.scalar(select(Material).where(Material.code == "JA616AJ"))
        assert created is not None
        assert created.layer_count == 7
        assert created.flute_type is None
        db.refresh(existing_material)
        assert existing_material.flute_type is None
        assert db.scalar(select(Material).where(Material.code == "A414B")) is None
        assert db.scalar(select(Material).where(Material.code == "JA616A!")) is not None
        for product in (products[0], products[1], products[3]):
            db.refresh(product)
            assert product.material_id is not None
        db.refresh(products[2])
        assert products[2].material_id is None
    engine.dispose()


def test_batch_versioned_update_retries_exact_p5_confirmation_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException
    from app.services import master_data_versioning
    from app.services.material_mapping import _apply_batch_versioned_update

    calls: list[dict[str, object]] = []

    def confirmation_then_update(_db: object, **kwargs: object) -> None:
        calls.append(kwargs)
        if len(calls) == 1:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "MASTER_CHANGE_CONFIRMATION_REQUIRED",
                    "confirmation_token": "returned-p5-token",
                },
            )

    monkeypatch.setattr(
        master_data_versioning,
        "apply_versioned_update",
        confirmation_then_update,
    )

    _apply_batch_versioned_update(
        object(),
        object_type="product",
        entity=object(),
        updates={"material_id": 7},
        expected_version=11,
        user=object(),
        reason="test",
        source="test",
        action="update",
        confirmation_token=None,
        preview_confirmed=True,
    )

    assert len(calls) == 2
    assert calls[0]["expected_version"] == calls[1]["expected_version"] == 11
    assert calls[0]["confirmation_token"] is None
    assert calls[1]["confirmation_token"] == "returned-p5-token"


@pytest.mark.parametrize(
    ("status_code", "detail", "preview_confirmed"),
    [
        (
            400,
            {
                "code": "MASTER_CHANGE_CONFIRMATION_REQUIRED",
                "confirmation_token": "returned-p5-token",
            },
            True,
        ),
        (409, "MASTER_CHANGE_CONFIRMATION_REQUIRED", True),
        (409, {"code": "MASTER_CHANGE_CONFIRMATION_REQUIRED"}, True),
        (
            409,
            {
                "code": "MASTER_CHANGE_CONFIRMATION_REQUIRED",
                "confirmation_token": "returned-p5-token",
            },
            False,
        ),
    ],
)
def test_batch_versioned_update_does_not_retry_other_errors_or_unconfirmed_batch(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    detail: object,
    preview_confirmed: bool,
) -> None:
    from fastapi import HTTPException
    from app.services import master_data_versioning
    from app.services.material_mapping import _apply_batch_versioned_update

    calls = 0

    def rejected_update(_db: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise HTTPException(status_code=status_code, detail=detail)

    monkeypatch.setattr(
        master_data_versioning,
        "apply_versioned_update",
        rejected_update,
    )

    with pytest.raises(HTTPException):
        _apply_batch_versioned_update(
            object(),
            object_type="product",
            entity=object(),
            updates={"material_id": 7},
            expected_version=11,
            user=object(),
            reason="test",
            source="test",
            action="update",
            confirmation_token=None,
            preview_confirmed=preview_confirmed,
        )

    assert calls == 1


def _alembic_config(
    monkeypatch: pytest.MonkeyPatch,
    database_path: Path,
) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database_path.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "seven-layer-inventory-migration-test")
    return Config(str(PROJECT_ROOT / "alembic.ini"))


def _insert_inventory_lot(
    connection: sqlite3.Connection,
    *,
    lot_number: str,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO inventory_lots (
            lot_number, inventory_type, warehouse_location_id,
            quantity_available, quantity_reserved, quantity_consumed,
            quantity_damaged, quantity_scrapped, unit, status, source_type,
            stock_date, last_movement_at, version
        )
        SELECT ?, 'semi_finished', id, 1, 0, 0, 0, 0, 'sheets', 'active',
               'replenishment', '2026-07-17', '2026-07-17 10:00:00', 1
        FROM warehouse_locations
        WHERE location_code = 'SF-TEMP'
        """,
        (lot_number,),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _insert_inventory_detail(
    connection: sqlite3.Connection,
    *,
    lot_id: int,
    material_code: str,
    layer_count: int,
    flute_type: str,
) -> None:
    connection.execute(
        """
        INSERT INTO semi_finished_inventory_details (
            inventory_lot_id, material_code_snapshot, normalized_material_code,
            layer_count, flute_type, board_length_mm, board_width_mm,
            component_type, pieces_per_box, stock_yield_per_sheet, sheet_type
        ) VALUES (?, ?, ?, ?, ?, 1200, 800, 'whole', 1, 1, 'raw_board')
        """,
        (lot_id, material_code, material_code, layer_count, flute_type),
    )


def test_migration_replaces_sqlite_checks_without_rewriting_business_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "seven-layer-inventory-migration.sqlite3"
    config = _alembic_config(monkeypatch, database_path)
    command.upgrade(config, PARENT_REVISION)

    with sqlite3.connect(database_path) as connection:
        three_lot_id = _insert_inventory_lot(connection, lot_number="MIG-3L")
        five_lot_id = _insert_inventory_lot(connection, lot_number="MIG-5L")
        _insert_inventory_detail(
            connection,
            lot_id=three_lot_id,
            material_code="A4B",
            layer_count=3,
            flute_type="A",
        )
        _insert_inventory_detail(
            connection,
            lot_id=five_lot_id,
            material_code="A414B",
            layer_count=5,
            flute_type="AB",
        )
        connection.commit()
        rows_before = connection.execute(
            "SELECT inventory_lot_id, material_code_snapshot, layer_count, flute_type "
            "FROM semi_finished_inventory_details ORDER BY inventory_lot_id"
        ).fetchall()
        indexes_before = {
            row[1]
            for row in connection.execute(
                "PRAGMA index_list(semi_finished_inventory_details)"
            )
        }
        foreign_keys_before = {
            (row[2], row[3], row[4], row[6])
            for row in connection.execute(
                "PRAGMA foreign_key_list(semi_finished_inventory_details)"
            )
        }

    command.upgrade(config, REVISION)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (REVISION,)
        assert connection.execute(
            "SELECT inventory_lot_id, material_code_snapshot, layer_count, flute_type "
            "FROM semi_finished_inventory_details ORDER BY inventory_lot_id"
        ).fetchall() == rows_before
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='semi_finished_inventory_details'"
        ).fetchone()[0]
        assert "ck_semi_inventory_layer" in table_sql
        assert "layer_count IN (3,5,7)" in table_sql
        assert "flute_type IN ('AAA','ABC')" in table_sql
        assert {
            row[1]
            for row in connection.execute(
                "PRAGMA index_list(semi_finished_inventory_details)"
            )
        } == indexes_before
        assert {
            (row[2], row[3], row[4], row[6])
            for row in connection.execute(
                "PRAGMA foreign_key_list(semi_finished_inventory_details)"
            )
        } == foreign_keys_before

        seven_lot_id = _insert_inventory_lot(connection, lot_number="MIG-7L")
        _insert_inventory_detail(
            connection,
            lot_id=seven_lot_id,
            material_code="JA616AJ",
            layer_count=7,
            flute_type="ABC",
        )
        invalid_lot_id = _insert_inventory_lot(
            connection,
            lot_number="MIG-7L-INVALID",
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            _insert_inventory_detail(
                connection,
                lot_id=invalid_lot_id,
                material_code="JA616AJ",
                layer_count=7,
                flute_type="AB",
            )
        connection.rollback()
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    with pytest.raises(RuntimeError, match="seven-layer semi-finished inventory"):
        command.downgrade(config, PARENT_REVISION)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (REVISION,)
        connection.execute(
            "DELETE FROM semi_finished_inventory_details WHERE layer_count=7"
        )
        connection.commit()

    command.downgrade(config, PARENT_REVISION)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (PARENT_REVISION,)
        assert connection.execute(
            "SELECT inventory_lot_id, material_code_snapshot, layer_count, flute_type "
            "FROM semi_finished_inventory_details ORDER BY inventory_lot_id"
        ).fetchall() == rows_before
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='semi_finished_inventory_details'"
        ).fetchone()[0]
        assert "layer_count IN (3,5)" in table_sql
        assert "flute_type IN ('AAA','ABC')" not in table_sql
        with pytest.raises(sqlite3.IntegrityError):
            _insert_inventory_detail(
                connection,
                lot_id=seven_lot_id,
                material_code="JA616AJ",
                layer_count=7,
                flute_type="ABC",
            )
        connection.rollback()
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    command.upgrade(config, REVISION)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT inventory_lot_id, material_code_snapshot, layer_count, flute_type "
            "FROM semi_finished_inventory_details ORDER BY inventory_lot_id"
        ).fetchall() == rows_before
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
