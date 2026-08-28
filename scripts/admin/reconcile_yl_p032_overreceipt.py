"""Reconcile the exact YL Z.001.000132 duplicate receipt and reserve conversion.

Dry-run is read-only.  Apply is deliberately limited to order item 10147 and
requires the P0-32 schema, an exact database SHA-256, a verified backup, a
stopped service, and an active admin/boss audit actor.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.database import backup_to_nas, create_sqlite_engine  # noqa: E402
from app.models.customer import Customer  # noqa: E402
from app.models.incoming_receipt import (  # noqa: E402
    IncomingReceipt,
    IncomingReceiptItem,
)
from app.models.order import OrderItem  # noqa: E402
from app.models.production import ProductionCompletion  # noqa: E402
from app.models.purchase_receipt import (  # noqa: E402
    IncomingReceiptPurposeAllocation,
    IncomingReceiptPurposeReversal,
    ProductionCompletionReserveConversion,
)
from app.models.supplier_requisition_order import (  # noqa: E402
    SupplierRequisitionOrderItem,
)
from app.models.user import User  # noqa: E402
from app.models.warehouse_inventory import (  # noqa: E402
    InventoryLot,
    InventoryMovement,
)
from app.services.audit_log import append_audit_event  # noqa: E402
from app.services.incoming_receipts import revert_receipt_item  # noqa: E402
from app.services.receipt_purpose_distribution import (  # noqa: E402
    consume_receipt_reserve_for_completion_adjustment,
)


TARGET_REVISION = "gr53v8x9z42"
CONFIRMATION = "APPLY_YL_P032_OVERRECEIPT_RECONCILIATION"
SOURCE = "scripts.admin.reconcile_yl_p032_overreceipt"
ACTION_CODE = "incoming.reserve_to_finished.reconcile"

CUSTOMER_ID = 136
CUSTOMER_CODE = "YL"
ORDER_ITEM_ID = 10147
PRODUCT_CODE = "Z.001.000132"
SUPPLIER_ITEM_ID = 387
PRIMARY_RECEIPT_ID = 292
DUPLICATE_RECEIPT_ID = 293
PRIMARY_ALLOCATION_ID = 35
DUPLICATE_ALLOCATION_ID = 36
COMPLETION_ID = 244
FINISHED_LOT_ID = 391
PRIMARY_RESERVE_LOT_ID = 392
DUPLICATE_RESERVE_LOT_ID = 393
FINISHED_ADJUST_MOVEMENT_ID = 1122
CONVERSION_KEY = "p0-32-yl-10147:reserve-convert:35"
REVERSAL_KEY = "p0-32-yl-10147:reverse-duplicate-receipt:293"

COUNT_TABLES = (
    "incoming_receipts",
    "incoming_receipt_items",
    "incoming_receipt_purpose_allocations",
    "incoming_receipt_purpose_reversals",
    "production_completion_reserve_conversions",
    "production_completions",
    "inventory_lots",
    "inventory_movements",
    "operation_logs",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def database_checks(path: Path) -> dict[str, Any]:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as connection:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA query_only=ON")
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        counts = {
            table: int(
                connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
            for table in COUNT_TABLES
            if table in tables
        }
        revision = str(
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        )
    return {
        "revision": revision,
        "integrity_check": integrity,
        "foreign_key_violations": len(foreign_keys),
        "counts": counts,
    }


def _readonly_engine(database: Path):
    uri = f"file:{database.resolve().as_posix()}?mode=ro"

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


def _row_snapshot(db: Session) -> dict[str, Any]:
    customer = db.get(Customer, CUSTOMER_ID)
    item = db.get(OrderItem, ORDER_ITEM_ID)
    source = db.get(SupplierRequisitionOrderItem, SUPPLIER_ITEM_ID)
    primary_receipt = db.get(IncomingReceiptItem, PRIMARY_RECEIPT_ID)
    duplicate_receipt = db.get(IncomingReceiptItem, DUPLICATE_RECEIPT_ID)
    primary_allocation = db.get(
        IncomingReceiptPurposeAllocation, PRIMARY_ALLOCATION_ID
    )
    duplicate_allocation = db.get(
        IncomingReceiptPurposeAllocation, DUPLICATE_ALLOCATION_ID
    )
    completion = db.get(ProductionCompletion, COMPLETION_ID)
    finished_lot = db.get(InventoryLot, FINISHED_LOT_ID)
    primary_reserve = db.get(InventoryLot, PRIMARY_RESERVE_LOT_ID)
    duplicate_reserve = db.get(InventoryLot, DUPLICATE_RESERVE_LOT_ID)
    adjust_movement = db.get(InventoryMovement, FINISHED_ADJUST_MOVEMENT_ID)
    duplicate_purpose_reversal = db.scalar(
        select(IncomingReceiptPurposeReversal).where(
            IncomingReceiptPurposeReversal.incoming_receipt_purpose_allocation_id
            == DUPLICATE_ALLOCATION_ID
        )
    )
    conversion = db.scalar(
        select(ProductionCompletionReserveConversion).where(
            ProductionCompletionReserveConversion.idempotency_key == CONVERSION_KEY
        )
    )
    parent_receipt = (
        db.get(IncomingReceipt, duplicate_receipt.receipt_id)
        if duplicate_receipt is not None
        else None
    )
    return {
        "revision": str(db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()),
        "customer": (
            {
                "id": int(customer.id),
                "code": customer.customer_code,
            }
            if customer is not None
            else None
        ),
        "order_item": (
            {
                "id": int(item.id),
                "quantity": int(item.quantity or 0),
                "delivered_quantity": int(item.delivered_quantity or 0),
                "product_code": item.snapshot_product_code,
            }
            if item is not None
            else None
        ),
        "source": (
            {
                "id": int(source.id),
                "order_item_id": int(source.order_item_id or 0),
                "quantity": int(source.quantity or 0),
                "requisition_qty": int(source.requisition_qty or 0),
                "purpose_contract_status": source.purpose_contract_status,
            }
            if source is not None
            else None
        ),
        "primary_receipt": (
            {
                "id": int(primary_receipt.id),
                "status": primary_receipt.status,
                "received_quantity": int(primary_receipt.received_quantity or 0),
                "cumulative": int(primary_receipt.cumulative_received_quantity or 0),
            }
            if primary_receipt is not None
            else None
        ),
        "duplicate_receipt": (
            {
                "id": int(duplicate_receipt.id),
                "status": duplicate_receipt.status,
                "received_quantity": int(duplicate_receipt.received_quantity or 0),
                "cumulative": int(duplicate_receipt.cumulative_received_quantity or 0),
                "parent_status": parent_receipt.status if parent_receipt else None,
            }
            if duplicate_receipt is not None
            else None
        ),
        "primary_allocation": (
            {
                "id": int(primary_allocation.id),
                "disposition": primary_allocation.surplus_disposition,
                "order_sheets": int(primary_allocation.receipt_order_purpose_sheet_qty),
                "reserve_sheets": int(primary_allocation.receipt_reserve_purpose_sheet_qty),
                "completion_id": primary_allocation.production_completion_id,
                "finished_lot_id": primary_allocation.finished_inventory_lot_id,
                "semi_lot_id": primary_allocation.semi_finished_inventory_lot_id,
            }
            if primary_allocation is not None
            else None
        ),
        "duplicate_allocation": (
            {
                "id": int(duplicate_allocation.id),
                "disposition": duplicate_allocation.surplus_disposition,
                "order_sheets": int(duplicate_allocation.receipt_order_purpose_sheet_qty),
                "reserve_sheets": int(duplicate_allocation.receipt_reserve_purpose_sheet_qty),
                "semi_lot_id": duplicate_allocation.semi_finished_inventory_lot_id,
            }
            if duplicate_allocation is not None
            else None
        ),
        "completion": (
            {
                "id": int(completion.id),
                "status": completion.status,
                "origin": completion.origin,
                "actual": int(completion.actual_output_quantity or 0),
                "reserved": int(completion.order_reserved_quantity or 0),
                "surplus": int(completion.surplus_finished_quantity or 0),
                "lot_id": completion.inventory_lot_id,
            }
            if completion is not None
            else None
        ),
        "finished_lot": (
            {
                "id": int(finished_lot.id),
                "status": finished_lot.status,
                "available": int(finished_lot.quantity_available or 0),
                "reserved": int(finished_lot.quantity_reserved or 0),
                "consumed": int(finished_lot.quantity_consumed or 0),
            }
            if finished_lot is not None
            else None
        ),
        "primary_reserve": (
            {
                "id": int(primary_reserve.id),
                "status": primary_reserve.status,
                "available": int(primary_reserve.quantity_available or 0),
                "reserved": int(primary_reserve.quantity_reserved or 0),
                "consumed": int(primary_reserve.quantity_consumed or 0),
            }
            if primary_reserve is not None
            else None
        ),
        "duplicate_reserve": (
            {
                "id": int(duplicate_reserve.id),
                "status": duplicate_reserve.status,
                "available": int(duplicate_reserve.quantity_available or 0),
                "reserved": int(duplicate_reserve.quantity_reserved or 0),
                "consumed": int(duplicate_reserve.quantity_consumed or 0),
            }
            if duplicate_reserve is not None
            else None
        ),
        "finished_adjust": (
            {
                "id": int(adjust_movement.id),
                "lot_id": int(adjust_movement.inventory_lot_id),
                "type": adjust_movement.movement_type,
                "quantity": int(adjust_movement.quantity or 0),
                "before_available": int(adjust_movement.before_available or 0),
                "after_available": int(adjust_movement.after_available or 0),
            }
            if adjust_movement is not None
            else None
        ),
        "duplicate_purpose_reversal": (
            int(duplicate_purpose_reversal.id)
            if duplicate_purpose_reversal is not None
            else None
        ),
        "conversion": (
            {
                "id": int(conversion.id),
                "completion_id": int(conversion.production_completion_id),
                "allocation_id": int(conversion.receipt_purpose_allocation_id),
                "semi_lot_id": int(conversion.semi_finished_inventory_lot_id),
                "finished_lot_id": int(conversion.finished_inventory_lot_id),
                "finished_adjust_movement_id": int(conversion.finished_adjust_movement_id),
                "converted_sheets": int(conversion.converted_sheet_quantity),
                "finished_delta": int(conversion.finished_quantity_delta),
                "supported_before": int(conversion.supported_finished_quantity_before),
                "supported_after": int(conversion.supported_finished_quantity_after),
            }
            if conversion is not None
            else None
        ),
    }


def build_plan(db: Session) -> dict[str, Any]:
    snapshot = _row_snapshot(db)
    common_expected = {
        "revision": TARGET_REVISION,
        "customer": {"id": CUSTOMER_ID, "code": CUSTOMER_CODE},
        "order_item": {
            "id": ORDER_ITEM_ID,
            "quantity": 600,
            "delivered_quantity": 0,
            "product_code": PRODUCT_CODE,
        },
        "source": {
            "id": SUPPLIER_ITEM_ID,
            "order_item_id": ORDER_ITEM_ID,
            "quantity": 600,
            "requisition_qty": 602,
            "purpose_contract_status": "frozen",
        },
        "primary_receipt": {
            "id": PRIMARY_RECEIPT_ID,
            "status": "posted",
            "received_quantity": 602,
            "cumulative": 602,
        },
        "primary_allocation": {
            "id": PRIMARY_ALLOCATION_ID,
            "disposition": "semi_finished_reserve",
            "order_sheets": 600,
            "reserve_sheets": 2,
            "completion_id": COMPLETION_ID,
            "finished_lot_id": FINISHED_LOT_ID,
            "semi_lot_id": PRIMARY_RESERVE_LOT_ID,
        },
        "duplicate_allocation": {
            "id": DUPLICATE_ALLOCATION_ID,
            "disposition": "semi_finished_reserve",
            "order_sheets": 0,
            "reserve_sheets": 602,
            "semi_lot_id": DUPLICATE_RESERVE_LOT_ID,
        },
        "completion": {
            "id": COMPLETION_ID,
            "status": "posted",
            "origin": "receipt_auto",
            "actual": 602,
            "reserved": 600,
            "surplus": 2,
            "lot_id": FINISHED_LOT_ID,
        },
        "finished_lot": {
            "id": FINISHED_LOT_ID,
            "status": "active",
            "available": 2,
            "reserved": 600,
            "consumed": 0,
        },
        "finished_adjust": {
            "id": FINISHED_ADJUST_MOVEMENT_ID,
            "lot_id": FINISHED_LOT_ID,
            "type": "adjust",
            "quantity": 2,
            "before_available": 0,
            "after_available": 2,
        },
    }
    ready_expected = {
        **common_expected,
        "duplicate_receipt": {
            "id": DUPLICATE_RECEIPT_ID,
            "status": "posted",
            "received_quantity": 602,
            "cumulative": 1204,
            "parent_status": "posted",
        },
        "primary_reserve": {
            "id": PRIMARY_RESERVE_LOT_ID,
            "status": "active",
            "available": 2,
            "reserved": 0,
            "consumed": 0,
        },
        "duplicate_reserve": {
            "id": DUPLICATE_RESERVE_LOT_ID,
            "status": "active",
            "available": 602,
            "reserved": 0,
            "consumed": 0,
        },
        "duplicate_purpose_reversal": None,
        "conversion": None,
    }
    applied_expected = {
        **common_expected,
        "duplicate_receipt": {
            "id": DUPLICATE_RECEIPT_ID,
            "status": "reversed",
            "received_quantity": 602,
            "cumulative": 1204,
            "parent_status": "reversed",
        },
        "primary_reserve": {
            "id": PRIMARY_RESERVE_LOT_ID,
            "status": "active",
            "available": 0,
            "reserved": 0,
            "consumed": 2,
        },
        "duplicate_reserve": {
            "id": DUPLICATE_RESERVE_LOT_ID,
            "status": "closed",
            "available": 0,
            "reserved": 0,
            "consumed": 0,
        },
        "conversion": {
            "id": snapshot["conversion"]["id"] if snapshot["conversion"] else None,
            "completion_id": COMPLETION_ID,
            "allocation_id": PRIMARY_ALLOCATION_ID,
            "semi_lot_id": PRIMARY_RESERVE_LOT_ID,
            "finished_lot_id": FINISHED_LOT_ID,
            "finished_adjust_movement_id": FINISHED_ADJUST_MOVEMENT_ID,
            "converted_sheets": 2,
            "finished_delta": 2,
            "supported_before": 600,
            "supported_after": 602,
        },
    }
    if snapshot == ready_expected:
        status = "ready"
        mismatches: list[dict[str, Any]] = []
    elif (
        snapshot["duplicate_purpose_reversal"] is not None
        and snapshot["conversion"] is not None
        and snapshot == {
            **applied_expected,
            "duplicate_purpose_reversal": snapshot["duplicate_purpose_reversal"],
        }
    ):
        status = "already_applied"
        mismatches = []
    else:
        status = "blocked"
        mismatches = [
            {
                "field": key,
                "actual": snapshot.get(key),
                "expected_ready": ready_expected.get(key),
                "expected_applied": applied_expected.get(key),
            }
            for key in snapshot
            if snapshot.get(key) != ready_expected.get(key)
            and snapshot.get(key) != applied_expected.get(key)
        ]
    return {
        "status": status,
        "target_order_item_id": ORDER_ITEM_ID,
        "mismatches": mismatches,
        "snapshot": snapshot,
    }


def inspect_plan(database: Path) -> dict[str, Any]:
    engine = _readonly_engine(database)
    try:
        with Session(engine) as db:
            if int(db.execute(text("PRAGMA query_only")).scalar_one()) != 1:
                raise RuntimeError("SQLite query_only is not enabled")
            return build_plan(db)
    finally:
        engine.dispose()


def apply_reconciliation(
    *,
    database: Path,
    backup_dir: Path,
    actor_username: str,
    expected_sha256: str,
) -> dict[str, Any]:
    source_sha256 = sha256_file(database)
    if source_sha256.casefold() != expected_sha256.strip().casefold():
        raise RuntimeError("database SHA-256 changed; refusing to apply")
    before_checks = database_checks(database)
    if (
        before_checks["revision"] != TARGET_REVISION
        or before_checks["integrity_check"].casefold() != "ok"
        or before_checks["foreign_key_violations"] != 0
    ):
        raise RuntimeError(f"database checks failed before apply: {before_checks}")
    preflight = inspect_plan(database)
    if preflight["status"] == "already_applied":
        return {
            "changed": False,
            "database_sha256": source_sha256,
            "checks": before_checks,
            "plan": preflight,
        }
    if preflight["status"] != "ready":
        raise RuntimeError(f"YL P0-32 reconciliation is blocked: {preflight}")

    backup = backup_to_nas(
        source_path=database,
        backup_dir=backup_dir,
        filename_suffix="_P0_32_YL_OVERRECEIPT_BEFORE_RECONCILIATION",
        keep_regular=1_000_000,
    )
    backup_checks = database_checks(backup.path)
    backup_plan = inspect_plan(backup.path)
    if (
        backup.integrity_check.casefold() != "ok"
        or backup.size <= 0
        or backup_checks != before_checks
        or backup_plan != preflight
        or sha256_file(database).casefold() != source_sha256.casefold()
    ):
        raise RuntimeError("verified backup does not match the live preflight")

    engine = create_sqlite_engine(database, busy_timeout_ms=30_000)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as db:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            actor = db.scalar(select(User).where(User.username == actor_username))
            if actor is None or not actor.is_active or actor.role not in {"admin", "boss"}:
                raise RuntimeError("audit actor must be an active admin or boss")
            live_plan = build_plan(db)
            if live_plan != preflight or live_plan["status"] != "ready":
                raise RuntimeError("data changed after backup; refusing to continue")

            revert_receipt_item(
                db,
                user=actor,
                receipt_item_id=DUPLICATE_RECEIPT_ID,
                reason="修正重复提交形成的第二笔 602 片来料；保留首笔真实实收",
                idempotency_key=REVERSAL_KEY,
                audit_context={"source": "script"},
            )
            postings = consume_receipt_reserve_for_completion_adjustment(
                db,
                production_completion_id=COMPLETION_ID,
                desired_completion_quantity=602,
                operator_id=int(actor.id),
                idempotency_key="p0-32-yl-10147-reconcile",
            )
            if len(postings) != 1:
                raise RuntimeError(
                    f"expected one reserve conversion posting, got {len(postings)}"
                )
            posting = postings[0]
            if (
                posting.receipt_purpose_allocation_id != PRIMARY_ALLOCATION_ID
                or posting.semi_finished_inventory_lot_id != PRIMARY_RESERVE_LOT_ID
                or posting.converted_sheet_quantity != 2
                or posting.finished_quantity_delta != 2
                or posting.supported_finished_quantity_before != 600
                or posting.supported_finished_quantity_after != 602
            ):
                raise RuntimeError(f"unexpected reserve conversion posting: {posting}")
            db.add(
                ProductionCompletionReserveConversion(
                    production_completion_id=COMPLETION_ID,
                    receipt_purpose_allocation_id=PRIMARY_ALLOCATION_ID,
                    semi_finished_inventory_lot_id=PRIMARY_RESERVE_LOT_ID,
                    finished_inventory_lot_id=FINISHED_LOT_ID,
                    semi_consume_movement_id=posting.semi_consume_movement_id,
                    finished_adjust_movement_id=FINISHED_ADJUST_MOVEMENT_ID,
                    converted_sheet_quantity=2,
                    finished_quantity_delta=2,
                    supported_finished_quantity_before=600,
                    supported_finished_quantity_after=602,
                    idempotency_key=CONVERSION_KEY,
                    request_hash=posting.request_hash,
                    created_by=int(actor.id),
                )
            )
            customer = db.get(Customer, CUSTOMER_ID)
            append_audit_event(
                db,
                event_category="system",
                result="success",
                source="script",
                module_code="incoming",
                action_code=ACTION_CODE,
                resource="ProductionCompletionReserveConversion",
                actor=actor,
                entity_type="order_item",
                entity_id=ORDER_ITEM_ID,
                object_ref=f"order-item:{ORDER_ITEM_ID}",
                customer_id=CUSTOMER_ID,
                customer_name=customer.name if customer is not None else None,
                batch_id="p0-32-yl-10147",
                description="Reconciled two reserve sheets already represented by finished output",
                details={
                    "source": SOURCE,
                    "product_code": PRODUCT_CODE,
                    "duplicate_receipt_item_id": DUPLICATE_RECEIPT_ID,
                    "receipt_purpose_allocation_id": PRIMARY_ALLOCATION_ID,
                    "semi_finished_inventory_lot_id": PRIMARY_RESERVE_LOT_ID,
                    "finished_inventory_lot_id": FINISHED_LOT_ID,
                    "converted_sheet_quantity": 2,
                    "finished_quantity": 602,
                },
            )
            db.flush()
            postflight = build_plan(db)
            if postflight["status"] != "already_applied":
                raise RuntimeError(f"transaction postcondition failed: {postflight}")
            db.commit()
    finally:
        engine.dispose()

    after_checks = database_checks(database)
    expected_deltas = {
        "incoming_receipts": 0,
        "incoming_receipt_items": 0,
        "incoming_receipt_purpose_allocations": 0,
        "incoming_receipt_purpose_reversals": 1,
        "production_completion_reserve_conversions": 1,
        "production_completions": 0,
        "inventory_lots": 0,
        "inventory_movements": 2,
        "operation_logs": 2,
    }
    for table, expected_delta in expected_deltas.items():
        actual_delta = (
            after_checks["counts"].get(table, 0)
            - before_checks["counts"].get(table, 0)
        )
        if actual_delta != expected_delta:
            raise RuntimeError(
                f"unexpected row-count delta for {table}: {actual_delta}, "
                f"expected {expected_delta}"
            )
    if (
        after_checks["revision"] != TARGET_REVISION
        or after_checks["integrity_check"].casefold() != "ok"
        or after_checks["foreign_key_violations"] != 0
    ):
        raise RuntimeError(f"database checks failed after apply: {after_checks}")
    postflight = inspect_plan(database)
    if postflight["status"] != "already_applied":
        raise RuntimeError(f"postflight is not closed: {postflight}")
    return {
        "changed": True,
        "database_sha256_before": source_sha256,
        "database_sha256_after": sha256_file(database),
        "backup": {
            **asdict(backup),
            "path": str(backup.path),
            "checks": backup_checks,
        },
        "checks_before": before_checks,
        "checks_after": after_checks,
        "plan_before": preflight,
        "plan_after": postflight,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--actor", default="admin")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--confirm-service-stopped", action="store_true")
    parser.add_argument("--expected-sha256")
    parser.add_argument("--backup-dir", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    database = args.database.resolve(strict=True)
    if args.apply:
        if args.confirm != CONFIRMATION:
            raise SystemExit(f"--apply requires --confirm {CONFIRMATION}")
        if not args.confirm_service_stopped:
            raise SystemExit("--apply requires --confirm-service-stopped")
        if not args.expected_sha256:
            raise SystemExit("--apply requires --expected-sha256")
        if not args.backup_dir:
            raise SystemExit("--apply requires --backup-dir")
        result = apply_reconciliation(
            database=database,
            backup_dir=args.backup_dir.resolve(),
            actor_username=args.actor,
            expected_sha256=args.expected_sha256,
        )
    else:
        result = {
            "changed": False,
            "apply": False,
            "database": str(database),
            "database_sha256": sha256_file(database),
            "checks": database_checks(database),
            "plan": inspect_plan(database),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
