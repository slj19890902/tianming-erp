from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload, sessionmaker


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.database import create_sqlite_engine  # noqa: E402
from app.models.audit import OperationLog  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.warehouse_inventory import (  # noqa: E402
    Floor3LocationLayout,
    InventoryLot,
    InventoryLotTransfer,
    InventoryMovement,
    InventoryPallet,
    InventoryPalletItem,
    InventoryReservation,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseLocation,
)
from app.services.audit_log import append_audit_event  # noqa: E402
from app.services.location_candidates import operational_location_issue  # noqa: E402
from app.core.time_contract import utc_now_naive  # noqa: E402
from app.services.warehouse_area_activation import (  # noqa: E402
    publish_floor_area_policies,
)
from app.services.warehouse_floor1_candidate_planner import (  # noqa: E402
    measured_pallet_slots_for_zone,
)
from app.services.warehouse_inventory import (  # noqa: E402
    record_location_transfer_without_quantity_change,
)
from app.services.warehouse_twin_layout import (  # noqa: E402
    load_warehouse_twin_floor,
)
from app.services import warehouse_twin_layout_editor as layout_editor  # noqa: E402


CONFIRMATION = "APPLY_A1_100_TO_ZONE_3F_FG_004"
SOURCE = "scripts.admin.merge_a1_100_to_fg004"
ACTOR_USERNAME = "tmbz"

SOURCE_AREA_ID = 23
SOURCE_AREA_CODE = "A1"
EMPTY_LEGACY_AREA_ID = 28
EMPTY_LEGACY_AREA_CODE = "A2"
SOURCE_LOCATION_ID = 1
SOURCE_LOCATION_CODE = "1FA"
SOURCE_LOT_ID = 238
SOURCE_LOT_NUMBER = "SI-20260814-561F5E5D83"
SOURCE_LOT_VERSION = 2
MOVE_QUANTITY = 100

TARGET_FLOOR_ID = 1
TARGET_FLOOR_CODE = "3F"
TARGET_FLOOR_NUMBER = 3
TARGET_AREA_ID = 26
TARGET_AREA_CODE = "FG-004"
TARGET_AREA_NAME = "成品存放（可临时混放）堆放区"
TARGET_FEATURE_ID = "36330234-afbf-4e25-979f-19b835d81541"
TARGET_FEATURE_CODE = "ZONE-3F-FG-004"
TARGET_LOCATION_CODE = "3F-FG-004-L001"
TARGET_LOCATION_NAME = f"{TARGET_AREA_NAME} 001 号位"
TARGET_ALLOWED_TYPES = ["finished", "semi_finished", "raw_material"]
TARGET_STORAGE_LAYOUT = "pallet_ground"

TRANSFER_KEY = "A1-LOT238-100-TO-3F-FG004-20260814"
MOVEMENT_KEY = f"{TRANSFER_KEY}-MOVEMENT"
MAP_OPERATION_KEY = "A1-100-TO-3F-FG004-MAP-20260814"
ARCHIVE_NOTE = "2026-08-14 老板指定将旧区域实物并入实测地图；原区域为空后停止使用。"

PROTECTED_COUNT_TABLES = (
    "sales_orders",
    "sales_order_items",
    "incoming_receipts",
    "incoming_receipt_items",
    "supplier_requisition_orders",
    "supplier_requisition_order_items",
    "delivery_orders",
    "delivery_order_items",
    "production_tasks",
    "inventory_reservations",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sqlite_checks(path: Path) -> dict[str, Any]:
    with sqlite3.connect(path, timeout=30) as connection:
        connection.execute("PRAGMA busy_timeout=30000")
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        revision_row = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        counts = {
            table: int(
                connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
            for table in PROTECTED_COUNT_TABLES
            if table in names
        }
    return {
        "integrity_check": integrity,
        "foreign_key_violations": len(foreign_keys),
        "revision": str(revision_row[0]) if revision_row else None,
        "protected_counts": counts,
    }


def sqlite_backup(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source, timeout=30) as source_db, sqlite3.connect(
        target, timeout=30
    ) as target_db:
        source_db.execute("PRAGMA busy_timeout=30000")
        source_db.backup(target_db)
    checks = sqlite_checks(target)
    if checks["integrity_check"] != "ok" or checks["foreign_key_violations"]:
        target.unlink(missing_ok=True)
        raise RuntimeError("数据库备份完整性校验失败")


def _policy_types(policy: WarehouseAreaStoragePolicy) -> list[str]:
    try:
        values = json.loads(policy.allowed_inventory_types_json or "[]")
    except json.JSONDecodeError as error:
        raise RuntimeError("FG-004 正式策略用途无法解析") from error
    return [str(value) for value in values]


def _feature(floor: dict[str, Any], feature_id: str) -> dict[str, Any]:
    row = next(
        (
            item
            for item in floor.get("features") or []
            if str(item.get("id") or "") == feature_id
        ),
        None,
    )
    if not isinstance(row, dict):
        raise RuntimeError(f"地图缺少目标区域 {TARGET_FEATURE_CODE}")
    return row


def _append_note(current: str | None, note: str) -> str:
    text = str(current or "").strip()
    if note in text:
        return text
    return f"{text}\n{note}".strip()


def _lot_snapshot(lot: InventoryLot) -> dict[str, Any]:
    return {
        "id": lot.id,
        "lot_number": lot.lot_number,
        "inventory_type": lot.inventory_type,
        "location_id": lot.warehouse_location_id,
        "available": int(lot.quantity_available or 0),
        "reserved": int(lot.quantity_reserved or 0),
        "consumed": int(lot.quantity_consumed or 0),
        "damaged": int(lot.quantity_damaged or 0),
        "scrapped": int(lot.quantity_scrapped or 0),
        "unit": lot.unit,
        "status": lot.status,
        "version": int(lot.version or 0),
        "source_type": lot.source_type,
        "source_ref_type": lot.source_ref_type,
        "source_ref_id": lot.source_ref_id,
    }


def _area_snapshot(area: WarehouseArea) -> dict[str, Any]:
    return {
        "id": area.id,
        "floor_id": area.floor_id,
        "area_code": area.area_code,
        "area_name": area.area_name,
        "planned_location_count": area.planned_location_count,
        "planned_pallet_capacity": area.planned_pallet_capacity,
        "construction_status": area.construction_status,
        "capacity_review_status": area.capacity_review_status,
        "capacity_eligible": area.capacity_eligible,
        "confirmed_pallet_capacity": area.confirmed_pallet_capacity,
    }


def _require_actor(db: Session) -> User:
    actor = db.scalar(select(User).where(User.username == ACTOR_USERNAME))
    if actor is None or actor.id != 6 or actor.role != "boss":
        raise RuntimeError("老板端审计账号 tmbz/id=6/role=boss 与预期不一致")
    return actor


def _build_ready_plan(
    db: Session, *, published_path: Path, draft_path: Path
) -> dict[str, Any]:
    actor = _require_actor(db)
    source_area = db.get(WarehouseArea, SOURCE_AREA_ID)
    empty_area = db.get(WarehouseArea, EMPTY_LEGACY_AREA_ID)
    target_area = db.scalar(
        select(WarehouseArea)
        .options(selectinload(WarehouseArea.floor), selectinload(WarehouseArea.storage_policy))
        .where(WarehouseArea.id == TARGET_AREA_ID)
    )
    source_location = db.get(WarehouseLocation, SOURCE_LOCATION_ID)
    lot = db.get(InventoryLot, SOURCE_LOT_ID)
    if None in (source_area, empty_area, target_area, source_location, lot):
        raise RuntimeError("正式源区域、目标区域、源库位或目标批次缺失")
    assert source_area is not None
    assert empty_area is not None
    assert target_area is not None
    assert source_location is not None
    assert lot is not None

    repeated = db.scalar(
        select(InventoryLotTransfer).where(
            InventoryLotTransfer.idempotency_key == TRANSFER_KEY
        )
    )
    if repeated is not None:
        target_location = db.get(WarehouseLocation, repeated.target_location_id)
        if (
            repeated.source_lot_id == SOURCE_LOT_ID
            and repeated.target_lot_id == SOURCE_LOT_ID
            and repeated.quantity == MOVE_QUANTITY
            and target_location is not None
            and target_location.location_code == TARGET_LOCATION_CODE
            and lot.warehouse_location_id == target_location.id
            and source_area.capacity_review_status == "excluded"
            and empty_area.capacity_review_status == "excluded"
            and target_area.storage_policy is not None
            and target_area.storage_policy.status == "published"
        ):
            return {
                "status": "already_applied",
                "actor_id": actor.id,
                "lot": _lot_snapshot(lot),
                "target_location_id": target_location.id,
                "target_location_code": target_location.location_code,
            }
        raise RuntimeError("已存在同操作键但结果不完整，拒绝重复执行")

    if (
        source_area.floor_id != 2
        or source_area.area_code.upper() != SOURCE_AREA_CODE
        or source_area.storage_policy is not None
    ):
        raise RuntimeError("旧 A1 区域身份与预期不一致")
    if (
        empty_area.floor_id != 2
        or empty_area.area_code.upper() != EMPTY_LEGACY_AREA_CODE
        or empty_area.storage_policy is not None
    ):
        raise RuntimeError("旧 A2 区域身份与预期不一致")
    if (
        source_location.location_code != SOURCE_LOCATION_CODE
        or source_location.warehouse_floor != 1
        or str(source_location.area_code or "").upper() != SOURCE_AREA_CODE
        or not source_location.is_active
    ):
        raise RuntimeError("旧 A1 实体库位与预期不一致")
    lot_state = _lot_snapshot(lot)
    expected_lot_state = {
        "id": SOURCE_LOT_ID,
        "lot_number": SOURCE_LOT_NUMBER,
        "inventory_type": "semi_finished",
        "location_id": SOURCE_LOCATION_ID,
        "available": MOVE_QUANTITY,
        "reserved": 0,
        "consumed": 0,
        "damaged": 0,
        "scrapped": 0,
        "unit": "sheets",
        "status": "active",
        "version": SOURCE_LOT_VERSION,
        "source_type": "replenishment",
        "source_ref_type": "stock_replenishment_receipt",
        "source_ref_id": 188,
    }
    if lot_state != expected_lot_state:
        raise RuntimeError(f"100 张半成品批次已变化：{lot_state}")
    active_reservations = int(
        db.scalar(
            select(func.count(InventoryReservation.id)).where(
                InventoryReservation.inventory_lot_id == lot.id,
                InventoryReservation.status.in_(("active", "partial")),
            )
        )
        or 0
    )
    pallet_items = int(
        db.scalar(
            select(func.count(InventoryPalletItem.id)).where(
                InventoryPalletItem.inventory_lot_id == lot.id
            )
        )
        or 0
    )
    source_pallets = int(
        db.scalar(
            select(func.count(InventoryPallet.id)).where(
                InventoryPallet.location_id == source_location.id,
                InventoryPallet.is_current.is_(True),
            )
        )
        or 0
    )
    if active_reservations or pallet_items or source_pallets:
        raise RuntimeError("100 张批次存在预占或实体栈板绑定，拒绝直接合并")

    if (
        target_area.floor_id != TARGET_FLOOR_ID
        or target_area.floor is None
        or target_area.floor.floor_number != TARGET_FLOOR_NUMBER
        or target_area.floor.floor_code.upper() != TARGET_FLOOR_CODE
        or target_area.area_code.upper() != TARGET_AREA_CODE
        or target_area.area_name != TARGET_AREA_NAME
        or target_area.capacity_review_status != "confirmed"
        or target_area.confirmed_pallet_capacity != 12
        or not target_area.capacity_eligible
        or target_area.storage_policy is None
    ):
        raise RuntimeError("FG-004 正式区域或容量状态与预期不一致")
    policy = target_area.storage_policy
    if (
        policy.map_feature_id != TARGET_FEATURE_ID
        or policy.status != "draft"
        or _policy_types(policy) != TARGET_ALLOWED_TYPES
        or policy.storage_layout != TARGET_STORAGE_LAYOUT
    ):
        raise RuntimeError("FG-004 策略身份或用途与预期不一致")
    target_location_count = int(
        db.scalar(
            select(func.count(WarehouseLocation.id)).where(
                WarehouseLocation.warehouse_floor == TARGET_FLOOR_NUMBER,
                func.upper(WarehouseLocation.area_code) == TARGET_AREA_CODE,
            )
        )
        or 0
    )
    if target_location_count:
        raise RuntimeError("FG-004 已有库位，拒绝覆盖或猜测目标位置")
    empty_area_location_count = int(
        db.scalar(
            select(func.count(WarehouseLocation.id)).where(
                WarehouseLocation.warehouse_floor == 1,
                func.upper(WarehouseLocation.area_code) == EMPTY_LEGACY_AREA_CODE,
            )
        )
        or 0
    )
    if empty_area_location_count:
        raise RuntimeError("旧 A2 仍有库位，拒绝自动停止使用")

    published_floor = load_warehouse_twin_floor(
        TARGET_FLOOR_CODE, path=published_path
    )
    published_feature = _feature(published_floor, TARGET_FEATURE_ID)
    if (
        str(published_feature.get("feature_code") or "") != TARGET_FEATURE_CODE
        or published_feature.get("feature_kind") != "zone"
        or published_feature.get("erp_area_code") not in (None, "")
    ):
        raise RuntimeError("正式实测地图目标区域已变化或已绑定")
    slots = measured_pallet_slots_for_zone(
        published_floor, feature_id=TARGET_FEATURE_ID
    )
    if not slots:
        raise RuntimeError("FG-004 实测边界无法生成安全栈板位置")

    draft_document = json.loads(draft_path.read_text(encoding="utf-8"))
    draft_floor = (draft_document.get("floors") or {}).get(TARGET_FLOOR_CODE)
    if not isinstance(draft_floor, dict):
        raise RuntimeError("高级维护草稿缺少 3F")
    draft_feature = _feature(draft_floor, TARGET_FEATURE_ID)
    if (
        str(draft_feature.get("erp_area_code") or "").upper() != TARGET_AREA_CODE
        or list(draft_feature.get("allowed_inventory_types") or [])
        != TARGET_ALLOWED_TYPES
        or str(draft_feature.get("storage_layout") or "")
        != TARGET_STORAGE_LAYOUT
    ):
        raise RuntimeError("FG-004 高级维护草稿不是已确认的混合栈板区域")

    return {
        "status": "ready",
        "actor_id": actor.id,
        "source_area": _area_snapshot(source_area),
        "empty_legacy_area": _area_snapshot(empty_area),
        "target_area": _area_snapshot(target_area),
        "target_policy_id": policy.id,
        "source_location": {
            "id": source_location.id,
            "code": source_location.location_code,
        },
        "lot": lot_state,
        "target_feature": {
            "id": TARGET_FEATURE_ID,
            "feature_code": TARGET_FEATURE_CODE,
            "safe_standard_slot_count": len(slots),
            "selected_slot": slots[0],
        },
        "protected_counts": sqlite_checks(Path(str(db.bind.url.database)))[
            "protected_counts"
        ],
    }


def inspect_plan(
    *, database: Path, published_path: Path, draft_path: Path
) -> dict[str, Any]:
    engine = create_sqlite_engine(database)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as db:
            return _build_ready_plan(
                db, published_path=published_path, draft_path=draft_path
            )
    finally:
        engine.dispose()


@contextmanager
def _layout_paths(
    *, published_path: Path, draft_path: Path, backup_dir: Path
) -> Iterator[None]:
    previous = (
        layout_editor.TWIN_LAYOUT_PATH,
        layout_editor.TWIN_LAYOUT_DRAFT_PATH,
        layout_editor.TWIN_LAYOUT_BACKUP_DIR,
    )
    layout_editor.TWIN_LAYOUT_PATH = published_path
    layout_editor.TWIN_LAYOUT_DRAFT_PATH = draft_path
    layout_editor.TWIN_LAYOUT_BACKUP_DIR = backup_dir
    try:
        yield
    finally:
        (
            layout_editor.TWIN_LAYOUT_PATH,
            layout_editor.TWIN_LAYOUT_DRAFT_PATH,
            layout_editor.TWIN_LAYOUT_BACKUP_DIR,
        ) = previous


def _create_target_location(
    db: Session, *, area: WarehouseArea, slot: dict[str, Any], actor_id: int
) -> WarehouseLocation:
    next_sort = int(db.scalar(select(func.max(WarehouseLocation.sort_order))) or 0) + 1
    row = WarehouseLocation(
        location_code=TARGET_LOCATION_CODE,
        location_name=TARGET_LOCATION_NAME,
        warehouse_type="shared",
        warehouse_floor=TARGET_FLOOR_NUMBER,
        area_code=TARGET_AREA_CODE,
        storage_type="ground",
        sort_order=next_sort,
        is_temporary=False,
        source_version="TWIN_V1",
        placement_status="placed",
        is_active=True,
        remarks="老板指定：旧 A1 的 100 张半成品并入 3F 实测 FG-004 区。",
    )
    row.floor3_layout = Floor3LocationLayout(
        left_pct=Decimal(str(slot["left_pct"])),
        top_pct=Decimal(str(slot["top_pct"])),
        width_pct=Decimal(str(slot["width_pct"])),
        height_pct=Decimal(str(slot["height_pct"])),
        z_index=0,
        version=1,
        source_type="manual",
        created_by=actor_id,
        updated_by=actor_id,
    )
    db.add(row)
    area.planned_location_count = 1
    db.flush()
    return row


def _publish_target_zone_only(
    db: Session,
    *,
    actor: User,
    published_path: Path,
    draft_path: Path,
    layout_backup_dir: Path,
    target_location: WarehouseLocation,
) -> dict[str, Any]:
    published_floor = load_warehouse_twin_floor(
        TARGET_FLOOR_CODE, path=published_path
    )
    published_feature = _feature(published_floor, TARGET_FEATURE_ID)
    published_revision = str(published_floor.get("revision") or "")
    published_version = int(published_feature.get("version") or 1)
    draft_snapshot = layout_editor.snapshot_warehouse_twin_layout_draft(
        draft_path=draft_path
    )
    context = layout_editor.LayoutOneStepDraftContext(
        draft_snapshot=draft_snapshot,
        had_active_draft=draft_snapshot.existed,
        published_floor_revision=published_revision,
        published_feature_version=published_version,
    )
    draft_path.unlink(missing_ok=True)
    mutation = layout_editor.update_warehouse_twin_zone_policy(
        TARGET_FLOOR_CODE,
        TARGET_FEATURE_ID,
        expected_revision=published_revision,
        expected_version=published_version,
        operation_key=f"{MAP_OPERATION_KEY}-policy",
        allowed_inventory_types=TARGET_ALLOWED_TYPES,
        storage_layout=TARGET_STORAGE_LAYOUT,
        erp_area_code=TARGET_AREA_CODE,
        area_name=TARGET_AREA_NAME,
        formal_area_id=TARGET_AREA_ID,
        formal_floor_id=TARGET_FLOOR_ID,
    )
    validation = layout_editor.validate_warehouse_twin_layout_draft(
        TARGET_FLOOR_CODE,
        expected_revision=mutation.floor_revision,
        published_path=published_path,
        draft_path=draft_path,
    )
    blockers = list(validation.value.get("blockers") or [])
    if blockers:
        raise RuntimeError("FG-004 选择性发布校验失败：" + "；".join(blockers[:5]))
    published = layout_editor.publish_warehouse_twin_layout_draft(
        TARGET_FLOOR_CODE,
        expected_published_revision=published_revision,
        expected_draft_revision=mutation.floor_revision,
        operation_key=f"{MAP_OPERATION_KEY}-publish",
        published_path=published_path,
        draft_path=draft_path,
        backup_dir=layout_backup_dir,
    )
    current_floor = load_warehouse_twin_floor(
        TARGET_FLOOR_CODE, path=published_path
    )
    current_feature = _feature(current_floor, TARGET_FEATURE_ID)
    policies = publish_floor_area_policies(
        db,
        floor_code=TARGET_FLOOR_CODE,
        published_revision=str(published.value["published_revision"]),
        operator_id=actor.id,
        # This is an explicitly scoped one-zone repair.  Legacy map features may
        # still carry old display-only area codes without a complete policy;
        # publishing those unrelated rows here would silently broaden the data
        # operation and is therefore forbidden.
        published_features=[current_feature],
    )
    target_policy = db.scalar(
        select(WarehouseAreaStoragePolicy).where(
            WarehouseAreaStoragePolicy.area_id == TARGET_AREA_ID
        )
    )
    if (
        target_policy is None
        or target_policy.status != "published"
        or target_policy.published_map_revision != published.value["published_revision"]
    ):
        raise RuntimeError("FG-004 地图已写入但正式策略未同步发布")
    issue = operational_location_issue(
        db,
        target_location,
        warehouse_types={"semi_finished", "shared"},
        pallet_storage_only=True,
        require_published=True,
        require_map_geometry=True,
        required_inventory_type="semi_finished",
        require_empty=True,
    )
    if issue:
        raise RuntimeError(f"FG-004 新货位不可用于本次合并：{issue}")
    advanced_preserved = layout_editor.rebase_warehouse_twin_advanced_draft_after_one_step(
        context,
        TARGET_FLOOR_CODE,
        TARGET_FEATURE_ID,
        published_path=published_path,
        draft_path=draft_path,
    )
    return {
        **published.value,
        "policy_ids": [row.id for row in policies],
        "advanced_draft_preserved": advanced_preserved,
    }


def _move_lot_and_archive_old_areas(
    db: Session,
    *,
    actor: User,
    source_area: WarehouseArea,
    empty_area: WarehouseArea,
    source_location: WarehouseLocation,
    target_location: WarehouseLocation,
    lot: InventoryLot,
) -> dict[str, Any]:
    before = _lot_snapshot(lot)
    now = utc_now_naive()
    updated = db.execute(
        update(InventoryLot)
        .where(
            InventoryLot.id == SOURCE_LOT_ID,
            InventoryLot.version == SOURCE_LOT_VERSION,
            InventoryLot.warehouse_location_id == SOURCE_LOCATION_ID,
            InventoryLot.quantity_available == MOVE_QUANTITY,
            InventoryLot.quantity_reserved == 0,
            InventoryLot.quantity_consumed == 0,
            InventoryLot.quantity_damaged == 0,
            InventoryLot.quantity_scrapped == 0,
            InventoryLot.status == "active",
        )
        .values(
            warehouse_location_id=target_location.id,
            version=InventoryLot.version + 1,
            last_movement_at=now,
        )
    )
    if updated.rowcount != 1:
        raise RuntimeError("100 张批次在执行时发生变化，已停止合并")
    db.flush()
    db.expire(lot)
    lot = db.get(InventoryLot, SOURCE_LOT_ID)
    assert lot is not None
    request_hash = hashlib.sha256(
        json.dumps(
            {
                "lot_id": SOURCE_LOT_ID,
                "source_location_id": SOURCE_LOCATION_ID,
                "target_location_id": target_location.id,
                "quantity": MOVE_QUANTITY,
                "expected_version": SOURCE_LOT_VERSION,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    transfer = InventoryLotTransfer(
        source_lot_id=SOURCE_LOT_ID,
        target_lot_id=SOURCE_LOT_ID,
        source_location_id=SOURCE_LOCATION_ID,
        target_location_id=target_location.id,
        quantity=MOVE_QUANTITY,
        available_quantity=MOVE_QUANTITY,
        reserved_quantity=0,
        source_version_before=SOURCE_LOT_VERSION,
        source_version_after=SOURCE_LOT_VERSION + 1,
        idempotency_key=TRANSFER_KEY,
        request_hash=request_hash,
        transferred_by=actor.id,
        transferred_at=now,
    )
    db.add(transfer)
    record_location_transfer_without_quantity_change(
        db,
        lot=lot,
        operator_id=actor.id,
        idempotency_key=MOVEMENT_KEY,
        remarks=(
            f"老板指定数据合并：{SOURCE_LOCATION_CODE} -> {TARGET_LOCATION_CODE}；"
            f"批次 {SOURCE_LOT_NUMBER}，100 张，数量不变"
        ),
    )
    source_location.is_active = False
    source_location.remarks = _append_note(source_location.remarks, ARCHIVE_NOTE)
    reviewed_by = actor.display_name or actor.real_name or actor.username
    for area in (source_area, empty_area):
        area.planned_location_count = 0
        area.planned_pallet_capacity = 0
        area.construction_status = "ledger_building"
        area.capacity_review_status = "excluded"
        area.capacity_eligible = False
        area.confirmed_pallet_capacity = None
        area.capacity_reviewed_by = reviewed_by
        area.capacity_reviewed_at = now
        area.remarks = _append_note(area.remarks, ARCHIVE_NOTE)
    db.flush()
    after = _lot_snapshot(lot)
    return {
        "before": before,
        "after": after,
        "transfer_id": transfer.id,
        "target_location_id": target_location.id,
    }


def apply_merge(
    *,
    database: Path,
    published_path: Path,
    draft_path: Path,
    backup_dir: Path,
    expected_database_sha256: str,
) -> dict[str, Any]:
    source_sha = sha256_file(database)
    if source_sha != expected_database_sha256:
        raise RuntimeError("正式数据库指纹已变化，拒绝执行")
    checks_before = sqlite_checks(database)
    if (
        checks_before["integrity_check"] != "ok"
        or checks_before["foreign_key_violations"]
        or checks_before["revision"] != "mm21v8x9z10"
    ):
        raise RuntimeError(f"数据库执行前门禁未通过：{checks_before}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_dir.mkdir(parents=True, exist_ok=True)
    database_backup = backup_dir / f"carton_erp_before_A1_to_FG004_{stamp}.sqlite3"
    published_backup = backup_dir / f"twin_layout_before_A1_to_FG004_{stamp}.json"
    draft_backup = backup_dir / f"twin_layout_draft_before_A1_to_FG004_{stamp}.json"
    sqlite_backup(database, database_backup)
    shutil.copy2(published_path, published_backup)
    shutil.copy2(draft_path, draft_backup)
    if sha256_file(published_path) != sha256_file(published_backup):
        raise RuntimeError("正式地图备份校验失败")
    if sha256_file(draft_path) != sha256_file(draft_backup):
        raise RuntimeError("地图草稿备份校验失败")

    engine = create_sqlite_engine(database)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    publish_snapshot = layout_editor.snapshot_warehouse_twin_publish_state(
        published_path=published_path, draft_path=draft_path
    )
    layout_publish_result: dict[str, Any] | None = None
    try:
        with factory() as db, _layout_paths(
            published_path=published_path,
            draft_path=draft_path,
            backup_dir=backup_dir,
        ):
            plan = _build_ready_plan(
                db, published_path=published_path, draft_path=draft_path
            )
            if plan["status"] == "already_applied":
                db.rollback()
                return {"changed": False, "plan": plan}
            actor = _require_actor(db)
            source_area = db.get(WarehouseArea, SOURCE_AREA_ID)
            empty_area = db.get(WarehouseArea, EMPTY_LEGACY_AREA_ID)
            target_area = db.scalar(
                select(WarehouseArea)
                .options(selectinload(WarehouseArea.floor), selectinload(WarehouseArea.storage_policy))
                .where(WarehouseArea.id == TARGET_AREA_ID)
            )
            source_location = db.get(WarehouseLocation, SOURCE_LOCATION_ID)
            lot = db.get(InventoryLot, SOURCE_LOT_ID)
            assert source_area and empty_area and target_area and source_location and lot
            target_location = _create_target_location(
                db,
                area=target_area,
                slot=plan["target_feature"]["selected_slot"],
                actor_id=actor.id,
            )
            layout_publish_result = _publish_target_zone_only(
                db,
                actor=actor,
                published_path=published_path,
                draft_path=draft_path,
                layout_backup_dir=backup_dir,
                target_location=target_location,
            )
            transfer_result = _move_lot_and_archive_old_areas(
                db,
                actor=actor,
                source_area=source_area,
                empty_area=empty_area,
                source_location=source_location,
                target_location=target_location,
                lot=lot,
            )
            append_audit_event(
                db,
                event_category="business",
                result="success",
                source="script",
                module_code="warehouse",
                action_code="MERGE_LEGACY_AREA_INTO_MEASURED_ZONE",
                resource="warehouse_inventory_lot",
                legacy_action="WAREHOUSE_DATA_MERGE",
                actor=actor,
                entity_type="inventory_lot",
                entity_id=SOURCE_LOT_ID,
                object_ref=f"{SOURCE_LOCATION_CODE}->{TARGET_LOCATION_CODE}",
                batch_id="A1-100-FG004-20260814",
                description="老板指定将旧 A1 的 100 张半成品并入 3F 实测 FG-004 区域",
                details={
                    "source": SOURCE,
                    "quantity": MOVE_QUANTITY,
                    "unit": "sheets",
                    "inventory_quantity_changed": False,
                    "source_area_archived": SOURCE_AREA_CODE,
                    "empty_legacy_area_archived": EMPTY_LEGACY_AREA_CODE,
                    "target_zone": TARGET_FEATURE_CODE,
                    "target_area": TARGET_AREA_CODE,
                    "target_location": TARGET_LOCATION_CODE,
                    "layout_publish": layout_publish_result,
                    "transfer": transfer_result,
                    "protected_counts_before": checks_before["protected_counts"],
                },
            )
            db.commit()
    except Exception:
        layout_editor.restore_warehouse_twin_publish_state(
            publish_snapshot,
            backup_name=(
                str(layout_publish_result.get("backup_name") or "")
                if layout_publish_result
                else None
            ),
            backup_dir=backup_dir,
        )
        raise
    finally:
        engine.dispose()

    checks_after = sqlite_checks(database)
    if (
        checks_after["integrity_check"] != "ok"
        or checks_after["foreign_key_violations"]
        or checks_after["revision"] != checks_before["revision"]
        or checks_after["protected_counts"] != checks_before["protected_counts"]
    ):
        raise RuntimeError(f"合并后数据库门禁未通过：{checks_after}")
    after = inspect_plan(
        database=database, published_path=published_path, draft_path=draft_path
    )
    if after["status"] != "already_applied":
        raise RuntimeError("合并后状态回读未闭合")
    return {
        "changed": True,
        "source_database_sha256": source_sha,
        "database_sha256_after": sha256_file(database),
        "database_backup": str(database_backup),
        "database_backup_sha256": sha256_file(database_backup),
        "published_backup": str(published_backup),
        "published_backup_sha256": sha256_file(published_backup),
        "draft_backup": str(draft_backup),
        "draft_backup_sha256": sha256_file(draft_backup),
        "checks_before": checks_before,
        "checks_after": checks_after,
        "layout_publish": layout_publish_result,
        "plan_after": after,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="将旧 A1 的 100 张半成品安全合并到 ZONE-3F-FG-004"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--published-layout", type=Path, required=True)
    parser.add_argument("--draft-layout", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirmation")
    parser.add_argument("--expected-database-sha256")
    args = parser.parse_args()
    database = args.database.resolve()
    published = args.published_layout.resolve()
    draft = args.draft_layout.resolve()
    if not database.is_file() or not published.is_file() or not draft.is_file():
        raise RuntimeError("数据库、正式地图或地图草稿不存在")
    if not args.apply:
        result = {
            "database_sha256": sha256_file(database),
            "published_layout_sha256": sha256_file(published),
            "draft_layout_sha256": sha256_file(draft),
            "database_checks": sqlite_checks(database),
            "plan": inspect_plan(
                database=database, published_path=published, draft_path=draft
            ),
        }
    else:
        if args.confirmation != CONFIRMATION:
            raise RuntimeError(f"正式执行必须提供 --confirmation {CONFIRMATION}")
        if not args.expected_database_sha256:
            raise RuntimeError("正式执行必须提供数据库 SHA-256")
        result = apply_merge(
            database=database,
            published_path=published,
            draft_path=draft,
            backup_dir=(
                args.backup_dir.resolve()
                if args.backup_dir
                else database.parent / "backups"
            ),
            expected_database_sha256=args.expected_database_sha256,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
