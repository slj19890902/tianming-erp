from datetime import date
from decimal import Decimal
import json

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select, func

from test_warehouse_goods import stocktake_app
from app.api.warehouse_goods import GoodsFacts, SheetEntry, create_sheet, options
from app.models.user import User
from app.models.supplier import Supplier
from app.models.warehouse_inventory import InventoryLot, InventoryMovement, Floor3LocationLayout
from app.services.inventory_valuation import cost_payload
from app.services.inventory_cost_snapshot import estimate_from_snapshot
from app.services.warehouse_goods import lot_face


def payload(location=1, version=1, supplier=1, **changes):
    values = dict(facts=GoodsFacts(material_code="灰底白板250克", face_paper="white"),
        location_id=location, expected_layout_version=version, quantity=100,
        stock_date=date.today(), internal_name="灰底白板原纸", board_length_mm=1092,
        board_width_mm=787, layer_count=1, flute_type="NONE", supplier_id=supplier,
        sheet_unit_cost="1.2500", idempotency_key="plain-paper-entry")
    values.update(changes)
    return SheetEntry(**values)


@pytest.mark.parametrize("changes", [dict(flute_type="B"), dict(sheet_unit_cost=None),
    dict(sheet_unit_cost="0"), dict(sheet_unit_cost="NaN"), dict(supplier_id=None),
    dict(layer_count=3), dict(facts=GoodsFacts(verified_material_id=1))])
def test_invalid_single_layer_cost_and_flute(changes):
    with pytest.raises(ValidationError):
        payload(**changes)


def setup(db, ids):
    supplier = Supplier(standard_name="灰底白板供应商", normalized_name="灰底白板供应商",
        display_name="灰底白板", is_active=True)
    db.add(supplier)
    db.commit()
    user = db.scalar(select(User).where(User.username == "p147d-admin"))
    location = ids["loc_fg1_add"]
    return user, supplier, payload(location, db.get(Floor3LocationLayout, location).version, supplier.id)


def test_sheet_entry_freezes_each_sheet_and_replay(stocktake_app):
    _, factory, ids, _ = stocktake_app
    with factory() as db:
        user, supplier, entry = setup(db, ids)
        assert {"id": supplier.id, "name": supplier.standard_name} in options(db=db, user=user)["suppliers"]
        result = create_sheet(entry, db, user)
        lot = db.get(InventoryLot, result["lot_id"])
        assert lot.inventory_type == "semi_finished" and lot.unit == "sheets"
        detail = lot.semi_finished_detail
        assert (detail.layer_count, detail.flute_type, detail.sheet_type) == (1, "NONE", "raw_board")
        assert detail.material_id is None and detail.supplier_name == "灰底白板供应商"
        assert lot.finished_detail is None and lot_face(db, lot) == "white"
        assert cost_payload(lot, db)["inventory_value"] == "125.00"
        assert estimate_from_snapshot(lot).unit_cost == Decimal("1.25")
        snapshot = json.loads(lot.cost_snapshot_detail_json)
        assert snapshot["supplier_id"] == supplier.id and snapshot["price_unit"] == "元/张"
        supplier.standard_name = "新名称"
        db.commit()
        assert create_sheet(entry, db, user) == result
        assert json.loads(lot.cost_snapshot_detail_json)["supplier_name"] == "灰底白板供应商"
        assert db.scalar(select(func.count()).select_from(InventoryMovement).where(
            InventoryMovement.idempotency_key == "goods:" + entry.idempotency_key)) == 1
        with pytest.raises(HTTPException) as exc:
            create_sheet(entry.model_copy(update={"quantity": 101}), db, user)
        assert exc.value.status_code == 409


def test_stale_location_and_disabled_supplier_do_not_add_stock(stocktake_app):
    _, factory, ids, _ = stocktake_app
    with factory() as db:
        user, supplier, entry = setup(db, ids)
        before = db.scalar(select(func.count()).select_from(InventoryLot))
        supplier.is_active = False
        db.commit()
        with pytest.raises(HTTPException, match="供应商"):
            create_sheet(entry, db, user)
        supplier.is_active = True
        db.get(Floor3LocationLayout, entry.location_id).version += 1
        db.commit()
        with pytest.raises(HTTPException) as exc:
            create_sheet(entry, db, user)
        assert exc.value.status_code == 409
        assert db.scalar(select(func.count()).select_from(InventoryLot)) == before


def test_permissions_cost_privacy_and_audit_failure(stocktake_app, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.deps import get_db, get_current_user
    from app.api.warehouse_goods import router
    from app.models.audit import OperationLog
    _, factory, ids, _ = stocktake_app
    with factory() as db:
        user, _, entry = setup(db, ids)
        before = db.scalar(select(func.count()).select_from(InventoryLot))
        original = db.add
        def fail_audit(row):
            if isinstance(row, OperationLog):
                raise RuntimeError("plain-paper-audit-failure")
            original(row)
        monkeypatch.setattr(db, "add", fail_audit)
        with pytest.raises(RuntimeError, match="audit-failure"):
            create_sheet(entry, db, user)
        assert db.scalar(select(func.count()).select_from(InventoryLot)) == before
        monkeypatch.setattr(db, "add", original)
        result = create_sheet(entry, db, user)
        app = FastAPI()
        app.include_router(router, prefix="/goods")
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_current_user] = lambda: user
        with TestClient(app) as client:
            response = client.get(f"/goods/{result['lot_id']}")
            assert response.status_code == 200
            assert Decimal(response.json()["physical"]["settlement_unit_price"]) == Decimal("1.25")
            user.role = "workshop"
            db.commit()
            assert client.post("/goods/sheet-entry", json=entry.model_dump(mode="json")).status_code == 403
            response = client.get(f"/goods/{result['lot_id']}")
            assert response.status_code == 200
            assert "settlement_unit_price" not in response.json()["physical"]


def test_migration_preserves_rows_indexes_and_triggers_and_blocks_loss(stocktake_app):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    _, factory, ids, _ = stocktake_app
    spec = importlib.util.spec_from_file_location("plain_migration", Path(__file__).resolve().parents[1] /
        "alembic/versions/mr0914_plain_paper_inventory.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = factory.kw["bind"]
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TRIGGER plain_paper_test_guard BEFORE UPDATE ON semi_finished_inventory_details BEGIN SELECT 1; END")
        rows = conn.exec_driver_sql("SELECT * FROM semi_finished_inventory_details ORDER BY inventory_lot_id").fetchall()
        indexes = conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='semi_finished_inventory_details' ORDER BY name").fetchall()
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
            migration.upgrade()
        assert rows == conn.exec_driver_sql("SELECT * FROM semi_finished_inventory_details ORDER BY inventory_lot_id").fetchall()
        assert indexes == conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='semi_finished_inventory_details' ORDER BY name").fetchall()
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name='plain_paper_test_guard'").scalar() == 1
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    with factory() as db:
        user, _, entry = setup(db, ids)
        create_sheet(entry, db, user)
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)), pytest.raises(RuntimeError, match="拒绝有损降级"):
            migration.downgrade()
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM semi_finished_inventory_details WHERE layer_count=1").scalar() == 1
