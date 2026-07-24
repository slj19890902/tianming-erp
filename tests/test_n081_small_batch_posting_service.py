from __future__ import annotations

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
