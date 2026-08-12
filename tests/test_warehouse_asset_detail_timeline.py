from __future__ import annotations

from datetime import date, datetime
from hashlib import sha256
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.orm import sessionmaker


def test_asset_detail_timeline_uses_persisted_facts_and_deduplicates_mirrors(
    tmp_path: Path,
) -> None:
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.mold_tool import MoldLocationMovement, MoldTool
    from app.models.stocktake import StocktakeItem, StocktakeOrder
    from app.models.user import User
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryLotTransfer,
        InventoryMovement,
        WarehouseLocation,
    )
    from app.services.asset_time_archive import (
        build_inventory_lot_detail_timeline,
        build_mold_detail_timeline,
    )

    engine = create_sqlite_engine(tmp_path / "asset-detail-timeline.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        user = User(
            username="timeline-admin",
            password_hash=hash_password("Timeline-Test-123!"),
            role="admin",
            real_name="时间线测试",
            must_change_password=False,
        )
        source = WarehouseLocation(
            location_code="1F-TL-01",
            location_name="时间线源位",
            warehouse_type="finished",
        )
        target = WarehouseLocation(
            location_code="1F-TL-02",
            location_name="时间线目标位",
            warehouse_type="finished",
        )
        mold = MoldTool(
            mold_code="TL-MOLD-001",
            mold_name="时间线测试模具",
            rack_location="1F-M-R01-L2-G02",
            created_at=datetime(2026, 5, 1, 1, 0),
            created_by=None,
        )
        db.add_all([user, source, target, mold])
        db.flush()
        mold.created_by = user.id
        lot = InventoryLot(
            lot_number="TL-LOT-001",
            inventory_type="finished",
            warehouse_location_id=target.id,
            quantity_available=12,
            quantity_reserved=0,
            quantity_consumed=0,
            quantity_damaged=0,
            quantity_scrapped=0,
            unit="boxes",
            status="active",
            source_type="manual",
            stock_date=date(2026, 5, 1),
            stock_date_accuracy="unknown",
            stock_date_original_text="历史未建立",
            last_movement_at=datetime(2026, 5, 1, 2, 0),
            created_at=datetime(2026, 5, 1, 2, 0),
            created_by=user.id,
        )
        db.add(lot)
        db.flush()

        long_key = "asset-timeline-" + "x" * 110
        db.add(
            InventoryLotTransfer(
                source_lot_id=lot.id,
                target_lot_id=lot.id,
                source_location_id=source.id,
                target_location_id=target.id,
                quantity=12,
                available_quantity=12,
                reserved_quantity=0,
                source_version_before=1,
                source_version_after=2,
                idempotency_key=long_key,
                request_hash="a" * 64,
                transferred_by=user.id,
                transferred_at=datetime(2026, 5, 2, 1, 0),
            )
        )
        raw_mirror_key = f"location-transfer:{long_key}:source"
        mirror_key = (
            f"{raw_mirror_key[:75]}:"
            f"{sha256(raw_mirror_key.encode('utf-8')).hexdigest()[:24]}"
        )

        def movement(number: str, key: str, remarks: str) -> InventoryMovement:
            return InventoryMovement(
                movement_number=number,
                inventory_lot_id=lot.id,
                movement_type="location_transfer",
                quantity=0,
                unit="boxes",
                before_available=12,
                after_available=12,
                before_reserved=0,
                after_reserved=0,
                before_consumed=0,
                after_consumed=0,
                before_damaged=0,
                after_damaged=0,
                before_scrapped=0,
                after_scrapped=0,
                operator_id=user.id,
                idempotency_key=key,
                remarks=remarks,
                created_at=datetime(2026, 5, 2, 1, 0),
            )

        db.add_all(
            [
                movement("TL-MIRROR", mirror_key, "镜像流水不应重复显示"),
                movement("TL-LEGACY", "legacy-location-fact", "历史自由文本"),
                MoldLocationMovement(
                    mold_tool_id=mold.id,
                    mold_code_snapshot=mold.mold_code,
                    from_location="1F-M-R01-L1-G02",
                    to_location="1F-M-R01-L2-G02",
                    moved_at=datetime(2026, 5, 3, 1, 0),
                    source="scanner_paste",
                    actor_id=user.id,
                    idempotency_key="timeline-mold-move",
                    expected_version=1,
                    resulting_version=2,
                ),
            ]
        )
        stocktake = StocktakeOrder(
            order_number="TL-STOCKTAKE-001",
            location_id=target.id,
            status="submitted",
            submitted_by=user.id,
            submitted_at=datetime(2026, 5, 4, 1, 0),
            idempotency_key="timeline-stocktake",
        )
        db.add(stocktake)
        db.flush()
        db.add(
            StocktakeItem(
                order_id=stocktake.id,
                inventory_lot_id=lot.id,
                lot_version_snapshot=1,
                available_quantity_snapshot=12,
                reserved_quantity_snapshot=0,
                on_hand_quantity_snapshot=12,
                counted_quantity=12,
                difference_quantity=0,
                lot_number_snapshot=lot.lot_number,
                location_code_snapshot=target.location_code,
                unit_snapshot="boxes",
            )
        )
        db.commit()

        statements: list[str] = []

        def capture(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement.strip().upper())

        event.listen(engine, "before_cursor_execute", capture)
        lot_timeline = build_inventory_lot_detail_timeline(db, lot)
        mold_detail = build_mold_detail_timeline(
            db,
            mold,
            products=[],
            allowed_customer_ids=None,
        )
        event.remove(engine, "before_cursor_execute", capture)

        same_lot_move = next(
            item
            for item in lot_timeline
            if item.get("basis") == "inventory_lot_transfer"
        )
        assert same_lot_move["label"] == "库存位置移位"
        assert same_lot_move["direction"] == "move"
        assert same_lot_move["from_location"] == source.location_code
        assert same_lot_move["to_location"] == target.location_code
        assert not any(
            item.get("movement_number") == "TL-MIRROR" for item in lot_timeline
        )
        legacy = next(
            item for item in lot_timeline if item.get("movement_number") == "TL-LEGACY"
        )
        assert legacy["event_type"] == "inventory_location_transfer"
        assert legacy["remarks"] is None
        assert "operator_name" not in legacy
        assert any(
            item["event_type"] == "stocktake_submitted" for item in lot_timeline
        )

        mold_move = next(
            item
            for item in mold_detail["timeline"]
            if item["event_type"] == "mold_location_move"
        )
        assert mold_move["label"] == "扫码粘贴移位"
        assert mold_detail["inbound_status"] == "not_recorded"
        assert mold_detail["stocktake_status"] == "not_supported"
        assert not any(
            statement.startswith(("INSERT", "UPDATE", "DELETE"))
            for statement in statements
        )
    engine.dispose()
