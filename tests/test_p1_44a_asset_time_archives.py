from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import hashlib
import json

from sqlalchemy import event
from sqlalchemy.orm import sessionmaker


def _canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_inventory_and_asset_time_archives_use_formal_facts_without_writes(tmp_path):
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.master_data_object_version import MasterDataObjectVersion
    from app.models.mold_tool import MoldLocationMovement, MoldTool
    from app.models.order import Order, OrderItem
    from app.models.printing_plate import PrintingPlate, PrintingPlateLocationMovement
    from app.models.product import Product
    from app.models.production import (
        ProductionCompletion,
        ProductionCompletionBatch,
        ProductionTask,
    )
    from app.models.stocktake import StocktakeItem, StocktakeOrder
    from app.models.user import User
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryLotTransfer,
        WarehouseLocation,
    )
    from app.services.asset_time_archive import (
        build_inventory_lot_time_archives,
        build_mold_time_archives,
        build_printing_plate_time_archives,
    )

    engine = create_sqlite_engine(tmp_path / "p1-44a.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        user = User(
            username="p1-44a-admin",
            password_hash=hash_password("P1-44a-Test!"),
            role="admin",
            real_name="时间档案测试",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=44001,
            customer_code="P144A",
            name="时间档案客户",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        source_location = WarehouseLocation(
            location_code="1F-A-01",
            location_name="一楼A区1号",
            warehouse_type="finished",
        )
        target_location = WarehouseLocation(
            location_code="1F-B-02",
            location_name="一楼B区2号",
            warehouse_type="finished",
        )
        mold = MoldTool(
            mold_code="P144A-MOLD",
            mold_name="时间档案模具",
            rack_location="1F-M-R01-L1-G01",
            created_at=datetime(2026, 1, 1, 1, 0),
        )
        db.add_all([user, customer, source_location, target_location, mold])
        db.flush()
        plate = PrintingPlate(
            plate_code="P144A-PLATE",
            customer_id=customer.id,
            plate_name="时间档案挂板",
            color_name="黑色",
            rack_location="1F-PL-R01-L1-P01",
            created_at=datetime(2026, 1, 2, 1, 0),
        )
        unused_mold = MoldTool(
            mold_code="P144A-MOLD-NO-HISTORY",
            mold_name="无历史模具",
            rack_location="1F-M-R01-L1-G02",
            created_at=datetime(2026, 1, 3, 1, 0),
        )
        unused_plate = PrintingPlate(
            plate_code="P144A-PLATE-NO-HISTORY",
            customer_id=customer.id,
            plate_name="无历史挂板",
            color_name="蓝色",
            rack_location="1F-PL-R01-L1-P02",
            created_at=datetime(2026, 1, 4, 1, 0),
        )
        product = Product(
            customer_id=customer.id,
            product_code="P144A-P001",
            customer_material_code="TM-P144A",
            product_name="时间档案纸箱",
            mold_tool_id=mold.id,
            printing_plate_mode="plate",
            printing_plate_1_id=None,
        )
        db.add_all([plate, unused_mold, unused_plate, product])
        db.flush()
        product.printing_plate_1_id = plate.id
        snapshot = {
            "customer_id": customer.id,
            "mold_tool_id": mold.id,
            "printing_plate_1_id": plate.id,
            "printing_plate_2_id": None,
            "printing_plate_3_id": None,
            "product_code": product.product_code,
        }
        snapshot_json = _canonical(snapshot)
        db.add(
            MasterDataObjectVersion(
                object_type="product",
                object_id=product.id,
                version=1,
                action="update",
                snapshot_json=snapshot_json,
                snapshot_sha256=hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest(),
                changed_fields_json="{}",
                change_set_id="p1-44a-binding-version",
                source="test",
                created_at=datetime(2026, 2, 1, 1, 0),
            )
        )
        order = Order(
            order_number="P144A-ORDER",
            customer_id=customer.id,
            order_date=date(2026, 3, 1),
            status="production",
            payment_status="unpaid",
            total_amount=Decimal("0"),
            created_at=datetime(2026, 3, 1, 2, 0),
        )
        db.add(order)
        db.flush()
        item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=100,
            delivered_quantity=0,
            unit_price=Decimal("0"),
            subtotal=Decimal("0"),
            material_status="received",
            snapshot_product_code=product.product_code,
            snapshot_product_name=product.product_name,
        )
        db.add(item)
        db.flush()
        task = ProductionTask(
            order_item_id=item.id,
            status="completed",
            planned_quantity=100,
            ordered_quantity_snapshot=100,
            printing_plate_mode_snapshot="plate",
            printing_plate_codes_snapshot=json.dumps([plate.plate_code]),
            created_at=datetime(2026, 3, 2, 1, 0),
        )
        batch = ProductionCompletionBatch(
            idempotency_key="p1-44a-completion",
            request_hash="a" * 64,
            item_count=1,
            completed_by=user.id,
            completed_at=datetime(2026, 3, 5, 1, 0),
        )
        db.add_all([task, batch])
        db.flush()
        db.add(
            ProductionCompletion(
                batch_id=batch.id,
                task_id=task.id,
                order_item_id=item.id,
                expected_version=1,
                quantity=100,
                initial_disposition="direct",
                completed_by=user.id,
                completed_at=datetime(2026, 3, 5, 1, 0),
            )
        )
        db.add_all(
            [
                MoldLocationMovement(
                    mold_tool_id=mold.id,
                    mold_code_snapshot=mold.mold_code,
                    from_location="1F-M-R01-L1-G01",
                    to_location="1F-M-R01-L2-G01",
                    moved_at=datetime(2026, 4, 1, 1, 0),
                    idempotency_key="p1-44a-mold-move",
                    expected_version=1,
                    resulting_version=2,
                ),
                PrintingPlateLocationMovement(
                    printing_plate_id=plate.id,
                    plate_code_snapshot=plate.plate_code,
                    from_location="1F-PL-R01-L1-P01",
                    to_location="1F-PL-R01-L2-P01",
                    moved_at=datetime(2026, 4, 2, 1, 0),
                    idempotency_key="p1-44a-plate-move",
                    expected_version=1,
                    resulting_version=2,
                ),
            ]
        )
        source_lot = InventoryLot(
            lot_number="P144A-SOURCE",
            inventory_type="finished",
            warehouse_location_id=source_location.id,
            quantity_available=0,
            quantity_reserved=0,
            quantity_consumed=10,
            quantity_damaged=0,
            quantity_scrapped=0,
            unit="boxes",
            status="closed",
            source_type="manual",
            stock_date=date(2026, 1, 10),
            stock_date_accuracy="exact",
            last_movement_at=datetime(2026, 5, 1, 1, 0),
            created_at=datetime(2026, 1, 10, 1, 0),
        )
        target_lot = InventoryLot(
            lot_number="P144A-TARGET",
            inventory_type="finished",
            warehouse_location_id=target_location.id,
            quantity_available=10,
            quantity_reserved=0,
            quantity_consumed=0,
            quantity_damaged=0,
            quantity_scrapped=0,
            unit="boxes",
            status="active",
            source_type="transfer",
            stock_date=date(2026, 1, 10),
            stock_date_accuracy="exact",
            last_movement_at=datetime(2026, 5, 1, 1, 0),
            created_at=datetime(2026, 5, 1, 1, 0),
        )
        unknown_lot = InventoryLot(
            lot_number="P144A-UNKNOWN",
            inventory_type="finished",
            warehouse_location_id=target_location.id,
            quantity_available=1,
            quantity_reserved=0,
            quantity_consumed=0,
            quantity_damaged=0,
            quantity_scrapped=0,
            unit="boxes",
            status="active",
            source_type="manual",
            stock_date=date(2026, 1, 1),
            stock_date_accuracy="unknown",
            stock_date_original_text="历史未建立",
            last_movement_at=datetime(2026, 5, 2, 1, 0),
            created_at=datetime(2026, 5, 2, 1, 0),
        )
        db.add_all([source_lot, target_lot, unknown_lot])
        db.flush()
        db.add(
            InventoryLotTransfer(
                source_lot_id=source_lot.id,
                target_lot_id=target_lot.id,
                source_location_id=source_location.id,
                target_location_id=target_location.id,
                quantity=10,
                available_quantity=10,
                reserved_quantity=0,
                source_version_before=1,
                source_version_after=2,
                idempotency_key="p1-44a-lot-transfer",
                request_hash="b" * 64,
                transferred_by=user.id,
                transferred_at=datetime(2026, 5, 1, 1, 0),
            )
        )
        stocktake = StocktakeOrder(
            order_number="P144A-STOCKTAKE",
            location_id=target_location.id,
            status="submitted",
            submitted_by=user.id,
            submitted_at=datetime(2026, 6, 1, 1, 0),
            idempotency_key="p1-44a-stocktake",
        )
        db.add(stocktake)
        db.flush()
        db.add(
            StocktakeItem(
                order_id=stocktake.id,
                inventory_lot_id=target_lot.id,
                lot_version_snapshot=1,
                available_quantity_snapshot=10,
                reserved_quantity_snapshot=0,
                on_hand_quantity_snapshot=10,
                counted_quantity=10,
                difference_quantity=0,
                lot_number_snapshot=target_lot.lot_number,
                location_code_snapshot=target_location.location_code,
                unit_snapshot="boxes",
            )
        )
        db.commit()

        statements: list[str] = []

        def capture(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement.strip().upper())

        event.listen(engine, "before_cursor_execute", capture)
        lots = build_inventory_lot_time_archives(
            db, [target_lot, unknown_lot], as_of=date(2026, 8, 12)
        )
        molds = build_mold_time_archives(
            db,
            [mold, unused_mold],
            products_by_mold={mold.id: [product], unused_mold.id: []},
            allowed_customer_ids=None,
            as_of=date(2026, 8, 12),
        )
        plates = build_printing_plate_time_archives(
            db,
            [plate, unused_plate],
            products_by_plate={plate.id: [product], unused_plate.id: []},
            allowed_customer_ids=None,
            as_of=date(2026, 8, 12),
        )
        event.remove(engine, "before_cursor_execute", capture)

        target_archive = lots[target_lot.id]
        assert target_archive["formed_on"] == "2026-01-10"
        assert target_archive["age_days"] == 214
        assert target_archive["entered_current_location_at"].startswith("2026-05-01")
        assert target_archive["latest_location_transfer_at"].startswith("2026-05-01")
        assert target_archive["latest_stocktake_at"].startswith("2026-06-01")
        assert lots[unknown_lot.id]["formed_on"] is None
        assert lots[unknown_lot.id]["age_days"] is None
        assert lots[unknown_lot.id]["formation_status"] == "历史未建立/待确认"

        for archive in (molds[mold.id], plates[plate.id]):
            assert archive["latest_customer_order_use"]["order_number"] == order.order_number
            assert archive["latest_actual_production_use"]["production_task_id"] == task.id
            assert archive["idle_days"] == 160
            assert archive["usage_history_status"] == "已建立"
        assert molds[mold.id]["latest_location_move_at"].startswith("2026-04-01")
        assert plates[plate.id]["latest_location_move_at"].startswith("2026-04-02")
        for archive in (molds[unused_mold.id], plates[unused_plate.id]):
            assert archive["latest_customer_order_use"] is None
            assert archive["latest_actual_production_use"] is None
            assert archive["idle_days"] is None
            assert archive["usage_history_status"] == "历史未建立/待确认"
        assert not any(
            statement.startswith(("INSERT", "UPDATE", "DELETE"))
            for statement in statements
        )
        assert sum(statement.startswith("SELECT") for statement in statements) <= 15
    engine.dispose()


def test_time_archive_frontend_distinguishes_order_and_actual_production():
    from pathlib import Path

    source = Path("static/warehouse.html").read_text(encoding="utf-8")
    assert "形成：" in source
    assert "入当前位置：" in source
    assert "最近盘点：" in source
    assert "最近下单：" in source
    assert "最近实际生产：" in source
    assert "距实际生产：" in source
    assert "查看时间线" in source
    assert "历史未建立/待确认" in source
    assert "latest_customer_order_use" in source
    assert "latest_actual_production_use" in source
