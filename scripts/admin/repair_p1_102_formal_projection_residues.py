"""Repair the three explicitly authorized P1-102 spatial projection residues.

Dry-run is the default and opens SQLite in read-only/query-only mode.  The
formal apply path is deliberately narrow: exact formal database path, fixed
authorization phrase, canonical plan SHA, verified rehearsal evidence, online
SQLite backup, one BEGIN IMMEDIATE transaction, exact row deltas, and a single
append-only audit event.

This script never changes inventory quantities, lot status/location/version,
reservations, quantity movements, orders, receipts, production, or deliveries.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, selectinload, sessionmaker


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.database import backup_to_nas, create_sqlite_engine  # noqa: E402
from app.core.time_contract import beijing_now_naive  # noqa: E402
from app.models.audit import OperationLog  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.warehouse_inventory import (  # noqa: E402
    Floor3LocationLayout,
    InventoryLocationMovement,
    InventoryLot,
    InventoryPallet,
    InventoryPalletItem,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseGroundLayoutPlan,
    WarehouseGroundLayoutSlot,
    WarehouseGroundOccupancy,
    WarehouseGroundOccupancySlot,
    WarehouseLocation,
)
from app.services.audit_log import append_audit_event  # noqa: E402
from app.services.floor3_locations import clear_pallet  # noqa: E402
from app.services.warehouse_ground_slots import (  # noqa: E402
    release_ground_occupancy_for_pallet,
)


AUTHORIZATION = "P1-102-FORMAL-PROJECTION-RESIDUE-REPAIR"
SOURCE = "scripts.admin.repair_p1_102_formal_projection_residues"
ACTION_CODE = "warehouse.projection_residue.repair"
BATCH_ID = "p1-102-r3-formal-20260825-v1"
FORMAL_DATABASE = Path(r"D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3")
FORMAL_RUNTIME_MAP = Path(
    r"D:\纸箱厂erp软件搭建\data\layout_runtime\twin_layout_v1.json"
)
FORMAL_RUNTIME_SHA256 = (
    "eb7292bd521072345835a0f72490c9ddd41ade5e9b1bac9267ab8ebaa691185c"
)
EXPECTED_ALEMBIC_REVISION = "df40v8x9z29"
EXPECTED_RUNTIME_REVISIONS = {
    "1F": "d27ae0261368c38c",
    "3F": "626699b76d91de90",
}
C1_PALLET_CODE = "PLT-P1-102-R3-C1R13-L42"
C1_MOVEMENT_KEY = "p1-102-r3-c1-r13-l42-bind-v1"
A1_CLEAR_KEY = "p1-102-r3-a1-l05-p148-clear-v1"
BACKUP_SUFFIX = "_FINAL_P1_102_BEFORE_PROJECTION_RESIDUE_REPAIR"

TARGETS = {
    "c1": {
        "location_code": "C1-R13",
        "location_id": 400,
        "lot_id": 42,
        "lot_version": 1,
        "physical_quantity": 20,
        "layout_id": 398,
        "layout_version": 1,
        "area_code": "C1",
        "floor": 3,
        "source_version": "V11",
    },
    "fin": (
        {
            "location_code": "F1-FIN-001-L007",
            "location_id": 429,
            "lot_id": 217,
            "lot_version": 3,
            "physical_quantity": 12,
            "pallet_id": 159,
            "pallet_version": 3,
            "pallet_item_id": 247,
            "slot_id": 7,
        },
        {
            "location_code": "F1-FIN-001-L009",
            "location_id": 431,
            "lot_id": 202,
            "lot_version": 3,
            "physical_quantity": 200,
            "pallet_id": 155,
            "pallet_version": 3,
            "pallet_item_id": 245,
            "slot_id": 9,
        },
    ),
    "a1": {
        "location_code": "A1-L05",
        "location_id": 6,
        "lot_id": 186,
        "lot_version": 14,
        "pallet_id": 148,
        "pallet_version_before": 1,
        "pallet_version_after": 2,
        "pallet_item_id": 215,
        "layout_id": 5,
        "layout_version": 1,
        "area_code": "A1",
        "floor": 3,
        "source_version": "V11",
    },
    "fin_plan": {
        "area_code": "FIN-001",
        "floor": 1,
        "plan_id": 1,
        "plan_version": 2,
        "policy_id": 6,
        "policy_version": 19,
        "published_revision": EXPECTED_RUNTIME_REVISIONS["1F"],
    },
    "area_counts": {
        "C1": {
            "capacity": 24,
            "positive_locations": 24,
            "current_before": 23,
            "current_after": 24,
        },
        "A1": {
            "capacity": 10,
            "positive_locations": 9,
            "current_before": 10,
            "current_after": 9,
        },
        "FIN-001": {
            "current_before": 10,
            "current_after": 10,
        },
    },
}

ALLOWED_DELTAS = {
    "inventory_pallets": 1,
    "inventory_pallet_items": 1,
    "inventory_location_movements": 2,
    "warehouse_ground_occupancies": 2,
    "warehouse_ground_occupancy_slots": 2,
    "operation_logs": 1,
}

PROTECTED_COUNT_TABLES = (
    "inventory_lots",
    "finished_goods_inventory_details",
    "inventory_movements",
    "inventory_reservations",
    "sales_orders",
    "sales_order_items",
    "incoming_receipts",
    "incoming_receipt_items",
    "production_tasks",
    "production_completions",
    "sales_deliveries",
    "sales_delivery_items",
)


class RepairRefused(RuntimeError):
    """Fail-closed signal for any target, authorization, or invariant drift."""


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def script_sha256() -> str:
    return sha256_file(Path(__file__).resolve())


def _physical(lot: InventoryLot) -> int:
    return (
        int(lot.quantity_available or 0)
        + int(lot.quantity_reserved or 0)
        + int(lot.quantity_damaged or 0)
    )


def _lot_snapshot(lot: InventoryLot) -> dict[str, Any]:
    detail = lot.finished_detail
    return {
        "id": int(lot.id),
        "inventory_type": lot.inventory_type,
        "location_id": int(lot.warehouse_location_id),
        "status": lot.status,
        "available": int(lot.quantity_available or 0),
        "reserved": int(lot.quantity_reserved or 0),
        "consumed": int(lot.quantity_consumed or 0),
        "damaged": int(lot.quantity_damaged or 0),
        "scrapped": int(lot.quantity_scrapped or 0),
        "version": int(lot.version),
        "owner_customer_id": (
            int(detail.owner_customer_id)
            if detail is not None and detail.owner_customer_id is not None
            else None
        ),
        "product_id": int(detail.product_id) if detail is not None else None,
    }


def _pallet_item_snapshot(item: InventoryPalletItem) -> dict[str, Any]:
    return {
        "id": int(item.id),
        "pallet_id": int(item.pallet_id),
        "inventory_lot_id": int(item.inventory_lot_id or 0),
        "item_type": item.item_type,
        "quantity": str(Decimal(item.quantity or 0).normalize()),
        "match_status": item.match_status,
    }


def _runtime_identity(runtime_map: Path) -> dict[str, Any]:
    if not runtime_map.is_file():
        raise RepairRefused(f"runtime map does not exist: {runtime_map}")
    payload = json.loads(runtime_map.read_text(encoding="utf-8"))
    floors = payload.get("floors")
    if not isinstance(floors, dict):
        raise RepairRefused("runtime map floors contract is invalid")
    revisions = {
        code: str((floors.get(code) or {}).get("revision") or "")
        for code in EXPECTED_RUNTIME_REVISIONS
    }
    return {
        "sha256": sha256_file(runtime_map),
        "revisions": revisions,
    }


def _alembic_revision(db: Session) -> str:
    rows = [str(row[0]) for row in db.execute(text("SELECT version_num FROM alembic_version"))]
    if len(rows) != 1:
        raise RepairRefused(f"expected one Alembic revision, found {rows}")
    return rows[0]


def _location(db: Session, target: dict[str, Any]) -> WarehouseLocation:
    row = db.scalar(
        select(WarehouseLocation)
        .where(WarehouseLocation.id == int(target["location_id"]))
        .options(selectinload(WarehouseLocation.floor3_layout))
    )
    if row is None or row.location_code != target["location_code"]:
        raise RepairRefused(f"location identity changed for {target['location_code']}")
    return row


def _lot(db: Session, target: dict[str, Any]) -> InventoryLot:
    row = db.scalar(
        select(InventoryLot)
        .where(InventoryLot.id == int(target["lot_id"]))
        .options(
            selectinload(InventoryLot.finished_detail),
            selectinload(InventoryLot.pallet_item),
        )
    )
    if row is None:
        raise RepairRefused(f"inventory lot {target['lot_id']} disappeared")
    return row


def _validate_location(
    db: Session,
    target: dict[str, Any],
    *,
    storage_type: str,
    warehouse_type: str = "finished",
) -> WarehouseLocation:
    row = _location(db, target)
    expected = {
        "active": True,
        "placement": "placed",
        "floor": int(target["floor"]),
        "area": target["area_code"],
        "warehouse_type": warehouse_type,
        "storage_type": storage_type,
        "source_version": target["source_version"],
    }
    actual = {
        "active": bool(row.is_active),
        "placement": row.placement_status,
        "floor": row.warehouse_floor,
        "area": row.area_code,
        "warehouse_type": row.warehouse_type,
        "storage_type": row.storage_type,
        "source_version": row.source_version,
    }
    if actual != expected:
        raise RepairRefused(
            f"location contract changed for {target['location_code']}: {actual}"
        )
    layout = row.floor3_layout
    if (
        layout is None
        or int(layout.id) != int(target["layout_id"])
        or int(layout.version) != int(target["layout_version"])
        # The accepted V11 compatibility projection predates layout_kind and
        # therefore carries ``unknown`` while retaining exact saved geometry.
        or layout.layout_kind not in {"unknown", "physical_pallet", "logical_anchor"}
    ):
        raise RepairRefused(f"map geometry changed for {target['location_code']}")
    return row


def _validate_lot(target: dict[str, Any], lot: InventoryLot) -> None:
    if (
        lot.inventory_type != "finished"
        or lot.status not in {"active", "frozen"}
        or int(lot.warehouse_location_id) != int(target["location_id"])
        or int(lot.version) != int(target["lot_version"])
        or _physical(lot) != int(target["physical_quantity"])
        or lot.finished_detail is None
        or lot.finished_detail.owner_customer_id is None
    ):
        raise RepairRefused(f"protected lot contract changed for lot {target['lot_id']}")


def _active_occupancies_for(
    db: Session, *, pallet_id: int | None = None, location_id: int | None = None
) -> list[WarehouseGroundOccupancy]:
    query = select(WarehouseGroundOccupancy).where(
        WarehouseGroundOccupancy.status == "active"
    )
    if pallet_id is not None:
        query = query.where(WarehouseGroundOccupancy.pallet_id == pallet_id)
    if location_id is not None:
        query = query.join(WarehouseGroundOccupancySlot).where(
            WarehouseGroundOccupancySlot.location_id == location_id,
            WarehouseGroundOccupancySlot.status == "active",
        )
    return list(
        db.scalars(query.options(selectinload(WarehouseGroundOccupancy.slots))).unique()
    )


def _table_counts(db: Session) -> dict[str, int]:
    names = (*PROTECTED_COUNT_TABLES, *ALLOWED_DELTAS)
    existing = {
        str(row[0])
        for row in db.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        )
    }
    return {
        name: int(db.execute(text(f'SELECT COUNT(*) FROM "{name}"')).scalar_one())
        for name in names
        if name in existing
    }


def _inventory_signature(db: Session) -> dict[str, Any]:
    aggregate = db.execute(
        text(
            "SELECT COUNT(*), COALESCE(SUM(quantity_available),0), "
            "COALESCE(SUM(quantity_reserved),0), COALESCE(SUM(quantity_consumed),0), "
            "COALESCE(SUM(quantity_damaged),0), COALESCE(SUM(quantity_scrapped),0), "
            "COALESCE(SUM(version),0) FROM inventory_lots"
        )
    ).one()
    lot_ids = [
        TARGETS["c1"]["lot_id"],
        *(target["lot_id"] for target in TARGETS["fin"]),
        TARGETS["a1"]["lot_id"],
    ]
    target_rows = list(
        db.scalars(
            select(InventoryLot)
            .where(InventoryLot.id.in_(lot_ids))
            .options(selectinload(InventoryLot.finished_detail))
            .order_by(InventoryLot.id)
        )
    )
    reservations = [
        list(row)
        for row in db.execute(
            text(
                "SELECT id, inventory_lot_id, reservation_type, "
                "reserved_stock_quantity, consumed_stock_quantity, "
                "released_stock_quantity, status FROM inventory_reservations "
                "WHERE inventory_lot_id IN (:c1,:f7,:f9,:a1) ORDER BY id"
            ),
            {
                "c1": TARGETS["c1"]["lot_id"],
                "f7": TARGETS["fin"][0]["lot_id"],
                "f9": TARGETS["fin"][1]["lot_id"],
                "a1": TARGETS["a1"]["lot_id"],
            },
        )
    ]
    return {
        "aggregate": [int(value or 0) for value in aggregate],
        "targets": [_lot_snapshot(row) for row in target_rows],
        "reservations": reservations,
    }


def _area_facts(db: Session, area_code: str, floor_number: int) -> dict[str, int | None]:
    area = db.scalar(
        select(WarehouseArea)
        .join(WarehouseFloor, WarehouseFloor.id == WarehouseArea.floor_id)
        .where(
            WarehouseFloor.floor_number == floor_number,
            WarehouseArea.area_code == area_code,
        )
    )
    if area is None or area.construction_status != "enabled":
        raise RepairRefused(f"warehouse area {floor_number}F/{area_code} is unavailable")
    current = int(
        db.scalar(
            select(func.count(InventoryPallet.id))
            .join(WarehouseLocation, WarehouseLocation.id == InventoryPallet.location_id)
            .where(
                WarehouseLocation.warehouse_floor == floor_number,
                WarehouseLocation.area_code == area_code,
                InventoryPallet.is_current.is_(True),
            )
        )
        or 0
    )
    positive_locations = int(
        db.scalar(
            select(func.count(func.distinct(InventoryLot.warehouse_location_id))).where(
                InventoryLot.warehouse_location_id.in_(
                    select(WarehouseLocation.id).where(
                        WarehouseLocation.warehouse_floor == floor_number,
                        WarehouseLocation.area_code == area_code,
                    )
                ),
                InventoryLot.inventory_type == "finished",
                (
                    InventoryLot.quantity_available
                    + InventoryLot.quantity_reserved
                    + InventoryLot.quantity_damaged
                )
                > 0,
            )
        )
        or 0
    )
    return {
        "area_id": int(area.id),
        "capacity": (
            int(area.confirmed_pallet_capacity)
            if area.confirmed_pallet_capacity is not None
            else None
        ),
        "current_pallets": current,
        "positive_locations": positive_locations,
    }


def _build_plan(db: Session, runtime_map: Path) -> dict[str, Any]:
    runtime = _runtime_identity(runtime_map)
    if runtime["sha256"].casefold() != FORMAL_RUNTIME_SHA256.casefold():
        raise RepairRefused("runtime map SHA-256 changed")
    if runtime["revisions"] != EXPECTED_RUNTIME_REVISIONS:
        raise RepairRefused(f"runtime map revisions changed: {runtime['revisions']}")
    revision = _alembic_revision(db)
    if revision != EXPECTED_ALEMBIC_REVISION:
        raise RepairRefused(f"Alembic revision changed: {revision}")

    c1_target = TARGETS["c1"]
    _validate_location(db, c1_target, storage_type="ground")
    c1_lot = _lot(db, c1_target)
    _validate_lot(c1_target, c1_lot)
    c1_positive = list(
        db.scalars(
            select(InventoryLot).where(
                InventoryLot.warehouse_location_id == c1_target["location_id"],
                InventoryLot.inventory_type == "finished",
                (
                    InventoryLot.quantity_available
                    + InventoryLot.quantity_reserved
                    + InventoryLot.quantity_damaged
                )
                > 0,
            )
        )
    )
    if [int(row.id) for row in c1_positive] != [c1_target["lot_id"]]:
        raise RepairRefused("C1-R13 no longer contains exactly the authorized lot")

    c1_code_pallet = db.scalar(
        select(InventoryPallet).where(InventoryPallet.pallet_code == C1_PALLET_CODE)
    )
    c1_current = list(
        db.scalars(
            select(InventoryPallet).where(
                InventoryPallet.location_id == c1_target["location_id"],
                InventoryPallet.is_current.is_(True),
            )
        )
    )
    c1_movement = db.scalar(
        select(InventoryLocationMovement).where(
            InventoryLocationMovement.idempotency_key == C1_MOVEMENT_KEY
        )
    )
    c1_before = (
        c1_lot.pallet_item is None
        and c1_code_pallet is None
        and not c1_current
        and c1_movement is None
        and not _active_occupancies_for(db, location_id=c1_target["location_id"])
    )
    c1_after = False
    if c1_code_pallet is not None:
        items = list(c1_code_pallet.items)
        c1_after = (
            c1_code_pallet.is_current
            and c1_code_pallet.status == "active"
            and c1_code_pallet.location_id == c1_target["location_id"]
            and c1_code_pallet.location_occupancy_key == "PRIMARY"
            and c1_code_pallet.version == 1
            and len(c1_current) == 1
            and c1_current[0].id == c1_code_pallet.id
            and len(items) == 1
            and items[0].inventory_lot_id == c1_lot.id
            and Decimal(items[0].quantity) == Decimal(c1_target["physical_quantity"])
            and c1_lot.pallet_item is not None
            and c1_lot.pallet_item.pallet_id == c1_code_pallet.id
            and c1_movement is not None
            and c1_movement.pallet_id == c1_code_pallet.id
            and c1_movement.to_location_id == c1_target["location_id"]
            and c1_movement.movement_type == "create"
            and not _active_occupancies_for(db, location_id=c1_target["location_id"])
        )

    fin_contracts: list[dict[str, Any]] = []
    fin_before = True
    fin_after = True
    fin_plan_target = TARGETS["fin_plan"]
    floor = db.scalar(
        select(WarehouseFloor).where(
            WarehouseFloor.floor_number == fin_plan_target["floor"]
        )
    )
    area = (
        db.scalar(
            select(WarehouseArea)
            .where(
                WarehouseArea.floor_id == floor.id,
                WarehouseArea.area_code == fin_plan_target["area_code"],
            )
            .options(selectinload(WarehouseArea.storage_policy))
        )
        if floor is not None
        else None
    )
    policy = area.storage_policy if area is not None else None
    plan = db.get(WarehouseGroundLayoutPlan, fin_plan_target["plan_id"])
    if (
        floor is None
        or floor.construction_status != "enabled"
        or area is None
        or area.construction_status != "enabled"
        or policy is None
        or policy.id != fin_plan_target["policy_id"]
        or policy.version != fin_plan_target["policy_version"]
        or policy.status != "published"
        or policy.published_map_revision != fin_plan_target["published_revision"]
        or plan is None
        or plan.area_id != area.id
        or plan.status != "published"
        or plan.version != fin_plan_target["plan_version"]
        or plan.published_map_revision != fin_plan_target["published_revision"]
    ):
        raise RepairRefused("FIN-001 published map/ground plan contract changed")

    for target in TARGETS["fin"]:
        location_target = {
            **target,
            "floor": fin_plan_target["floor"],
            "area_code": fin_plan_target["area_code"],
            "source_version": "TWIN_V1",
        }
        location = _location(db, location_target)
        if (
            not location.is_active
            or location.placement_status != "placed"
            or location.warehouse_floor != fin_plan_target["floor"]
            or location.area_code != fin_plan_target["area_code"]
            or location.warehouse_type not in {"finished", "shared"}
            or location.storage_type != "ground"
            or location.source_version != "TWIN_V1"
            or location.floor3_layout is None
        ):
            raise RepairRefused(f"FIN location changed: {target['location_code']}")
        lot = _lot(db, target)
        _validate_lot(target, lot)
        pallet = db.scalar(
            select(InventoryPallet)
            .where(InventoryPallet.id == target["pallet_id"])
            .options(selectinload(InventoryPallet.items))
        )
        item = db.get(InventoryPalletItem, target["pallet_item_id"])
        slot = db.get(WarehouseGroundLayoutSlot, target["slot_id"])
        if (
            pallet is None
            or not pallet.is_current
            or pallet.status != "active"
            or pallet.location_id != target["location_id"]
            or pallet.version != target["pallet_version"]
            or item is None
            or item.pallet_id != pallet.id
            or item.inventory_lot_id != lot.id
            or Decimal(item.quantity) != Decimal(target["physical_quantity"])
            or lot.pallet_item is None
            or lot.pallet_item.id != item.id
            or len(pallet.items) != 1
            or slot is None
            or slot.plan_id != plan.id
            or slot.location_id != target["location_id"]
        ):
            raise RepairRefused(f"FIN pallet/slot contract changed: {target['location_code']}")
        occupancies = _active_occupancies_for(db, pallet_id=pallet.id)
        by_location = _active_occupancies_for(db, location_id=target["location_id"])
        is_before = not occupancies and not by_location
        is_after = False
        if len(occupancies) == 1 and len(by_location) == 1:
            occupancy = occupancies[0]
            is_after = (
                by_location[0].id == occupancy.id
                and occupancy.pallet_id == pallet.id
                and occupancy.primary_location_id == target["location_id"]
                and occupancy.customer_id == lot.finished_detail.owner_customer_id
                and occupancy.product_id == lot.finished_detail.product_id
                and occupancy.footprint_kind == "single"
                and occupancy.capacity_quantity == target["physical_quantity"]
                and occupancy.version == 1
                and len(occupancy.slots) == 1
                and occupancy.slots[0].location_id == target["location_id"]
                and occupancy.slots[0].slot_sequence == 1
                and occupancy.slots[0].status == "active"
            )
        fin_before = fin_before and is_before
        fin_after = fin_after and is_after
        fin_contracts.append(
            {
                "target": deepcopy(target),
                "lot": _lot_snapshot(lot),
                "pallet_item": _pallet_item_snapshot(item),
                "layout_id": int(location.floor3_layout.id),
                "layout_version": int(location.floor3_layout.version),
                "slot_plan_id": int(slot.plan_id),
            }
        )

    a1_target = TARGETS["a1"]
    _validate_location(db, a1_target, storage_type="ground")
    a1_lot = _lot(db, a1_target)
    if (
        a1_lot.inventory_type != "finished"
        or int(a1_lot.warehouse_location_id) != a1_target["location_id"]
        or int(a1_lot.version) != a1_target["lot_version"]
        or _physical(a1_lot) != 0
    ):
        raise RepairRefused("A1-L05 zero-physical lot contract changed")
    a1_pallet = db.scalar(
        select(InventoryPallet)
        .where(InventoryPallet.id == a1_target["pallet_id"])
        .options(
            selectinload(InventoryPallet.items).selectinload(
                InventoryPalletItem.inventory_lot
            )
        )
    )
    a1_item = db.get(InventoryPalletItem, a1_target["pallet_item_id"])
    if (
        a1_pallet is None
        or a1_item is None
        or a1_item.pallet_id != a1_pallet.id
        or a1_item.inventory_lot_id != a1_lot.id
        or len(a1_pallet.items) != 1
        or any(_physical(item.inventory_lot) > 0 for item in a1_pallet.items if item.inventory_lot)
    ):
        raise RepairRefused("A1-L05 pallet/item contract changed")
    a1_clear = db.scalar(
        select(InventoryLocationMovement).where(
            InventoryLocationMovement.idempotency_key == A1_CLEAR_KEY
        )
    )
    a1_active_occupancy = _active_occupancies_for(db, pallet_id=a1_pallet.id)
    a1_before = (
        a1_pallet.is_current
        and a1_pallet.status == "active"
        and a1_pallet.location_id == a1_target["location_id"]
        and a1_pallet.version == a1_target["pallet_version_before"]
        and a1_clear is None
        and not a1_active_occupancy
    )
    a1_after = (
        not a1_pallet.is_current
        and a1_pallet.status == "closed"
        and a1_pallet.location_id is None
        and a1_pallet.version == a1_target["pallet_version_after"]
        and a1_clear is not None
        and a1_clear.pallet_id == a1_pallet.id
        and a1_clear.from_location_id == a1_target["location_id"]
        and a1_clear.to_location_id is None
        and a1_clear.movement_type == "clear"
        and not a1_active_occupancy
    )

    area_facts = {
        "C1": _area_facts(db, "C1", 3),
        "A1": _area_facts(db, "A1", 3),
        "FIN-001": _area_facts(db, "FIN-001", 1),
    }
    phase_before = c1_before and fin_before and a1_before
    phase_after = c1_after and fin_after and a1_after
    expected_counts = TARGETS["area_counts"]
    if phase_before:
        expected_phase = "before"
    elif phase_after:
        expected_phase = "after"
    else:
        expected_phase = "partial_or_drifted"
    for code, fact in area_facts.items():
        expected = expected_counts[code]
        if "capacity" in expected and fact["capacity"] != expected["capacity"]:
            raise RepairRefused(f"area capacity changed for {code}: {fact}")
        if (
            "positive_locations" in expected
            and fact["positive_locations"] != expected["positive_locations"]
        ):
            raise RepairRefused(f"positive location count changed for {code}: {fact}")
        if expected_phase in {"before", "after"} and fact["current_pallets"] != expected[
            f"current_{expected_phase}"
        ]:
            raise RepairRefused(f"current pallet count changed for {code}: {fact}")

    audit_rows = list(
        db.scalars(
            select(OperationLog).where(
                OperationLog.batch_id == BATCH_ID,
                OperationLog.action_code == ACTION_CODE,
            )
        )
    )
    if phase_before and audit_rows:
        raise RepairRefused("repair audit exists while target projection is still incomplete")
    if phase_after and len(audit_rows) != 1:
        raise RepairRefused("final projection does not have exactly one repair audit event")
    if not phase_before and not phase_after:
        raise RepairRefused("authorized residues are partially repaired or drifted")

    contract = {
        "contract_version": "p1-102-r3-v1",
        "alembic_revision": revision,
        "runtime": runtime,
        "targets": deepcopy(TARGETS),
        "stable_keys": {
            "batch_id": BATCH_ID,
            "c1_pallet_code": C1_PALLET_CODE,
            "c1_movement": C1_MOVEMENT_KEY,
            "a1_clear": A1_CLEAR_KEY,
        },
        "protected_lots": {
            "c1": _lot_snapshot(c1_lot),
            "fin": fin_contracts,
            "a1": _lot_snapshot(a1_lot),
            "a1_item": _pallet_item_snapshot(a1_item),
        },
        "fin_plan": {
            "plan_id": int(plan.id),
            "plan_version": int(plan.version),
            "policy_id": int(policy.id),
            "policy_version": int(policy.version),
            "published_revision": plan.published_map_revision,
        },
        "area_contract": {
            code: {
                "area_id": fact["area_id"],
                "capacity": fact["capacity"],
                "positive_locations": fact["positive_locations"],
            }
            for code, fact in area_facts.items()
        },
        "allowed_deltas": ALLOWED_DELTAS,
    }
    plan_sha = canonical_sha256(contract)
    return {
        "status": "ready" if phase_before else "already_applied",
        "plan_sha256": plan_sha,
        "contract": contract,
        "observed": {
            "phase": expected_phase,
            "area_facts": area_facts,
            "audit_count": len(audit_rows),
        },
    }


def _readonly_engine(database: Path):
    # ``Path.resolve()`` raises WinError 1005 for the factory NAS mapped drive
    # even though normal file and SQLite access are healthy. ``absolute()``
    # preserves the exact mapped path without asking Windows for a final path.
    uri = f"file:{database.absolute().as_posix()}?mode=ro"

    def creator() -> sqlite3.Connection:
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=30,
            check_same_thread=False,
        )
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA query_only=ON")
        return connection

    return create_engine("sqlite+pysqlite://", creator=creator, future=True)


def inspect_plan(database: Path, runtime_map: Path) -> dict[str, Any]:
    engine = _readonly_engine(database)
    try:
        with Session(engine) as db:
            if int(db.execute(text("PRAGMA query_only")).scalar_one()) != 1:
                raise RepairRefused("SQLite query_only is not enabled")
            total_changes_before = int(
                db.execute(text("SELECT total_changes()")) .scalar_one()
            )
            plan = _build_plan(db, runtime_map)
            total_changes_after = int(
                db.execute(text("SELECT total_changes()")) .scalar_one()
            )
            if total_changes_before != total_changes_after:
                raise RepairRefused("dry-run unexpectedly changed SQLite state")
            return {
                **plan,
                "query_only": True,
                "total_changes_before": total_changes_before,
                "total_changes_after": total_changes_after,
            }
    finally:
        engine.dispose()


def database_checks(path: Path) -> dict[str, Any]:
    uri = f"file:{path.absolute().as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as connection:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA query_only=ON")
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        revision_rows = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchall()
        return {
            "sha256": sha256_file(path),
            "size": int(path.stat().st_size),
            "integrity_check": integrity,
            "foreign_key_violations": len(foreign_keys),
            "alembic_revisions": [str(row[0]) for row in revision_rows],
        }


def _normalized_path(path: Path) -> str:
    return str(path.absolute()).replace("/", "\\").casefold()


def _same_file(left: Path, right: Path) -> bool:
    if _normalized_path(left) == _normalized_path(right):
        return True
    try:
        return left.exists() and right.exists() and left.samefile(right)
    except OSError:
        return False


def _require_database_role(database: Path, role: str) -> None:
    exact_formal_path = _normalized_path(database) == _normalized_path(FORMAL_DATABASE)
    same_formal_file = _same_file(database, FORMAL_DATABASE)
    if role == "formal" and not (exact_formal_path and same_formal_file):
        raise RepairRefused("formal role requires the exact formal database path")
    if role == "isolated-rehearsal" and same_formal_file:
        raise RepairRefused("isolated-rehearsal role cannot target the formal database")


def _actor(db: Session, operator_user_id: int) -> User:
    actor = db.get(User, operator_user_id)
    if actor is None or not actor.is_active or actor.role not in {"admin", "boss"}:
        raise RepairRefused("operator must be an active admin or boss")
    return actor


def _insert_c1_projection(db: Session, actor: User, lot: InventoryLot) -> InventoryPallet:
    detail = lot.finished_detail
    assert detail is not None and detail.owner_customer_id is not None
    now = beijing_now_naive()
    pallet = InventoryPallet(
        pallet_code=C1_PALLET_CODE,
        location_id=TARGETS["c1"]["location_id"],
        location_occupancy_key="PRIMARY",
        status="active",
        is_current=True,
        needs_relocation=False,
        version=1,
        remarks="P1-102 正式空间投影残留治理：C1-R13 原位补栈板身份",
        created_by=actor.id,
        updated_by=actor.id,
    )
    db.add(pallet)
    db.flush()
    db.add(
        InventoryPalletItem(
            pallet_id=pallet.id,
            inventory_lot_id=lot.id,
            customer_id=detail.owner_customer_id,
            product_id=detail.product_id,
            inventory_code=detail.inventory_code_snapshot,
            customer_name_snapshot=detail.owner_customer_name_snapshot,
            product_name=detail.product_name_snapshot,
            item_type="finished",
            quantity=TARGETS["c1"]["physical_quantity"],
            unit=lot.unit,
            match_status="matched",
            remarks="原库存原库位空间投影补齐，不改变库存事实",
            created_by=actor.id,
        )
    )
    db.add(
        InventoryLocationMovement(
            pallet_id=pallet.id,
            from_location_id=None,
            to_location_id=TARGETS["c1"]["location_id"],
            movement_type="create",
            operator_id=actor.id,
            moved_at=now,
            idempotency_key=C1_MOVEMENT_KEY,
            confirmed_at=now,
            pallet_version_before=None,
            pallet_version_after=1,
            remarks="P1-102 正式空间投影残留治理：原位补栈板身份",
        )
    )
    db.flush()
    return pallet


def _insert_fin_occupancy(
    db: Session, actor: User, target: dict[str, Any], lot: InventoryLot
) -> WarehouseGroundOccupancy:
    detail = lot.finished_detail
    assert detail is not None and detail.owner_customer_id is not None
    occupancy = WarehouseGroundOccupancy(
        pallet_id=target["pallet_id"],
        primary_location_id=target["location_id"],
        customer_id=detail.owner_customer_id,
        product_id=detail.product_id,
        footprint_kind="single",
        capacity_quantity=target["physical_quantity"],
        status="active",
        version=1,
        created_by=actor.id,
    )
    db.add(occupancy)
    db.flush()
    db.add(
        WarehouseGroundOccupancySlot(
            occupancy_id=occupancy.id,
            location_id=target["location_id"],
            slot_sequence=1,
            status="active",
        )
    )
    db.flush()
    return occupancy


def _perform_repair(
    db: Session,
    *,
    actor: User,
    plan: dict[str, Any],
    database_role: str,
    backup_details: dict[str, Any] | None,
) -> None:
    c1_lot = _lot(db, TARGETS["c1"])
    _insert_c1_projection(db, actor, c1_lot)
    occupancy_ids: list[int] = []
    for target in TARGETS["fin"]:
        occupancy = _insert_fin_occupancy(db, actor, target, _lot(db, target))
        occupancy_ids.append(int(occupancy.id))
    clear_pallet(
        db,
        pallet_id=TARGETS["a1"]["pallet_id"],
        expected_version=TARGETS["a1"]["pallet_version_before"],
        remarks="P1-102 正式空间投影残留治理：清理零实物量空栈板投影",
        operator_id=actor.id,
        idempotency_key=A1_CLEAR_KEY,
    )
    release_ground_occupancy_for_pallet(
        db,
        pallet_id=TARGETS["a1"]["pallet_id"],
        operator_id=actor.id,
    )
    append_audit_event(
        db,
        event_category="system",
        result="success",
        source="script",
        module_code="warehouse",
        action_code=ACTION_CODE,
        legacy_action="PROJECTION_RESIDUE_REPAIR",
        resource="WarehouseProjection",
        actor=actor,
        entity_type="warehouse_projection_repair",
        object_ref=BATCH_ID,
        request_id=plan["plan_sha256"],
        batch_id=BATCH_ID,
        description="P1-102 正式成品空间投影三类残留治理",
        details={
            "source": SOURCE,
            "database_role": database_role,
            "plan_sha256": plan["plan_sha256"],
            "location_codes": [
                TARGETS["c1"]["location_code"],
                *(target["location_code"] for target in TARGETS["fin"]),
                TARGETS["a1"]["location_code"],
            ],
            "lot_ids": [
                TARGETS["c1"]["lot_id"],
                *(target["lot_id"] for target in TARGETS["fin"]),
                TARGETS["a1"]["lot_id"],
            ],
            "occupancy_ids": occupancy_ids,
            "allowed_deltas": ALLOWED_DELTAS,
            "backup": backup_details,
            "inventory_fact_changes": 0,
        },
    )
    db.flush()


def _assert_deltas(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    deltas = {
        name: int(after.get(name, 0) - before.get(name, 0))
        for name in sorted(set(before) | set(after))
    }
    for table in PROTECTED_COUNT_TABLES:
        if deltas.get(table, 0) != 0:
            raise RepairRefused(f"protected table count changed for {table}")
    for table, expected in ALLOWED_DELTAS.items():
        if deltas.get(table, 0) != expected:
            raise RepairRefused(
                f"unexpected row delta for {table}: {deltas.get(table, 0)} != {expected}"
            )
    return deltas


def _load_snapshot_evidence(
    path: Path,
    expected_plan_sha256: str,
    runtime_map: Path,
) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    backup = payload.get("backup") or {}
    backup_path_raw = backup.get("path")
    if (
        payload.get("status") != "snapshot_verified"
        or payload.get("plan_sha256") != expected_plan_sha256
        or payload.get("script_sha256") != script_sha256()
        or not backup_path_raw
        or not backup.get("sha256")
        or backup.get("size") is None
    ):
        raise RepairRefused("snapshot evidence does not match this script and plan")

    backup_path = Path(str(backup_path_raw))
    if not backup_path.is_file():
        raise RepairRefused("verified snapshot backup is missing")
    current_checks = database_checks(backup_path)
    current_plan = inspect_plan(backup_path, runtime_map)
    recorded_checks = backup.get("checks") or {}
    if (
        str(current_checks.get("integrity_check") or "").casefold() != "ok"
        or current_checks.get("foreign_key_violations") != 0
        or current_checks.get("alembic_revisions") != [EXPECTED_ALEMBIC_REVISION]
        or str(current_checks.get("sha256") or "").casefold()
        != str(backup.get("sha256") or "").casefold()
        or int(current_checks.get("size") or -1) != int(backup.get("size") or -2)
        or recorded_checks.get("sha256") != current_checks.get("sha256")
        or recorded_checks.get("size") != current_checks.get("size")
        or str(recorded_checks.get("integrity_check") or "").casefold() != "ok"
        or recorded_checks.get("foreign_key_violations") != 0
        or recorded_checks.get("alembic_revisions") != [EXPECTED_ALEMBIC_REVISION]
        or current_plan.get("status") != "ready"
        or current_plan.get("plan_sha256") != expected_plan_sha256
    ):
        raise RepairRefused("verified snapshot backup changed after evidence creation")
    return payload


def _load_rehearsal_evidence(
    path: Path,
    snapshot_evidence_path: Path,
    expected_plan_sha256: str,
    runtime_map: Path,
) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    snapshot = _load_snapshot_evidence(
        snapshot_evidence_path,
        expected_plan_sha256,
        runtime_map,
    )
    if (
        payload.get("database_role") != "isolated-rehearsal"
        or payload.get("status") != "verified"
        or payload.get("plan_sha256") != expected_plan_sha256
        or payload.get("script_sha256") != script_sha256()
        or payload.get("replay_verified") is not True
        or payload.get("projection_complete") is not True
    ):
        raise RepairRefused("rehearsal evidence does not match this script and plan")
    backup = snapshot.get("backup") or {}
    if (
        payload.get("source_snapshot_sha256") != backup.get("sha256")
    ):
        raise RepairRefused("rehearsal evidence is not bound to the verified snapshot")
    return payload


def _assert_verified_backup(
    *,
    backup: Any,
    checks: dict[str, Any],
    plan: dict[str, Any],
    expected_plan_sha256: str,
) -> None:
    if (
        str(backup.integrity_check).casefold() != "ok"
        or str(checks.get("integrity_check") or "").casefold() != "ok"
        or checks.get("foreign_key_violations") != 0
        or checks.get("alembic_revisions") != [EXPECTED_ALEMBIC_REVISION]
        or str(backup.sha256).casefold()
        != str(checks.get("sha256") or "").casefold()
        or int(backup.size) != int(checks.get("size") or -1)
        or plan.get("status") != "ready"
        or plan.get("plan_sha256") != expected_plan_sha256
    ):
        raise RepairRefused("verified backup does not match the locked repair plan")


def create_snapshot(
    *,
    database: Path,
    runtime_map: Path,
    backup_dir: Path,
    expected_plan_sha256: str,
) -> dict[str, Any]:
    _require_database_role(database, "formal")
    engine = create_sqlite_engine(database, busy_timeout_ms=30_000)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as db:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            plan = _build_plan(db, runtime_map)
            if plan["status"] != "ready":
                raise RepairRefused(f"snapshot preflight is not ready: {plan['status']}")
            if plan["plan_sha256"] != expected_plan_sha256:
                raise RepairRefused("plan SHA-256 changed before snapshot")
            backup = backup_to_nas(
                source_path=database,
                backup_dir=backup_dir,
                filename_suffix=BACKUP_SUFFIX,
                keep_regular=1_000_000,
            )
            backup_checks = database_checks(backup.path)
            backup_plan = inspect_plan(backup.path, runtime_map)
            _assert_verified_backup(
                backup=backup,
                checks=backup_checks,
                plan=backup_plan,
                expected_plan_sha256=expected_plan_sha256,
            )
            db.rollback()
            return {
                "status": "snapshot_verified",
                "plan_sha256": expected_plan_sha256,
                "script_sha256": script_sha256(),
                "backup": {
                    **asdict(backup),
                    "path": str(backup.path),
                    "checks": backup_checks,
                },
            }
    finally:
        engine.dispose()


def apply_repair(
    *,
    database: Path,
    runtime_map: Path,
    database_role: str,
    operator_user_id: int,
    expected_plan_sha256: str,
    authorization: str,
    backup_dir: Path | None = None,
    rehearsal_evidence: Path | None = None,
    snapshot_evidence: Path | None = None,
    source_snapshot_sha256: str | None = None,
) -> dict[str, Any]:
    _require_database_role(database, database_role)
    if authorization != AUTHORIZATION:
        raise RepairRefused("formal projection repair authorization phrase is invalid")
    snapshot_payload: dict[str, Any] | None = None
    if database_role == "formal":
        if (
            backup_dir is None
            or rehearsal_evidence is None
            or snapshot_evidence is None
        ):
            raise RepairRefused(
                "formal apply requires backup-dir, snapshot evidence, and rehearsal evidence"
            )
        _load_rehearsal_evidence(
            rehearsal_evidence,
            snapshot_evidence,
            expected_plan_sha256,
            runtime_map,
        )
    elif database_role == "isolated-rehearsal":
        if snapshot_evidence is None or not source_snapshot_sha256:
            raise RepairRefused(
                "isolated rehearsal requires snapshot evidence and source snapshot SHA-256"
            )
        snapshot_payload = _load_snapshot_evidence(
            snapshot_evidence,
            expected_plan_sha256,
            runtime_map,
        )
        snapshot_backup = snapshot_payload.get("backup") or {}
        snapshot_backup_path = Path(str(snapshot_backup.get("path") or ""))
        if _same_file(database, snapshot_backup_path):
            raise RepairRefused(
                "isolated rehearsal must use an independent copy of the verified snapshot"
            )
        if (
            str(source_snapshot_sha256).casefold()
            != str(snapshot_backup.get("sha256") or "").casefold()
        ):
            raise RepairRefused(
                "isolated rehearsal source SHA-256 does not match snapshot evidence"
            )

    database_sha256_at_entry = sha256_file(database)

    engine = create_sqlite_engine(database, busy_timeout_ms=30_000)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    backup_payload: dict[str, Any] | None = None
    changed = False
    deltas: dict[str, int] = {}
    before_inventory: dict[str, Any] | None = None
    try:
        with factory() as db:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            actor = _actor(db, operator_user_id)
            plan = _build_plan(db, runtime_map)
            if plan["plan_sha256"] != expected_plan_sha256:
                raise RepairRefused("plan SHA-256 changed before apply")
            if plan["status"] == "already_applied":
                db.rollback()
                return {
                    "status": "already_applied",
                    "changed": False,
                    "database_role": database_role,
                    "plan_sha256": expected_plan_sha256,
                    "script_sha256": script_sha256(),
                    "projection_complete": True,
                    "replay_verified": True,
                }
            if plan["status"] != "ready":
                raise RepairRefused(f"repair preflight is not ready: {plan['status']}")
            if database_role == "isolated-rehearsal":
                if not source_snapshot_sha256:
                    raise RepairRefused(
                        "isolated rehearsal requires the verified source snapshot SHA-256"
                    )
                if (
                    database_sha256_at_entry.casefold()
                    != source_snapshot_sha256.casefold()
                ):
                    raise RepairRefused(
                        "isolated rehearsal database is not the verified snapshot copy"
                    )

            before_counts = _table_counts(db)
            before_inventory = _inventory_signature(db)
            if database_role == "formal":
                assert backup_dir is not None
                backup = backup_to_nas(
                    source_path=database,
                    backup_dir=backup_dir,
                    filename_suffix=BACKUP_SUFFIX,
                    keep_regular=1_000_000,
                )
                backup_checks = database_checks(backup.path)
                backup_plan = inspect_plan(backup.path, runtime_map)
                _assert_verified_backup(
                    backup=backup,
                    checks=backup_checks,
                    plan=backup_plan,
                    expected_plan_sha256=expected_plan_sha256,
                )
                backup_payload = {
                    **asdict(backup),
                    "path": str(backup.path),
                    "checks": backup_checks,
                }

            _perform_repair(
                db,
                actor=actor,
                plan=plan,
                database_role=database_role,
                backup_details=(
                    {
                        "path": backup_payload["path"],
                        "sha256": backup_payload["sha256"],
                        "size": backup_payload["size"],
                    }
                    if backup_payload is not None
                    else None
                ),
            )
            db.flush()
            db.expire_all()
            after_plan = _build_plan(db, runtime_map)
            after_counts = _table_counts(db)
            after_inventory = _inventory_signature(db)
            deltas = _assert_deltas(before_counts, after_counts)
            if before_inventory != after_inventory:
                raise RepairRefused("protected inventory facts changed inside transaction")
            if (
                after_plan["status"] != "already_applied"
                or after_plan["plan_sha256"] != expected_plan_sha256
            ):
                raise RepairRefused("transaction postcondition is incomplete")
            db.commit()
            changed = True
    except Exception:
        raise
    finally:
        engine.dispose()

    checks_after = database_checks(database)
    postflight = inspect_plan(database, runtime_map)
    if (
        checks_after["integrity_check"].casefold() != "ok"
        or checks_after["foreign_key_violations"] != 0
        or postflight["status"] != "already_applied"
        or postflight["plan_sha256"] != expected_plan_sha256
    ):
        raise RepairRefused("committed database failed postflight verification")
    replay_verified = False
    if database_role == "isolated-rehearsal":
        replay_sha_before = sha256_file(database)
        replay_result = apply_repair(
            database=database,
            runtime_map=runtime_map,
            database_role=database_role,
            operator_user_id=operator_user_id,
            expected_plan_sha256=expected_plan_sha256,
            authorization=authorization,
            snapshot_evidence=snapshot_evidence,
            source_snapshot_sha256=source_snapshot_sha256,
        )
        replay_sha_after = sha256_file(database)
        if (
            replay_result.get("status") != "already_applied"
            or replay_result.get("changed") is not False
            or replay_sha_before != replay_sha_after
        ):
            raise RepairRefused("isolated rehearsal idempotent replay verification failed")
        replay_verified = True

    result = {
        "status": "verified",
        "changed": changed,
        "database_role": database_role,
        "plan_sha256": expected_plan_sha256,
        "script_sha256": script_sha256(),
        "row_deltas": deltas,
        "protected_inventory_signature": before_inventory,
        "database_checks_after": checks_after,
        "backup": backup_payload,
        "source_snapshot_sha256": (
            source_snapshot_sha256 if database_role == "isolated-rehearsal" else None
        ),
        "projection_complete": True,
        "replay_verified": replay_verified,
    }
    return result


def _write_report(
    path: Path | None,
    payload: dict[str, Any],
    *,
    forbidden_paths: Sequence[Path] = (),
) -> None:
    if path is None:
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    for forbidden in forbidden_paths:
        if _same_file(path, forbidden) or _same_file(temporary, forbidden):
            raise RepairRefused(
                f"report path conflicts with protected input: {forbidden}"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--runtime-map", type=Path, required=True)
    parser.add_argument(
        "--database-role",
        choices=("formal", "isolated-rehearsal"),
        default="isolated-rehearsal",
    )
    parser.add_argument("--operator-user-id", type=int)
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--authorization")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--rehearsal-evidence", type=Path)
    parser.add_argument("--snapshot-evidence", type=Path)
    parser.add_argument("--source-snapshot-sha256")
    parser.add_argument("--snapshot-only", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    forbidden_report_paths = [args.database, args.runtime_map]
    if args.rehearsal_evidence is not None:
        forbidden_report_paths.append(args.rehearsal_evidence)
    if args.snapshot_evidence is not None:
        forbidden_report_paths.append(args.snapshot_evidence)
    if args.report is not None:
        report_temporary = args.report.with_suffix(args.report.suffix + ".tmp")
        for forbidden in forbidden_report_paths:
            if _same_file(args.report, forbidden) or _same_file(
                report_temporary, forbidden
            ):
                raise RepairRefused(
                    f"report path conflicts with protected input: {forbidden}"
                )
    if args.apply and args.snapshot_only:
        raise RepairRefused("--apply and --snapshot-only are mutually exclusive")
    if args.snapshot_only:
        if args.database_role != "formal":
            raise RepairRefused("snapshot-only requires formal database role")
        if not args.expected_plan_sha256 or args.backup_dir is None:
            raise RepairRefused("snapshot-only requires expected plan SHA and backup dir")
        payload = create_snapshot(
            database=args.database,
            runtime_map=args.runtime_map,
            backup_dir=args.backup_dir,
            expected_plan_sha256=args.expected_plan_sha256,
        )
    elif args.apply:
        if args.operator_user_id is None or not args.expected_plan_sha256:
            raise RepairRefused("apply requires operator user id and expected plan SHA")
        payload = apply_repair(
            database=args.database,
            runtime_map=args.runtime_map,
            database_role=args.database_role,
            operator_user_id=args.operator_user_id,
            expected_plan_sha256=args.expected_plan_sha256,
            authorization=args.authorization or "",
            backup_dir=args.backup_dir,
            rehearsal_evidence=args.rehearsal_evidence,
            snapshot_evidence=args.snapshot_evidence,
            source_snapshot_sha256=args.source_snapshot_sha256,
        )
    else:
        payload = {
            **inspect_plan(args.database, args.runtime_map),
            "database_role": args.database_role,
            "script_sha256": script_sha256(),
            "query_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
    _write_report(
        args.report,
        payload,
        forbidden_paths=forbidden_report_paths,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
