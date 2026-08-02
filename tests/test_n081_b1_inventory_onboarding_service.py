from __future__ import annotations

from datetime import date, datetime
from hashlib import sha256
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
from app.models.material import Material
from app.models.product import Product
from app.models.user import User
from app.models.warehouse_inventory import (
    FinishedGoodsInventoryDetail,
    InventoryLot,
    InventoryMovement,
    InventoryPallet,
    InventoryPalletItem,
    InventoryReservation,
    SemiFinishedInventoryDetail,
    WarehouseLocation,
)
from app.services.inventory_onboarding import (
    InventoryOnboardingError,
    create_onboarding_draft,
    dry_run_onboarding_batch,
    get_onboarding_batch,
    submit_onboarding_batch,
    update_onboarding_line,
)
from app.services.inventory_onboarding_uploads import (
    InventoryOnboardingRawRow,
    InventoryOnboardingRawSheet,
    StoredInventoryOnboardingUpload,
)


HEADERS = (
    "盘点日期",
    "盘点人",
    "库存类型",
    "归属类型",
    "楼层",
    "区域",
    "库位编码",
    "栈板号",
    "客户编码",
    "客户名称",
    "存货编码",
    "产品名称",
    "材质编码",
    "数量",
    "单位",
    "入库日期",
    "日期可信度",
    "入库日期原文",
    "供应商",
    "层数",
    "楞型",
    "纸板长",
    "纸板宽",
    "片料类型",
    "组件类型",
    "每箱片数",
    "每张产出",
    "备注",
)


@pytest.fixture()
def onboarding_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "ERP_FILE_STORAGE_DIR",
        str(tmp_path / "private-storage"),
    )
    engine = create_sqlite_engine(tmp_path / "n081-b1-service.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="n081-admin",
            password_hash="test-only",
            role="admin",
            real_name="N081管理员",
            must_change_password=False,
            customer_access_mode="all",
        )
        customer = Customer(
            customer_number=8101,
            customer_code="N081-C",
            name="N081测试客户",
            payment_term_days=0,
            credit_limit=0,
        )
        other_customer = Customer(
            customer_number=8102,
            customer_code="N081-O",
            name="N081其他客户",
            payment_term_days=0,
            credit_limit=0,
        )
        material = Material(
            code="K=A",
            layer_count=5,
            flute_type="AB",
            is_active=True,
        )
        db.add_all([admin, customer, other_customer, material])
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
        other_product = Product(
            customer_id=other_customer.id,
            product_code="N081-OTHER",
            customer_material_code="N081-OTHER",
            product_name="其他客户产品",
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
                location_code="E1-S01",
                location_name="三楼 E1-S01",
                warehouse_type="semi_finished",
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
                location_code="D1-R01",
                location_name="三楼 D1-R01",
                warehouse_type="finished",
                warehouse_floor=3,
                area_code="D1",
                storage_type="ground",
                placement_status="placed",
                source_version="V11",
            ),
            WarehouseLocation(
                location_code="E1-X01",
                location_name="未放置库位",
                warehouse_type="finished",
                warehouse_floor=3,
                area_code="E1",
                storage_type="temporary_aisle",
                placement_status="unplaced",
                source_version="V11",
            ),
        ]
        db.add_all([product, other_product, *locations])
        db.commit()
        yield db, {
            "admin": admin,
            "customer": customer,
            "other_customer": other_customer,
            "product": product,
            "other_product": other_product,
            "material": material,
            "locations": {row.location_code: row for row in locations},
            "tmp_path": tmp_path,
        }
    engine.dispose()


def _finished_values(
    *,
    location: str = "E1-R01",
    pallet: str = "STK-UAT-FG-001",
    customer_code: str = "N081-C",
    customer_name: str = "N081测试客户",
    inventory_code: str = "N081-P001",
    product_name: str = "N081成品纸箱",
    quantity: int = 12,
) -> tuple[object, ...]:
    return (
        "2026-07-23",
        "盘点员甲",
        "成品",
        "客户专用",
        3,
        location.split("-")[0],
        location,
        pallet,
        customer_code,
        customer_name,
        inventory_code,
        product_name,
        "",
        quantity,
        "boxes",
        "",
        "unknown",
        "历史日期不明",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "服务测试",
    )


def _semi_values(
    *,
    location: str = "E1-S01",
    pallet: str = "STK-UAT-SI-001",
    quantity: int = 20,
) -> tuple[object, ...]:
    return (
        "2026-07-23",
        "盘点员乙",
        "半成品",
        "通用",
        3,
        "E1",
        location,
        pallet,
        "",
        "",
        "",
        "",
        "K=A",
        quantity,
        "sheets",
        "2026-07-01",
        "estimated",
        "约 2026 年 7 月",
        "上游纸板厂",
        5,
        "AB",
        800,
        600,
        "net_sheet",
        "whole",
        1,
        1,
        "半成品服务测试",
    )


def _upload(
    tmp_path: Path,
    rows: list[tuple[object, ...]],
    *,
    digest_seed: str,
    headers: tuple[str, ...] = HEADERS,
) -> StoredInventoryOnboardingUpload:
    payload = (
        f"N081-B1 test private source: {digest_seed}\n"
    ).encode("utf-8")
    digest = sha256(payload).hexdigest()
    private_path = (
        tmp_path
        / "private-storage"
        / "inventory_onboarding"
        / f"{digest}.csv"
    )
    private_path.parent.mkdir(parents=True, exist_ok=True)
    private_path.write_bytes(payload)
    source_rows = [
        InventoryOnboardingRawRow(
            sheet_name="盘点",
            row_number=1,
            raw_text=",".join(str(value) for value in headers),
            original_values=headers,
        )
    ]
    for index, values in enumerate(rows, start=2):
        source_rows.append(
            InventoryOnboardingRawRow(
                sheet_name="盘点",
                row_number=index,
                raw_text=",".join(str(value) for value in values),
                original_values=values,
            )
        )
    return StoredInventoryOnboardingUpload(
        private_reference=f"private:inventory_onboarding/{digest}.csv",
        private_path=private_path,
        filename="N081盘点.csv",
        content_type="text/csv",
        size=len(payload),
        sha256=digest,
        format="csv",
        encoding="utf-8",
        sheets=(
            InventoryOnboardingRawSheet(
                name="盘点",
                rows=tuple(source_rows),
            ),
        ),
    )


def _formal_counts(db: Session) -> tuple[int, ...]:
    return tuple(
        int(db.scalar(select(func.count(model.id))) or 0)
        for model in (
            InventoryLot,
            InventoryMovement,
            InventoryPallet,
            InventoryPalletItem,
            InventoryReservation,
        )
    )


def test_draft_dry_run_and_submit_never_create_formal_inventory(
    onboarding_db,
) -> None:
    db, data = onboarding_db
    before = _formal_counts(db)
    batch, created = create_onboarding_draft(
        db,
        upload=_upload(
            data["tmp_path"],
            [_finished_values(), _semi_values()],
            digest_seed="a",
        ),
        creator=data["admin"],
    )
    db.commit()

    assert created is True
    assert batch.status == "draft"
    assert batch.version == 2
    assert len(batch.lines) == 2
    assert {line.match_status for line in batch.lines} == {"ready"}
    assert {line.action_decision for line in batch.lines} == {"create_new"}
    assert all(line.version == 2 for line in batch.lines)
    assert _formal_counts(db) == before

    dry_run_onboarding_batch(
        db,
        batch_id=batch.id,
        expected_version=batch.version,
        operator=data["admin"],
    )
    db.commit()
    assert batch.dry_run_fingerprint
    assert batch.dry_run_summary_json["error_count"] == 0
    assert batch.dry_run_summary_json["create_new_rows"] == 2
    assert batch.resolved_area_code == "E1"
    assert _formal_counts(db) == before

    lines = submit_onboarding_batch(
        db,
        batch_id=batch.id,
        expected_version=batch.version,
        dry_run_fingerprint=batch.dry_run_fingerprint,
        idempotency_key="n081-b1-submit-1",
        confirmed=True,
        operator=data["admin"],
    )
    db.commit()
    assert batch.status == "submitted"
    assert len(lines) == 2
    assert _formal_counts(db) == before
    assert {
        row.action
        for row in db.scalars(select(OperationLog)).all()
    } >= {"N081_B1_IMPORT", "N081_B1_DRY_RUN", "N081_B1_SUBMIT"}

    replay = submit_onboarding_batch(
        db,
        batch_id=batch.id,
        expected_version=1,
        dry_run_fingerprint=batch.dry_run_fingerprint,
        idempotency_key="n081-b1-submit-1",
        confirmed=True,
        operator=data["admin"],
    )
    assert [line.id for line in replay] == [line.id for line in lines]
    assert _formal_counts(db) == before


def test_duplicate_file_returns_the_original_batch(onboarding_db) -> None:
    db, data = onboarding_db
    upload = _upload(
        data["tmp_path"],
        [_finished_values()],
        digest_seed="b",
    )
    first, created = create_onboarding_draft(
        db,
        upload=upload,
        creator=data["admin"],
    )
    db.commit()
    second, replay_created = create_onboarding_draft(
        db,
        upload=upload,
        creator=data["admin"],
    )

    assert created is True
    assert replay_created is False
    assert second.id == first.id
    assert db.scalar(select(func.count(InventoryOnboardingBatch.id))) == 1
    assert db.scalar(select(func.count(InventoryOnboardingLine.id))) == 1


def test_cross_customer_and_unplaced_rows_are_blocked(onboarding_db) -> None:
    db, data = onboarding_db
    batch, _ = create_onboarding_draft(
        db,
        upload=_upload(
            data["tmp_path"],
            [
                _finished_values(
                    inventory_code="N081-OTHER",
                    product_name="其他客户产品",
                ),
                _finished_values(
                    location="E1-X01",
                    pallet="STK-UAT-FG-002",
                ),
            ],
            digest_seed="c",
        ),
        creator=data["admin"],
    )
    db.commit()
    lines = sorted(batch.lines, key=lambda line: line.source_row_number)

    assert lines[0].match_status == "blocked"
    assert "PRODUCT_NOT_FOUND" in lines[0].error_codes_json
    assert lines[1].match_status == "blocked"
    assert "LOCATION_UNPLACED" in lines[1].error_codes_json
    assert _formal_counts(db) == (0, 0, 0, 0, 0)


def test_edit_uses_versions_and_invalidates_previous_dry_run(onboarding_db) -> None:
    db, data = onboarding_db
    batch, _ = create_onboarding_draft(
        db,
        upload=_upload(
            data["tmp_path"],
            [
                _finished_values(
                    customer_code="",
                    customer_name="完全错误客户",
                )
            ],
            digest_seed="d",
        ),
        creator=data["admin"],
    )
    db.commit()
    line = batch.lines[0]
    assert line.match_status == "blocked"

    updated = update_onboarding_line(
        db,
        batch_id=batch.id,
        line_id=line.id,
        expected_version=line.version,
        batch_expected_version=batch.version,
        values={
            "customer_id": data["customer"].id,
            "customer_code": data["customer"].customer_code,
            "customer_name": data["customer"].name,
            "quantity": 15,
        },
        operator=data["admin"],
    )
    db.commit()
    assert updated.match_status == "ready"
    assert updated.quantity == 15

    dry_run_onboarding_batch(
        db,
        batch_id=batch.id,
        expected_version=batch.version,
        operator=data["admin"],
    )
    db.commit()
    assert batch.dry_run_fingerprint
    old_line_version = updated.version
    update_onboarding_line(
        db,
        batch_id=batch.id,
        line_id=updated.id,
        expected_version=old_line_version,
        values={"remarks": "dry-run 后修改"},
        operator=data["admin"],
    )
    db.commit()
    assert batch.dry_run_fingerprint is None
    with pytest.raises(
        InventoryOnboardingError,
        match="其他操作更新",
    ):
        update_onboarding_line(
            db,
            batch_id=batch.id,
            line_id=updated.id,
            expected_version=old_line_version,
            values={"remarks": "旧版本覆盖"},
            operator=data["admin"],
        )


def test_wrong_customer_code_can_be_corrected_without_rewriting_source(
    onboarding_db,
) -> None:
    db, data = onboarding_db
    batch, _ = create_onboarding_draft(
        db,
        upload=_upload(
            data["tmp_path"],
            [
                _finished_values(
                    customer_code="WRONG-CODE",
                    customer_name="N081测试客户",
                )
            ],
            digest_seed="customer-code-correction",
        ),
        creator=data["admin"],
    )
    db.commit()
    line = batch.lines[0]
    assert line.match_status == "blocked"
    assert "CUSTOMER_IDENTITY_CONFLICT" in line.error_codes_json
    original_values = line.original_values_json

    updated = update_onboarding_line(
        db,
        batch_id=batch.id,
        line_id=line.id,
        expected_version=line.version,
        batch_expected_version=batch.version,
        values={"customer_code": "N081-C"},
        operator=data["admin"],
    )
    db.commit()

    assert updated.match_status == "ready"
    assert updated.customer_id == data["customer"].id
    assert updated.customer_code_snapshot == "N081-C"
    assert updated.customer_name_snapshot == "N081测试客户"
    assert updated.original_values_json == original_values
    assert (
        updated.original_values_json["mapped"]["customer_code"]
        == "WRONG-CODE"
    )


def test_master_data_drift_invalidates_dry_run(onboarding_db) -> None:
    db, data = onboarding_db
    batch, _ = create_onboarding_draft(
        db,
        upload=_upload(
            data["tmp_path"],
            [_finished_values()],
            digest_seed="e",
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
    fingerprint = batch.dry_run_fingerprint
    data["product"].version += 1
    db.commit()

    with pytest.raises(
        InventoryOnboardingError,
        match="主数据",
    ) as caught:
        submit_onboarding_batch(
            db,
            batch_id=batch.id,
            expected_version=batch.version,
            dry_run_fingerprint=fingerprint,
            idempotency_key="n081-b1-stale-master",
            confirmed=True,
            operator=data["admin"],
        )
    assert caught.value.code == "INVENTORY_ONBOARDING_DRY_RUN_STALE"
    assert get_onboarding_batch(db, batch.id).status == "draft"
    assert _formal_counts(db) == (0, 0, 0, 0, 0)


def test_cross_area_field_sheet_is_accepted_as_one_stocktake(onboarding_db) -> None:
    db, data = onboarding_db
    batch, _ = create_onboarding_draft(
        db,
        upload=_upload(
            data["tmp_path"],
            [
                _finished_values(),
                _finished_values(
                    location="D1-R01",
                    pallet="STK-UAT-D1-001",
                ),
            ],
            digest_seed="f",
        ),
        creator=data["admin"],
    )
    db.commit()

    assert batch.resolved_area_code == "多区域盘点"
    assert all(line.match_status == "ready" for line in batch.lines)
    assert all(
        "BATCH_CROSS_AREA" not in line.error_codes_json
        for line in batch.lines
    )
    dry_run_onboarding_batch(
        db,
        batch_id=batch.id,
        expected_version=batch.version,
        operator=data["admin"],
    )
    db.commit()
    assert batch.dry_run_summary_json["error_count"] == 0
    assert batch.dry_run_summary_json["blocked_rows"] == 0


def test_minimal_field_row_needs_only_identity_and_quantity(
    onboarding_db,
) -> None:
    db, data = onboarding_db
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
            [("001", "N081", "N081-P001", 12, "?")],
            digest_seed="minimal-field-row",
            headers=headers,
        ),
        creator=data["admin"],
    )
    db.commit()

    line = batch.lines[0]
    assert line.match_status == "ready"
    assert line.action_decision == "create_new"
    assert line.customer_id == data["customer"].id
    assert line.product_id == data["product"].id
    assert line.quantity == 12
    assert line.stocktake_date is not None
    assert line.stocktaker_name == data["admin"].real_name
    assert line.location_id is None
    assert line.area_code_snapshot == "待定位"
    assert line.location_code_snapshot.startswith("PD-")
    assert line.warehouse_name_snapshot == "位置待确认 · 现场 001"
    assert line.pallet_code
    assert "LOCATION_NOT_FOUND" not in line.error_codes_json


def test_question_mark_is_missing_fact_and_exact_product_can_infer_customer(
    onboarding_db,
) -> None:
    db, data = onboarding_db
    headers = (
        "现场序号",
        "客户",
        "存货编码",
        "产品名称",
        "现场数量",
        "位置",
    )
    batch, _ = create_onboarding_draft(
        db,
        upload=_upload(
            data["tmp_path"],
            [("002", "？", "N081-P001", "?", 9, "?")],
            digest_seed="question-mark-field-row",
            headers=headers,
        ),
        creator=data["admin"],
    )
    db.commit()

    line = batch.lines[0]
    assert line.match_status == "ready"
    assert line.customer_id == data["customer"].id
    assert line.product_id == data["product"].id
    assert line.customer_name_snapshot == data["customer"].name
    assert line.product_name_snapshot == data["product"].product_name
    assert line.warehouse_name_snapshot == "位置待确认 · 现场 002"


@pytest.mark.parametrize("quantity", ("?", "", None, 0))
def test_new_field_inventory_requires_a_positive_unambiguous_quantity(
    onboarding_db,
    quantity: object,
) -> None:
    db, data = onboarding_db
    batch, _ = create_onboarding_draft(
        db,
        upload=_upload(
            data["tmp_path"],
            [
                _finished_values(
                    pallet=f"N081-INVALID-QTY-{str(quantity) or 'blank'}",
                    quantity=quantity,
                )
            ],
            digest_seed=f"new-field-invalid-quantity-{quantity!r}",
        ),
        creator=data["admin"],
    )
    db.commit()

    line = batch.lines[0]
    assert line.match_status == "blocked"
    assert "QUANTITY_INVALID" in line.error_codes_json


@pytest.mark.parametrize(
    ("inventory_type", "has_formal_lot", "expected_action"),
    (
        ("finished", True, "route_n035"),
        ("semi_finished", True, "route_semi_adjust"),
        ("finished", False, "route_snapshot_conversion"),
    ),
)
def test_existing_pallets_are_routed_without_duplicate_inventory_writes(
    onboarding_db,
    inventory_type: str,
    has_formal_lot: bool,
    expected_action: str,
) -> None:
    db, data = onboarding_db
    is_finished = inventory_type == "finished"
    location = data["locations"]["E1-R01" if is_finished else "E1-S01"]
    pallet_code = f"EXISTING-{expected_action.upper()}"
    pallet = InventoryPallet(
        pallet_code=pallet_code,
        location_id=location.id,
        status="active",
        is_current=True,
        version=3,
        created_by=data["admin"].id,
    )
    db.add(pallet)
    db.flush()

    lot = None
    if has_formal_lot:
        lot = InventoryLot(
            lot_number=f"LOT-{expected_action.upper()}",
            inventory_type=inventory_type,
            warehouse_location_id=location.id,
            quantity_available=24,
            quantity_reserved=0,
            quantity_consumed=0,
            quantity_damaged=0,
            quantity_scrapped=0,
            unit="boxes" if is_finished else "sheets",
            status="active",
            source_type="stocktake",
            stock_date=date(2026, 7, 1),
            stock_date_accuracy="exact",
            last_movement_at=datetime(2026, 7, 1, 8, 0, 0),
            version=2,
            created_by=data["admin"].id,
        )
        db.add(lot)
        db.flush()
        if is_finished:
            lot.finished_detail = FinishedGoodsInventoryDetail(
                owner_customer_id=data["customer"].id,
                owner_customer_name_snapshot=data["customer"].name,
                is_general=False,
                product_id=data["product"].id,
                inventory_code_snapshot="N081-P001",
                product_name_snapshot="N081成品纸箱",
            )
        else:
            lot.semi_finished_detail = SemiFinishedInventoryDetail(
                supplier_name="上游纸板厂",
                owner_customer_id=None,
                material_id=data["material"].id,
                material_code_snapshot="K=A",
                normalized_material_code="K=A",
                layer_count=5,
                flute_type="AB",
                board_length_mm=800,
                board_width_mm=600,
                sheet_type="net_sheet",
                component_type="whole",
                pieces_per_box=1,
                stock_yield_per_sheet=1,
            )
        db.flush()

    db.add(
        InventoryPalletItem(
            pallet_id=pallet.id,
            inventory_lot_id=lot.id if lot is not None else None,
            customer_id=data["customer"].id if is_finished else None,
            product_id=data["product"].id if is_finished else None,
            inventory_code="N081-P001" if is_finished else None,
            customer_name_snapshot="N081测试客户" if is_finished else None,
            product_name="N081成品纸箱" if is_finished else None,
            item_type=inventory_type,
            quantity=(
                12
                if expected_action == "route_snapshot_conversion"
                else 24
            ),
            unit="boxes" if is_finished else "sheets",
            match_status="matched",
            created_by=data["admin"].id,
        )
    )
    db.commit()
    formal_before = _formal_counts(db)

    row = (
        _finished_values(location=location.location_code, pallet=pallet_code)
        if is_finished
        else _semi_values(location=location.location_code, pallet=pallet_code)
    )
    batch, created = create_onboarding_draft(
        db,
        upload=_upload(
            data["tmp_path"],
            [row],
            digest_seed=expected_action,
        ),
        creator=data["admin"],
    )
    db.commit()

    assert created is True
    assert len(batch.lines) == 1
    line = batch.lines[0]
    assert line.match_status == "routed"
    assert line.action_decision == expected_action
    assert line.existing_pallet_id == pallet.id
    assert line.existing_lot_id == (lot.id if lot is not None else None)
    assert _formal_counts(db) == formal_before

    dry_run_onboarding_batch(
        db,
        batch_id=batch.id,
        expected_version=batch.version,
        operator=data["admin"],
    )
    db.commit()
    assert batch.dry_run_summary_json[f"{expected_action}_rows"] == 1
    assert _formal_counts(db) == formal_before


def test_finished_snapshot_quantity_must_equal_the_onboarding_line(
    onboarding_db,
) -> None:
    db, data = onboarding_db
    location = data["locations"]["E1-R01"]
    pallet = InventoryPallet(
        pallet_code="SNAPSHOT-QUANTITY-MISMATCH",
        location_id=location.id,
        status="active",
        is_current=True,
        version=1,
        created_by=data["admin"].id,
    )
    db.add(pallet)
    db.flush()
    db.add(
        InventoryPalletItem(
            pallet_id=pallet.id,
            inventory_lot_id=None,
            customer_id=data["customer"].id,
            product_id=data["product"].id,
            inventory_code="N081-P001",
            customer_name_snapshot="N081测试客户",
            product_name="N081成品纸箱",
            item_type="finished",
            quantity=24,
            unit="boxes",
            match_status="matched",
            created_by=data["admin"].id,
        )
    )
    db.commit()

    batch, _ = create_onboarding_draft(
        db,
        upload=_upload(
            data["tmp_path"],
            [
                _finished_values(
                    pallet=pallet.pallet_code,
                    quantity=12,
                )
            ],
            digest_seed="snapshot-quantity-mismatch",
        ),
        creator=data["admin"],
    )
    db.commit()
    line = batch.lines[0]

    assert line.match_status == "blocked"
    assert line.action_decision == "pending"
    assert "SNAPSHOT_QUANTITY_UNIT_MISMATCH" in line.error_codes_json


def test_dry_run_rejects_new_pallet_occupancy_drift(onboarding_db) -> None:
    db, data = onboarding_db
    batch, _ = create_onboarding_draft(
        db,
        upload=_upload(
            data["tmp_path"],
            [_finished_values(pallet="STK-DRIFT-001")],
            digest_seed="negative-evidence",
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
    fingerprint = batch.dry_run_fingerprint

    db.add(
        InventoryPallet(
            pallet_code="STK-DRIFT-001",
            location_id=data["locations"]["E1-R01"].id,
            status="active",
            is_current=True,
            version=1,
            created_by=data["admin"].id,
        )
    )
    db.commit()

    with pytest.raises(
        InventoryOnboardingError,
        match="库存事实已变化",
    ) as caught:
        submit_onboarding_batch(
            db,
            batch_id=batch.id,
            expected_version=batch.version,
            dry_run_fingerprint=fingerprint,
            idempotency_key="n081-b1-negative-evidence",
            confirmed=True,
            operator=data["admin"],
        )
    assert caught.value.code == "INVENTORY_ONBOARDING_DRY_RUN_STALE"
    assert batch.status == "draft"


def test_fingerprint_covers_audit_and_semi_finished_facts(
    onboarding_db,
) -> None:
    db, data = onboarding_db
    batch, _ = create_onboarding_draft(
        db,
        upload=_upload(
            data["tmp_path"],
            [_semi_values()],
            digest_seed="full-fingerprint",
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
    fingerprint = batch.dry_run_fingerprint
    line = batch.lines[0]

    # Simulate a versioned out-of-band edit that did not call the service
    # invalidation helper. The submit fingerprint must still reject it.
    line.supplier_name = "另一家纸板厂"
    line.remarks = "dry-run 后被改写"
    line.version += 1
    db.commit()

    with pytest.raises(
        InventoryOnboardingError,
        match="库存事实已变化",
    ) as caught:
        submit_onboarding_batch(
            db,
            batch_id=batch.id,
            expected_version=batch.version,
            dry_run_fingerprint=fingerprint,
            idempotency_key="n081-b1-full-fingerprint",
            confirmed=True,
            operator=data["admin"],
        )
    assert caught.value.code == "INVENTORY_ONBOARDING_DRY_RUN_STALE"
    assert batch.status == "draft"


@pytest.mark.parametrize("mutation", ("delete", "same_size_tamper"))
def test_submit_requires_the_original_private_source_file(
    onboarding_db,
    mutation: str,
) -> None:
    db, data = onboarding_db
    upload = _upload(
        data["tmp_path"],
        [_finished_values()],
        digest_seed=f"source-file-{mutation}",
    )
    batch, _ = create_onboarding_draft(
        db,
        upload=upload,
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
    fingerprint = batch.dry_run_fingerprint
    if mutation == "delete":
        upload.private_path.unlink()
    else:
        upload.private_path.write_bytes(b"X" * upload.size)

    with pytest.raises(
        InventoryOnboardingError,
        match="私有盘点源文件",
    ) as caught:
        submit_onboarding_batch(
            db,
            batch_id=batch.id,
            expected_version=batch.version,
            dry_run_fingerprint=fingerprint,
            idempotency_key=f"n081-b1-source-{mutation}",
            confirmed=True,
            operator=data["admin"],
        )

    assert (
        caught.value.code
        == "INVENTORY_ONBOARDING_SOURCE_FILE_STALE"
    )
    assert batch.status == "draft"


def test_imported_ids_must_match_all_human_readable_identity_fields(
    onboarding_db,
) -> None:
    db, data = onboarding_db

    customer_headers = HEADERS + ("customer_id", "product_id")
    customer_values = _finished_values(
        customer_code="N081-O",
        customer_name="N081其他客户",
    ) + (data["customer"].id, data["product"].id)
    customer_batch, _ = create_onboarding_draft(
        db,
        upload=_upload(
            data["tmp_path"],
            [customer_values],
            digest_seed="customer-id-conflict",
            headers=customer_headers,
        ),
        creator=data["admin"],
    )
    db.commit()
    assert (
        "CUSTOMER_IDENTITY_CONFLICT"
        in customer_batch.lines[0].error_codes_json
    )

    product_headers = HEADERS + ("product_id",)
    product_values = _finished_values(
        inventory_code="N081-OTHER",
        product_name="其他客户产品",
        pallet="STK-UAT-FG-PRODUCT-CONFLICT",
    ) + (data["product"].id,)
    product_batch, _ = create_onboarding_draft(
        db,
        upload=_upload(
            data["tmp_path"],
            [product_values],
            digest_seed="product-id-conflict",
            headers=product_headers,
        ),
        creator=data["admin"],
    )
    db.commit()
    assert (
        "PRODUCT_IDENTITY_CONFLICT"
        in product_batch.lines[0].error_codes_json
    )

    other_material = Material(
        code="B=C",
        layer_count=5,
        flute_type="AB",
        is_active=True,
    )
    db.add(other_material)
    db.commit()
    material_headers = HEADERS + ("material_id",)
    material_values = _semi_values(
        pallet="STK-UAT-SI-MATERIAL-CONFLICT"
    ) + (other_material.id,)
    material_batch, _ = create_onboarding_draft(
        db,
        upload=_upload(
            data["tmp_path"],
            [material_values],
            digest_seed="material-id-conflict",
            headers=material_headers,
        ),
        creator=data["admin"],
    )
    db.commit()
    assert (
        "MATERIAL_IDENTITY_CONFLICT"
        in material_batch.lines[0].error_codes_json
    )


def test_semi_finished_snapshot_requires_a_separate_supported_route(
    onboarding_db,
) -> None:
    db, data = onboarding_db
    location = data["locations"]["E1-S01"]
    pallet = InventoryPallet(
        pallet_code="SEMI-SNAPSHOT-ONLY",
        location_id=location.id,
        status="active",
        is_current=True,
        version=1,
        created_by=data["admin"].id,
    )
    db.add(pallet)
    db.flush()
    db.add(
        InventoryPalletItem(
            pallet_id=pallet.id,
            inventory_lot_id=None,
            item_type="semi_finished",
            quantity=20,
            unit="sheets",
            match_status="matched",
            created_by=data["admin"].id,
        )
    )
    db.commit()

    batch, _ = create_onboarding_draft(
        db,
        upload=_upload(
            data["tmp_path"],
            [_semi_values(pallet=pallet.pallet_code)],
            digest_seed="semi-snapshot",
        ),
        creator=data["admin"],
    )
    db.commit()
    line = batch.lines[0]

    assert line.match_status == "blocked"
    assert line.action_decision == "pending"
    assert "SEMI_PALLET_SNAPSHOT_UNSUPPORTED" in line.error_codes_json


def test_line_correction_and_rematch_are_audited_and_exclusion_needs_no_reason(
    onboarding_db,
) -> None:
    db, data = onboarding_db
    batch, _ = create_onboarding_draft(
        db,
        upload=_upload(
            data["tmp_path"],
            [_finished_values()],
            digest_seed="audit-edits",
        ),
        creator=data["admin"],
    )
    db.commit()
    line = batch.lines[0]
    line.remarks = None
    db.commit()

    updated = update_onboarding_line(
        db,
        batch_id=batch.id,
        line_id=line.id,
        expected_version=line.version,
        values={"action_decision": "exclude"},
        operator=data["admin"],
    )
    db.commit()
    assert updated.match_status == "excluded"
    assert updated.remarks is None

    from app.services.inventory_onboarding import rematch_onboarding_batch

    rematch_onboarding_batch(
        db,
        batch_id=batch.id,
        expected_version=batch.version,
        operator=data["admin"],
    )
    db.commit()
    actions = {
        row.action for row in db.scalars(select(OperationLog)).all()
    }
    assert "N081_B1_LINE_UPDATE" in actions
    assert "N081_B1_REMATCH" in actions
    line_update = db.scalar(
        select(OperationLog)
        .where(OperationLog.action == "N081_B1_LINE_UPDATE")
        .order_by(OperationLog.id.desc())
    )
    assert line_update is not None
    assert "不计入本次盘点" in line_update.description


def test_batch_blocks_one_new_pallet_across_multiple_locations(
    onboarding_db,
) -> None:
    db, data = onboarding_db
    batch, _ = create_onboarding_draft(
        db,
        upload=_upload(
            data["tmp_path"],
            [
                _finished_values(
                    location="E1-R01",
                    pallet="BATCH-PALLET-CONFLICT",
                    quantity=12,
                ),
                _finished_values(
                    location="E1-R02",
                    pallet="BATCH-PALLET-CONFLICT",
                    quantity=13,
                ),
            ],
            digest_seed="pallet-multiple-locations",
        ),
        creator=data["admin"],
    )
    db.commit()

    assert all(line.match_status == "blocked" for line in batch.lines)
    assert all(
        "BATCH_PALLET_MULTIPLE_LOCATIONS" in line.error_codes_json
        for line in batch.lines
    )
    assert all(line.action_decision == "pending" for line in batch.lines)


def test_batch_blocks_multiple_new_pallets_in_one_location(
    onboarding_db,
) -> None:
    db, data = onboarding_db
    batch, _ = create_onboarding_draft(
        db,
        upload=_upload(
            data["tmp_path"],
            [
                _finished_values(
                    pallet="BATCH-LOCATION-CONFLICT-A",
                    quantity=12,
                ),
                _finished_values(
                    pallet="BATCH-LOCATION-CONFLICT-B",
                    quantity=13,
                ),
            ],
            digest_seed="location-multiple-pallets",
        ),
        creator=data["admin"],
    )
    db.commit()

    assert all(line.match_status == "blocked" for line in batch.lines)
    assert all(
        "BATCH_LOCATION_MULTIPLE_NEW_PALLETS"
        in line.error_codes_json
        for line in batch.lines
    )
    assert all(line.action_decision == "pending" for line in batch.lines)


def test_same_new_pallet_and_location_can_contain_distinct_products(
    onboarding_db,
) -> None:
    db, data = onboarding_db
    second_product = Product(
        customer_id=data["customer"].id,
        product_code="N081-P002",
        customer_material_code="N081-P002",
        product_name="N081第二款成品",
        box_category="normal",
        material_id=data["material"].id,
        default_material_code=data["material"].code,
        flute_type="AB",
        layer_count=5,
    )
    db.add(second_product)
    db.commit()
    batch, _ = create_onboarding_draft(
        db,
        upload=_upload(
            data["tmp_path"],
            [
                _finished_values(
                    pallet="BATCH-MULTI-ITEM",
                    quantity=12,
                ),
                _finished_values(
                    pallet="BATCH-MULTI-ITEM",
                    inventory_code=second_product.product_code,
                    product_name=second_product.product_name,
                    quantity=13,
                ),
            ],
            digest_seed="same-pallet-distinct-products",
        ),
        creator=data["admin"],
    )
    db.commit()

    assert all(line.match_status == "ready" for line in batch.lines)
    assert all(line.action_decision == "create_new" for line in batch.lines)
    assert all(line.error_codes_json == [] for line in batch.lines)


def test_transferred_general_finished_lot_uses_formal_detail_as_authority(
    onboarding_db,
) -> None:
    db, data = onboarding_db
    location = data["locations"]["E1-R01"]
    pallet = InventoryPallet(
        pallet_code="TRANSFERRED-GENERAL-FINISHED",
        location_id=location.id,
        status="active",
        is_current=True,
        version=2,
        created_by=data["admin"].id,
    )
    lot = InventoryLot(
        lot_number="LOT-TRANSFERRED-GENERAL",
        inventory_type="finished",
        warehouse_location_id=location.id,
        quantity_available=24,
        quantity_reserved=0,
        quantity_consumed=0,
        quantity_damaged=0,
        quantity_scrapped=0,
        unit="boxes",
        status="active",
        source_type="stocktake",
        stock_date=date(2026, 7, 1),
        stock_date_accuracy="exact",
        last_movement_at=datetime(2026, 7, 1, 8, 0, 0),
        version=3,
        created_by=data["admin"].id,
    )
    db.add_all([pallet, lot])
    db.flush()
    lot.finished_detail = FinishedGoodsInventoryDetail(
        owner_customer_id=None,
        owner_customer_name_snapshot=None,
        is_general=True,
        product_id=data["product"].id,
        inventory_code_snapshot="N081-P001",
        product_name_snapshot="N081成品纸箱",
    )
    # transfer_to_general historically updates the formal detail but leaves
    # this physical projection's customer snapshot unchanged.
    db.add(
        InventoryPalletItem(
            pallet_id=pallet.id,
            inventory_lot_id=lot.id,
            customer_id=data["customer"].id,
            product_id=data["product"].id,
            inventory_code="N081-P001",
            customer_name_snapshot="N081测试客户",
            product_name="N081成品纸箱",
            item_type="finished",
            quantity=24,
            unit="boxes",
            match_status="matched",
            created_by=data["admin"].id,
        )
    )
    db.commit()
    values = list(
        _finished_values(
            pallet=pallet.pallet_code,
            quantity=12,
        )
    )
    values[3] = "通用"

    batch, _ = create_onboarding_draft(
        db,
        upload=_upload(
            data["tmp_path"],
            [tuple(values)],
            digest_seed="transferred-general",
        ),
        creator=data["admin"],
    )
    db.commit()
    line = batch.lines[0]

    assert line.match_status == "routed"
    assert line.action_decision == "route_n035"
    assert line.existing_lot_id == lot.id
