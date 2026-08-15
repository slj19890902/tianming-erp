from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.inventory_onboarding import (
    InventoryOnboardingBatch,
    InventoryOnboardingLine,
)
from app.models.inventory_onboarding_posting import (
    InventoryOnboardingPosting,
)
from app.models.material import Material
from app.models.product import Product
from app.models.stocktake import StocktakeOrder
from app.models.user import User
from app.models.warehouse_inventory import (
    Floor3LocationLayout,
    InventoryLocationMovement,
    InventoryLot,
    InventoryMovement,
    InventoryPallet,
    InventoryPalletItem,
    InventoryReservation,
    WarehouseLocation,
)
from app.services import inventory_onboarding_posting as posting_service
from app.services.inventory_onboarding import (
    create_onboarding_draft,
    dry_run_onboarding_batch,
    get_onboarding_batch,
    submit_onboarding_batch,
)
from app.services.inventory_onboarding_posting import (
    InventoryOnboardingPostingError,
)
from app.services.warehouse_inventory import manual_finished_in
from tests.test_n081_b1_inventory_onboarding_service import (
    _finished_values,
    _formal_counts,
    _semi_values,
    _upload,
)


@pytest.fixture()
def posting_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "ERP_FILE_STORAGE_DIR",
        str(tmp_path / "private-storage"),
    )
    engine = create_sqlite_engine(
        tmp_path / "n081-small-batch-posting.sqlite3"
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="n081-posting-admin",
            password_hash="test-only",
            role="admin",
            real_name="N081入账管理员",
            must_change_password=False,
            customer_access_mode="all",
        )
        customer = Customer(
            customer_number=8201,
            customer_code="N081-C",
            name="N081测试客户",
            payment_term_days=0,
            credit_limit=0,
        )
        material = Material(
            code="K=A",
            layer_count=5,
            flute_type="AB",
            is_active=True,
        )
        db.add_all([admin, customer, material])
        db.flush()
        product = Product(
            customer_id=customer.id,
            product_code="N081-P001",
            customer_material_code="N081-P001",
            product_name="N081成品纸箱",
            box_category="normal",
            material_id=material.id,
            default_material_code=material.code,
            flute_type="AB",
            layer_count=5,
        )
        locations = [
            WarehouseLocation(
                location_code="E1-R01",
                location_name="三楼 E1-R01",
                warehouse_type="finished",
                warehouse_floor=3,
                area_code="E1",
                storage_type="ground",
                placement_status="placed",
                source_version="V11",
            ),
            WarehouseLocation(
                location_code="E1-R02",
                location_name="三楼 E1-R02",
                warehouse_type="finished",
                warehouse_floor=3,
                area_code="E1",
                storage_type="ground",
                placement_status="placed",
                source_version="V11",
            ),
            WarehouseLocation(
                location_code="E1-S01",
                location_name="三楼 E1-S01",
                warehouse_type="semi_finished",
                warehouse_floor=3,
                area_code="E1",
                storage_type="ground",
                placement_status="placed",
                source_version="N081",
            ),
        ]
        db.add_all([product, *locations])
        db.commit()
        yield db, {
            "admin": admin,
            "customer": customer,
            "material": material,
            "product": product,
            "locations": {
                location.location_code: location
                for location in locations
            },
            "tmp_path": tmp_path,
        }
    engine.dispose()


def _general_finished_values() -> tuple[object, ...]:
    values = list(
        _finished_values(
            location="E1-R02",
            pallet="STK-POST-GENERAL-FG",
            quantity=12,
        )
    )
    values[3] = "通用"
    return tuple(values)


def _submitted_batch(
    db: Session,
    data: dict[str, object],
) -> InventoryOnboardingBatch:
    rows = [
        _finished_values(
            location="E1-R01",
            pallet="STK-POST-CUSTOMER-FG",
            quantity=11,
        ),
        _general_finished_values(),
        _semi_values(
            location="E1-S01",
            pallet="STK-POST-GENERAL-SI",
            quantity=21,
        ),
    ]
    batch, created = create_onboarding_draft(
        db,
        upload=_upload(
            data["tmp_path"],
            rows,
            digest_seed="small-batch-posting",
        ),
        creator=data["admin"],
    )
    db.commit()
    assert created is True
    assert len(batch.lines) == 3
    assert {
        (line.action_decision, line.match_status)
        for line in batch.lines
    } == {("create_new", "ready")}

    dry_run_onboarding_batch(
        db,
        batch_id=batch.id,
        expected_version=batch.version,
        operator=data["admin"],
    )
    db.commit()
    fingerprint = str(batch.dry_run_fingerprint)
    submit_onboarding_batch(
        db,
        batch_id=batch.id,
        expected_version=batch.version,
        dry_run_fingerprint=fingerprint,
        idempotency_key=f"n081-small-batch-submit-{batch.id}",
        confirmed=True,
        operator=data["admin"],
    )
    db.commit()
    assert batch.status == "submitted"
    return batch


def _count(db: Session, model: type) -> int:
    return int(db.scalar(select(func.count(model.id))) or 0)


_EXISTING_FIELD_HEADERS = (
    "现场序号",
    "位置",
    "客户",
    "存货编码",
    "产品名称",
    "系统数量",
    "现场数量",
    "备注",
    "库存批次ID",
    "库存版本",
    "可用数量",
    "预占数量",
    "库存类型",
    "归属类型",
    "楼层",
    "区域",
    "栈板号",
    "客户ID",
    "产品ID",
    "单位",
    "入库日期",
    "日期可信度",
    "入库日期原文",
    "现场位置",
)


def _exported_existing_field_values(
    data: dict[str, object],
    lot: InventoryLot,
    *,
    quantity: int,
    position_note: str,
    sequence: str,
) -> tuple[object, ...]:
    customer = data["customer"]
    product = data["product"]
    pallet = lot.pallet_item.pallet
    return (
        sequence,
        "E1-R01",
        customer.name,
        product.product_code,
        product.product_name,
        int(lot.quantity_available or 0),
        quantity,
        "盘点现场复核",
        lot.id,
        lot.version,
        int(lot.quantity_available or 0),
        int(lot.quantity_reserved or 0),
        "成品",
        "客户专用",
        3,
        "E1",
        pallet.pallet_code,
        customer.id,
        product.id,
        "个",
        "2026-07-23",
        "精确",
        "",
        position_note,
    )


def _submit_exported_existing_batch(
    db: Session,
    data: dict[str, object],
    rows: list[tuple[object, ...]],
    *,
    seed: str,
) -> InventoryOnboardingBatch:
    batch, _ = create_onboarding_draft(
        db,
        upload=_upload(
            data["tmp_path"],
            rows,
            digest_seed=seed,
            headers=_EXISTING_FIELD_HEADERS,
        ),
        creator=data["admin"],
    )
    db.commit()
    dry_run_onboarding_batch(
        db,
        batch_id=batch.id,
        expected_version=batch.version,
        operator=data["admin"],
    )
    db.commit()
    submit_onboarding_batch(
        db,
        batch_id=batch.id,
        expected_version=batch.version,
        dry_run_fingerprint=str(batch.dry_run_fingerprint),
        idempotency_key=f"n081-exported-existing-submit-{batch.id}",
        confirmed=True,
        operator=data["admin"],
    )
    db.commit()
    return batch


def _existing_finished_lot(
    db: Session,
    data: dict[str, object],
    *,
    quantity: int = 12,
    pallet_code: str,
    idempotency_key: str,
    pallet_id: int | None = None,
) -> InventoryLot:
    return manual_finished_in(
        db,
        customer_id=data["customer"].id,
        product_id=data["product"].id,
        location_id=data["locations"]["E1-R01"].id,
        quantity=quantity,
        stock_date=date(2026, 7, 23),
        source_type="stocktake",
        remarks="导出盘点基线",
        operator_id=data["admin"].id,
        idempotency_key=idempotency_key,
        pallet_code=pallet_code,
        pallet_id=pallet_id,
    )


def test_exported_existing_lot_moves_pallet_to_exact_field_target_and_replays(
    posting_db,
) -> None:
    db, data = posting_db
    lot = _existing_finished_lot(
        db,
        data,
        pallet_code="PLT-N081-EXACT-MOVE",
        idempotency_key="n081-exact-move-lot",
    )
    db.commit()
    movement_count_before = _count(db, InventoryLocationMovement)
    batch = _submit_exported_existing_batch(
        db,
        data,
        [
            _exported_existing_field_values(
                data,
                lot,
                quantity=12,
                position_note="E1-R02",
                sequence="001",
            )
        ],
        seed="existing-lot-exact-target",
    )

    source = batch.lines[0]
    assert source.action_decision == "route_n035"
    requested_location = source.match_evidence_json["requested_location"]
    assert requested_location["raw"] == "E1-R02"
    assert (
        requested_location["target_location_id"]
        == data["locations"]["E1-R02"].id
    )
    assert requested_location["move_required"] is True
    posting = posting_service.post_submitted_batch(
        db,
        batch_id=batch.id,
        operator=data["admin"],
    )
    db.commit()
    db.refresh(lot)
    assert lot.warehouse_location_id == data["locations"]["E1-R02"].id
    assert lot.pallet_item.pallet.location_id == lot.warehouse_location_id
    assert _count(db, InventoryLocationMovement) == movement_count_before + 1
    assert len(posting.evidence_json["existing_location_movement_ids"]) == 1

    replay = posting_service.post_submitted_batch(
        db,
        batch_id=batch.id,
        operator=data["admin"],
    )
    db.commit()
    assert replay.id == posting.id
    assert _count(db, InventoryLocationMovement) == movement_count_before + 1


def test_exported_existing_lot_free_text_position_marks_needs_relocation(
    posting_db,
) -> None:
    db, data = posting_db
    lot = _existing_finished_lot(
        db,
        data,
        pallet_code="PLT-N081-RELOCATION",
        idempotency_key="n081-relocation-lot",
    )
    db.commit()
    movement_count_before = _count(db, InventoryLocationMovement)
    batch = _submit_exported_existing_batch(
        db,
        data,
        [
            _exported_existing_field_values(
                data,
                lot,
                quantity=12,
                position_note="E1 东侧待归位",
                sequence="001",
            )
        ],
        seed="existing-lot-free-text-location",
    )
    assert "EXISTING_LOCATION_NEEDS_REVIEW" in batch.lines[0].warning_codes_json

    posting = posting_service.post_submitted_batch(
        db,
        batch_id=batch.id,
        operator=data["admin"],
    )
    db.commit()
    db.refresh(lot)
    assert lot.warehouse_location_id == data["locations"]["E1-R01"].id
    assert lot.pallet_item.pallet.needs_relocation is True
    assert _count(db, InventoryLocationMovement) == movement_count_before
    assert posting.evidence_json["relocation_pallet_ids"] == [
        lot.pallet_item.pallet_id
    ]


def test_exported_same_pallet_with_multiple_targets_is_rejected(
    posting_db,
) -> None:
    db, data = posting_db
    first = _existing_finished_lot(
        db,
        data,
        quantity=12,
        pallet_code="PLT-N081-CONFLICT",
        idempotency_key="n081-conflict-first",
    )
    db.flush()
    second = _existing_finished_lot(
        db,
        data,
        quantity=8,
        pallet_code="PLT-N081-CONFLICT",
        pallet_id=first.pallet_item.pallet_id,
        idempotency_key="n081-conflict-second",
    )
    target = WarehouseLocation(
        location_code="E1-R03",
        location_name="三楼 E1-R03",
        warehouse_type="finished",
        warehouse_floor=3,
        area_code="E1",
        storage_type="ground",
        placement_status="placed",
        source_version="V11",
    )
    db.add(target)
    db.commit()
    batch = _submit_exported_existing_batch(
        db,
        data,
        [
            _exported_existing_field_values(
                data, first, quantity=12, position_note="E1-R02", sequence="001"
            ),
            _exported_existing_field_values(
                data, second, quantity=8, position_note="E1-R03", sequence="002"
            ),
        ],
        seed="existing-pallet-target-conflict",
    )

    with pytest.raises(InventoryOnboardingPostingError) as captured:
        posting_service.post_submitted_batch(
            db,
            batch_id=batch.id,
            operator=data["admin"],
        )
    assert captured.value.code == "INVENTORY_ONBOARDING_POSTING_PALLET_TARGET_CONFLICT"
    db.rollback()
    db.expire_all()
    assert first.pallet_item.pallet.location_id == data["locations"]["E1-R01"].id
    assert _count(db, InventoryOnboardingPosting) == 0


def test_posts_the_entire_mixed_batch_and_replays_without_duplicates(
    posting_db,
) -> None:
    db, data = posting_db
    batch = _submitted_batch(db, data)
    formal_before = _formal_counts(db)
    location_movements_before = _count(db, InventoryLocationMovement)
    post_logs_before = int(
        db.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.action == "N081_POST"
            )
        )
        or 0
    )

    posting = posting_service.post_submitted_batch(
        db,
        batch_id=batch.id,
        operator=data["admin"],
    )
    db.commit()

    assert posting.line_count == 3
    assert posting.finished_line_count == 2
    assert posting.semi_finished_line_count == 1
    assert posting.onboarding_batch_id == batch.id
    assert posting.onboarding_batch_version == batch.version
    assert (
        posting.onboarding_batch_fingerprint
        == batch.dry_run_fingerprint
    )
    assert _formal_counts(db) == tuple(
        before + delta
        for before, delta in zip(
            formal_before,
            (3, 3, 3, 3, 0),
            strict=True,
        )
    )
    assert (
        _count(db, InventoryLocationMovement)
        == location_movements_before + 6
    )
    assert _count(db, InventoryOnboardingPosting) == 1
    assert (
        int(
            db.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.action == "N081_POST"
                )
            )
            or 0
        )
        == post_logs_before + 1
    )

    source_lines = {
        line.id: line
        for line in db.scalars(
            select(InventoryOnboardingLine).where(
                InventoryOnboardingLine.batch_id == batch.id
            )
        ).all()
    }
    lots = db.scalars(
        select(InventoryLot)
        .where(
            InventoryLot.source_ref_type
            == "inventory_onboarding_line",
            InventoryLot.source_ref_id.in_(source_lines),
        )
        .order_by(InventoryLot.id)
    ).all()
    assert len(lots) == 3
    assert len({lot.source_ref_id for lot in lots}) == 3
    assert _count(db, InventoryReservation) == formal_before[4]

    for lot in lots:
        source = source_lines[int(lot.source_ref_id)]
        assert lot.source_type == "stocktake"
        assert lot.inventory_type == source.inventory_type
        assert lot.warehouse_location_id == source.location_id
        assert lot.quantity_available == source.quantity
        assert lot.quantity_reserved == 0
        assert lot.unit == source.unit
        assert lot.pallet_item is not None
        assert lot.pallet_item.pallet.pallet_code == source.pallet_code
        movement = db.scalar(
            select(InventoryMovement).where(
                InventoryMovement.inventory_lot_id == lot.id,
                InventoryMovement.movement_type == "manual_in",
            )
        )
        assert movement is not None
        assert movement.reason == posting_service.MOVEMENT_REASON

        if source.inventory_type == "semi_finished":
            assert source.ownership_type == "general"
            assert lot.semi_finished_detail is not None
            assert lot.semi_finished_detail.owner_customer_id is None
            assert lot.semi_finished_detail.material_id == data["material"].id
            assert lot.pallet_item.customer_id is None
        elif source.ownership_type == "general":
            assert lot.finished_detail is not None
            assert lot.finished_detail.is_general is True
            assert lot.finished_detail.owner_customer_id is None
            assert lot.finished_detail.product_id == data["product"].id
            assert lot.pallet_item.customer_id is None
        else:
            assert lot.finished_detail is not None
            assert lot.finished_detail.is_general is False
            assert (
                lot.finished_detail.owner_customer_id
                == data["customer"].id
            )
            assert lot.pallet_item.customer_id == data["customer"].id

    evidence = posting.evidence_json
    assert sorted(evidence["lot_ids"]) == sorted(lot.id for lot in lots)
    assert len(evidence["lines"]) == 3

    counts_after = _formal_counts(db)
    location_movements_after = _count(db, InventoryLocationMovement)
    post_logs_after = int(
        db.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.action == "N081_POST"
            )
        )
        or 0
    )
    replay = posting_service.post_submitted_batch(
        db,
        batch_id=batch.id,
        operator=data["admin"],
    )
    db.commit()

    assert replay.id == posting.id
    assert _formal_counts(db) == counts_after
    assert (
        _count(db, InventoryLocationMovement)
        == location_movements_after
    )
    assert _count(db, InventoryOnboardingPosting) == 1
    assert (
        int(
            db.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.action == "N081_POST"
                )
            )
            or 0
        )
        == post_logs_after
    )
    assert get_onboarding_batch(db, batch.id).status == "submitted"


def test_post_rejects_mapped_location_changed_after_submitted_snapshot(
    posting_db,
) -> None:
    db, data = posting_db
    target = data["locations"]["E1-R01"]
    layout = Floor3LocationLayout(
        location_id=target.id,
        left_pct=10,
        top_pct=10,
        width_pct=12,
        height_pct=10,
        layout_kind="physical_pallet",
        source_type="seeded",
        version=1,
    )
    db.add(layout)
    db.commit()

    batch = _submitted_batch(db, data)
    formal_before = _formal_counts(db)
    pallets_before = _count(db, InventoryPallet)
    logs_before = _count(db, OperationLog)

    layout.left_pct = 40
    layout.version += 1
    db.commit()

    with pytest.raises(InventoryOnboardingPostingError) as captured:
        posting_service.post_submitted_batch(
            db,
            batch_id=batch.id,
            operator=data["admin"],
        )
    assert (
        captured.value.code
        == "INVENTORY_ONBOARDING_POSTING_LOCATION_LAYOUT_STALE"
    )
    db.rollback()
    db.expire_all()

    assert _formal_counts(db) == formal_before
    assert _count(db, InventoryPallet) == pallets_before
    assert _count(db, InventoryOnboardingPosting) == 0
    assert _count(db, OperationLog) == logs_before
    assert get_onboarding_batch(db, batch.id).status == "submitted"


def test_minimal_unknown_location_posts_to_explicit_pending_location(
    posting_db,
) -> None:
    db, data = posting_db
    headers = (
        "现场序号",
        "客户",
        "产品名称或存货编码",
        "现场数量",
        "现场位置",
    )
    batch, _ = create_onboarding_draft(
        db,
        upload=_upload(
            data["tmp_path"],
            [("001", "N081", "N081-P001", 8, "?")],
            digest_seed="small-batch-pending-location",
            headers=headers,
        ),
        creator=data["admin"],
    )
    db.commit()
    source = batch.lines[0]
    assert source.match_status == "ready"
    assert source.location_id is None
    assert source.area_code_snapshot == "待定位"

    dry_run_onboarding_batch(
        db,
        batch_id=batch.id,
        expected_version=batch.version,
        operator=data["admin"],
    )
    db.commit()
    submit_onboarding_batch(
        db,
        batch_id=batch.id,
        expected_version=batch.version,
        dry_run_fingerprint=str(batch.dry_run_fingerprint),
        idempotency_key="n081-pending-location-submit",
        confirmed=True,
        operator=data["admin"],
    )
    db.commit()

    posting = posting_service.post_submitted_batch(
        db,
        batch_id=batch.id,
        operator=data["admin"],
    )
    db.commit()

    lot = db.get(InventoryLot, posting.evidence_json["lot_ids"][0])
    assert lot is not None
    target = db.get(WarehouseLocation, lot.warehouse_location_id)
    assert target is not None
    assert target.location_code == source.location_code_snapshot
    assert target.location_name == "位置待确认 · 现场 001"
    assert target.area_code == "待定位"
    assert target.source_version == "N081_PENDING"
    assert target.is_temporary is True
    assert lot.quantity_available == 8
    assert lot.finished_detail is not None
    assert lot.finished_detail.product_id == data["product"].id
    assert lot.finished_detail.owner_customer_id == data["customer"].id
    assert lot.pallet_item is not None
    assert lot.pallet_item.pallet.needs_relocation is True
    assert posting.evidence_json["pending_location_ids"] == [target.id]


def test_exported_existing_lot_is_adjusted_once_without_creating_new_lot(
    posting_db,
) -> None:
    db, data = posting_db
    lot = manual_finished_in(
        db,
        customer_id=data["customer"].id,
        product_id=data["product"].id,
        location_id=data["locations"]["E1-R01"].id,
        quantity=12,
        stock_date=date(2026, 7, 23),
        source_type="stocktake",
        remarks="导出盘点基线",
        operator_id=data["admin"].id,
        idempotency_key="n081-existing-stocktake-lot",
        pallet_code="PLT-N081-EXISTING",
    )
    db.commit()
    lot_count_before = _count(db, InventoryLot)
    movement_count_before = _count(db, InventoryMovement)
    headers = (
        "现场序号",
        "位置",
        "客户",
        "存货编码",
        "产品名称",
        "系统数量",
        "现场数量",
        "备注",
        "库存批次ID",
        "库存版本",
        "可用数量",
        "预占数量",
        "库存类型",
        "归属类型",
        "楼层",
        "区域",
        "栈板号",
        "客户ID",
        "产品ID",
        "单位",
        "入库日期",
        "日期可信度",
        "入库日期原文",
    )
    values = (
        "001",
        "E1-R01",
        data["customer"].name,
        data["product"].product_code,
        data["product"].product_name,
        12,
        0,
        "现场已无库存",
        lot.id,
        lot.version,
        12,
        0,
        "成品",
        "客户专用",
        3,
        "E1",
        "PLT-N081-EXISTING",
        data["customer"].id,
        data["product"].id,
        "个",
        "2026-07-23",
        "精确",
        "",
    )
    batch, _ = create_onboarding_draft(
        db,
        upload=_upload(
            data["tmp_path"],
            [values],
            digest_seed="existing-stocktake-round-trip",
            headers=headers,
        ),
        creator=data["admin"],
    )
    db.commit()
    source = batch.lines[0]
    assert source.action_decision == "route_n035"
    assert source.match_status == "routed"
    assert source.existing_lot_id == lot.id

    dry_run_onboarding_batch(
        db,
        batch_id=batch.id,
        expected_version=batch.version,
        operator=data["admin"],
    )
    db.commit()
    submit_onboarding_batch(
        db,
        batch_id=batch.id,
        expected_version=batch.version,
        dry_run_fingerprint=str(batch.dry_run_fingerprint),
        idempotency_key="n081-existing-stocktake-submit",
        confirmed=True,
        operator=data["admin"],
    )
    db.commit()

    posting = posting_service.post_submitted_batch(
        db,
        batch_id=batch.id,
        operator=data["admin"],
    )
    db.commit()
    db.refresh(lot)

    assert _count(db, InventoryLot) == lot_count_before
    assert _count(db, InventoryMovement) == movement_count_before + 1
    assert lot.quantity_available == 0
    assert lot.quantity_reserved == 0
    assert posting.line_count == 1
    assert posting.evidence_json["adjusted_existing_lot_ids"] == [lot.id]
    assert len(posting.evidence_json["stocktake_order_ids"]) == 1
    order = db.get(
        StocktakeOrder,
        posting.evidence_json["stocktake_order_ids"][0],
    )
    assert order is not None
    assert order.status == "approved"
    assert order.items[0].counted_quantity == 0

    replay = posting_service.post_submitted_batch(
        db,
        batch_id=batch.id,
        operator=data["admin"],
    )
    db.commit()
    assert replay.id == posting.id
    assert _count(db, InventoryLot) == lot_count_before
    assert _count(db, InventoryMovement) == movement_count_before + 1


@pytest.mark.parametrize("failure_line", (1, 2, 3))
def test_any_line_failure_rolls_back_the_whole_batch(
    posting_db,
    monkeypatch: pytest.MonkeyPatch,
    failure_line: int,
) -> None:
    db, data = posting_db
    batch = _submitted_batch(db, data)
    formal_before = _formal_counts(db)
    location_movements_before = _count(db, InventoryLocationMovement)
    postings_before = _count(db, InventoryOnboardingPosting)
    logs_before = _count(db, OperationLog)
    original_manual_in = posting_service._manual_in
    call_count = 0

    def fail_selected_line(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == failure_line:
            raise InventoryOnboardingPostingError(
                f"模拟第 {failure_line} 行入账失败"
            )
        return original_manual_in(*args, **kwargs)

    monkeypatch.setattr(
        posting_service,
        "_manual_in",
        fail_selected_line,
    )

    with pytest.raises(
        InventoryOnboardingPostingError,
        match=f"第 {failure_line} 行入账失败",
    ):
        posting_service.post_submitted_batch(
            db,
            batch_id=batch.id,
            operator=data["admin"],
        )
    db.rollback()
    db.expire_all()

    assert call_count == failure_line
    assert _formal_counts(db) == formal_before
    assert (
        _count(db, InventoryLocationMovement)
        == location_movements_before
    )
    assert _count(db, InventoryOnboardingPosting) == postings_before
    assert _count(db, OperationLog) == logs_before
    assert (
        db.scalar(
            select(InventoryLot.id).where(
                InventoryLot.source_ref_type
                == "inventory_onboarding_line"
            )
        )
        is None
    )
    assert get_onboarding_batch(db, batch.id).status == "submitted"
