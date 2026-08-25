"""Audit or reconcile stale virtual-composite production tasks.

Dry-run is the default and opens SQLite in read-only mode.  Apply is intended
only for a separately authorized maintenance window: it requires an exact
database SHA-256, explicit order-item ids, a verified backup, a stopped-service
confirmation, and an admin/boss audit actor.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import asdict
from math import ceil
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.database import backup_to_nas, create_sqlite_engine  # noqa: E402
from app.models.customer import Customer  # noqa: E402
from app.models.order import Order, OrderItem  # noqa: E402
from app.models.product_bom import SalesOrderItemBomComponent  # noqa: E402
from app.models.production import ProductionCompletion, ProductionTask  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.audit_log import append_audit_event  # noqa: E402
from app.services.composite_bom_workflow import (  # noqa: E402
    component_available_quantity,
    effective_component_demands,
)
from app.services.order_status_policy import (  # noqa: E402
    ORDER_ITEM_ACTIVE_ORDER_STATUSES,
)
from app.services.production_workflow import (  # noqa: E402
    cutting_output_factor,
    refresh_production_task,
    _virtual_composite_receipts_are_complete,
)


CONFIRMATION = "APPLY_STALE_VIRTUAL_COMPOSITE_TASK_RECONCILIATION_P0_20"
SOURCE = "scripts.admin.reconcile_stale_virtual_composite_tasks"
ACTION_CODE = "production.task.reconcile_component_receipts"
PROTECTED_COUNT_TABLES = (
    "sales_orders",
    "sales_order_items",
    "material_requisition_items",
    "requisition_item_bom_sources",
    "incoming_receipts",
    "incoming_receipt_items",
    "production_tasks",
    "production_completions",
    "inventory_lots",
    "inventory_reservations",
    "inventory_movements",
    "sales_deliveries",
    "sales_delivery_items",
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
        table_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        counts = {
            table: int(
                connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
            for table in (*PROTECTED_COUNT_TABLES, "operation_logs")
            if table in table_names
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


def _task_snapshot(task: ProductionTask) -> dict[str, Any]:
    return {
        "task_id": int(task.id),
        "bom_component_snapshot_id": int(
            task.sales_order_item_bom_component_id
        ),
        "status": str(task.status),
        "planned_quantity": int(task.planned_quantity or 0),
        "material_received_quantity": int(task.material_received_quantity or 0),
        "material_input_quantity": int(task.material_input_quantity or 0),
        "output_factor": int(task.output_factor or 1),
        "readiness_basis": task.readiness_basis,
        "version": int(task.version or 0),
    }


def build_plan(
    db: Session,
    *,
    order_item_ids: Sequence[int] | None = None,
) -> dict[str, Any]:
    selected = (
        sorted({int(value) for value in order_item_ids if int(value) > 0})
        if order_item_ids is not None
        else None
    )
    query = (
        select(OrderItem, Order, Customer)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Customer, Customer.id == Order.customer_id)
        .where(
            Order.status.in_(ORDER_ITEM_ACTIVE_ORDER_STATUSES),
            OrderItem.is_force_closed.is_(False),
            OrderItem.quantity > OrderItem.delivered_quantity,
            OrderItem.is_virtual_composite_parent_snapshot.is_(True),
            OrderItem.supply_mode_snapshot != "external_purchase",
        )
        .order_by(Order.id, OrderItem.id)
    )
    if selected is not None:
        query = query.where(OrderItem.id.in_(selected))

    targets: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    seen_item_ids: set[int] = set()
    for item, order, customer in db.execute(query).all():
        item_id = int(item.id)
        seen_item_ids.add(item_id)
        demands = effective_component_demands(db, item_id)
        if not _virtual_composite_receipts_are_complete(
            db,
            item,
            demands=demands,
        ):
            continue
        tasks = list(
            db.scalars(
                select(ProductionTask)
                .where(ProductionTask.order_item_id == item_id)
                .order_by(ProductionTask.id)
            ).all()
        )
        waiting_tasks = [task for task in tasks if task.status == "waiting_material"]
        if not waiting_tasks:
            continue
        waiting_task_ids = [int(task.id) for task in waiting_tasks]
        completion_ids = list(
            db.scalars(
                select(ProductionCompletion.id).where(
                    ProductionCompletion.task_id.in_(waiting_task_ids),
                    ProductionCompletion.status == "posted",
                )
            ).all()
        )
        if completion_ids:
            blockers.append(
                {
                    "order_item_id": item_id,
                    "reason": "waiting_task_has_posted_completion",
                    "task_ids": waiting_task_ids,
                    "completion_ids": [int(value) for value in completion_ids],
                }
            )
            continue

        demand_by_snapshot = {int(row.snapshot_id): row for row in demands}
        projected_tasks: list[dict[str, Any]] = []
        row_blockers: list[str] = []
        for task in waiting_tasks:
            snapshot_id = int(task.sales_order_item_bom_component_id or 0)
            demand = demand_by_snapshot.get(snapshot_id)
            if snapshot_id <= 0 or demand is None:
                row_blockers.append(
                    f"task {task.id} has no effective component demand"
                )
                continue
            snapshot = db.get(SalesOrderItemBomComponent, snapshot_id)
            if snapshot is None:
                row_blockers.append(
                    f"task {task.id} component snapshot {snapshot_id} is missing"
                )
                continue
            coverage = max(component_available_quantity(db, snapshot_id), 0)
            production_needed = max(
                int(demand.required_piece_quantity) - coverage,
                0,
            )
            factor = cutting_output_factor(
                snapshot.snapshot_component_default_cutting_mode
            )
            if production_needed <= 0:
                expected_status = "not_required"
                expected_input = 0
                expected_basis = "component_finished_inventory"
            else:
                expected_status = "pending"
                expected_input = ceil(production_needed / max(factor, 1))
                expected_basis = "component_receipts_reconciled"
            projected_tasks.append(
                {
                    **_task_snapshot(task),
                    "expected_status": expected_status,
                    "expected_planned_quantity": production_needed,
                    "expected_material_input_quantity": expected_input,
                    "expected_output_factor": factor,
                    "expected_readiness_basis": expected_basis,
                    "expected_version": int(task.version or 0) + 1,
                }
            )
        if row_blockers:
            blockers.append(
                {
                    "order_item_id": item_id,
                    "reason": "invalid_waiting_task_projection",
                    "details": row_blockers,
                }
            )
            continue
        targets.append(
            {
                "order_id": int(order.id),
                "order_number": order.order_number,
                "customer_po": order.customer_po,
                "customer_id": int(order.customer_id),
                "customer_name": customer.name,
                "order_item_id": item_id,
                "item_order_number": item.item_order_number,
                "product_code": item.snapshot_product_code,
                "product_name": item.snapshot_product_name,
                "ordered_set_quantity": int(item.quantity or 0),
                "delivered_set_quantity": int(item.delivered_quantity or 0),
                "material_status": item.material_status,
                "tasks_before": [_task_snapshot(task) for task in tasks],
                "projected_waiting_tasks": projected_tasks,
            }
        )

    missing_selected_ids = (
        sorted(set(selected) - seen_item_ids) if selected is not None else []
    )
    if missing_selected_ids:
        blockers.append(
            {
                "reason": "selected_order_items_not_active_virtual_composites",
                "order_item_ids": missing_selected_ids,
            }
        )
    status = "blocked" if blockers else "ready" if targets else "already_applied"
    return {
        "status": status,
        "selected_order_item_ids": selected,
        "target_count": len(targets),
        "target_order_item_ids": [row["order_item_id"] for row in targets],
        "blocker_count": len(blockers),
        "blockers": blockers,
        "targets": targets,
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


def inspect_plan(
    database: Path,
    *,
    order_item_ids: Sequence[int] | None = None,
) -> dict[str, Any]:
    engine = _readonly_engine(database)
    try:
        with Session(engine) as db:
            if int(db.execute(text("PRAGMA query_only")).scalar_one()) != 1:
                raise RuntimeError("SQLite query_only is not enabled")
            return build_plan(db, order_item_ids=order_item_ids)
    finally:
        engine.dispose()


def apply_reconciliation(
    *,
    database: Path,
    backup_dir: Path,
    actor_username: str,
    expected_sha256: str,
    order_item_ids: Sequence[int],
) -> dict[str, Any]:
    selected = sorted({int(value) for value in order_item_ids if int(value) > 0})
    if not selected:
        raise RuntimeError("apply requires at least one explicit order-item id")
    source_sha256 = sha256_file(database)
    if source_sha256.casefold() != expected_sha256.strip().casefold():
        raise RuntimeError("database SHA-256 changed; refusing to apply")
    before_checks = database_checks(database)
    if (
        before_checks["integrity_check"].casefold() != "ok"
        or before_checks["foreign_key_violations"] != 0
    ):
        raise RuntimeError(f"database checks failed before apply: {before_checks}")
    preflight = inspect_plan(database, order_item_ids=selected)
    if preflight["status"] != "ready":
        raise RuntimeError(f"reconciliation preflight is not ready: {preflight}")
    if preflight["target_order_item_ids"] != selected:
        raise RuntimeError("explicit order-item ids do not exactly match ready targets")

    backup = backup_to_nas(
        source_path=database,
        backup_dir=backup_dir,
        filename_suffix="_P0_20_BEFORE_PRODUCTION_TASK_RECONCILIATION",
        keep_regular=1_000_000,
    )
    backup_checks = database_checks(backup.path)
    backup_plan = inspect_plan(backup.path, order_item_ids=selected)
    if (
        backup.integrity_check.casefold() != "ok"
        or backup.size <= 0
        or backup_checks != before_checks
        or backup_plan != preflight
        or sha256_file(database).casefold() != source_sha256.casefold()
    ):
        raise RuntimeError("verified backup does not match the live preflight")

    batch_id = f"p0-20-task-reconcile-{source_sha256[:16]}"
    engine = create_sqlite_engine(database, busy_timeout_ms=30_000)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as db:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            actor = db.scalar(select(User).where(User.username == actor_username))
            if actor is None or not actor.is_active or actor.role not in {"admin", "boss"}:
                raise RuntimeError("audit actor must be an active admin or boss")
            live_plan = build_plan(db, order_item_ids=selected)
            if live_plan != preflight or live_plan["status"] != "ready":
                raise RuntimeError("data changed after backup; refusing to continue")
            for target in live_plan["targets"]:
                item = db.get(OrderItem, int(target["order_item_id"]))
                if item is None:
                    raise RuntimeError("target order item disappeared in transaction")
                refresh_production_task(db, item.id, create_if_missing=False)
                tasks_after = list(
                    db.scalars(
                        select(ProductionTask)
                        .where(ProductionTask.order_item_id == item.id)
                        .order_by(ProductionTask.id)
                    ).all()
                )
                after_by_id = {int(task.id): _task_snapshot(task) for task in tasks_after}
                for expected in target["projected_waiting_tasks"]:
                    actual = after_by_id[int(expected["task_id"])]
                    for actual_key, expected_key in (
                        ("status", "expected_status"),
                        ("planned_quantity", "expected_planned_quantity"),
                        (
                            "material_input_quantity",
                            "expected_material_input_quantity",
                        ),
                        ("output_factor", "expected_output_factor"),
                        ("readiness_basis", "expected_readiness_basis"),
                        ("version", "expected_version"),
                    ):
                        if actual[actual_key] != expected[expected_key]:
                            raise RuntimeError(
                                f"task {actual['task_id']} postcondition mismatch: "
                                f"{actual_key}={actual[actual_key]!r}, "
                                f"expected {expected[expected_key]!r}"
                            )
                append_audit_event(
                    db,
                    event_category="system",
                    result="success",
                    source="script",
                    module_code="production",
                    action_code=ACTION_CODE,
                    resource="ProductionTask",
                    actor=actor,
                    entity_type="order_item",
                    entity_id=item.id,
                    object_ref=item.item_order_number or f"order-item:{item.id}",
                    customer_id=int(target["customer_id"]),
                    customer_name=target["customer_name"],
                    batch_id=batch_id,
                    description="Reconciled component task readiness from exact posted receipts",
                    details={
                        "source": SOURCE,
                        "order_number": target["order_number"],
                        "product_code": target["product_code"],
                        "before": target["tasks_before"],
                        "after": list(after_by_id.values()),
                    },
                )
            db.commit()
    finally:
        engine.dispose()

    after_checks = database_checks(database)
    for table in PROTECTED_COUNT_TABLES:
        if before_checks["counts"].get(table) != after_checks["counts"].get(table):
            raise RuntimeError(f"protected row count changed for {table}")
    audit_delta = (
        after_checks["counts"].get("operation_logs", 0)
        - before_checks["counts"].get("operation_logs", 0)
    )
    if audit_delta != len(selected):
        raise RuntimeError(
            f"operation log delta {audit_delta} does not match target count {len(selected)}"
        )
    if (
        after_checks["integrity_check"].casefold() != "ok"
        or after_checks["foreign_key_violations"] != 0
    ):
        raise RuntimeError("database checks failed after reconciliation")
    postflight = inspect_plan(database, order_item_ids=selected)
    if postflight["status"] != "already_applied":
        raise RuntimeError(f"postflight is not closed: {postflight}")
    return {
        "changed": True,
        "batch_id": batch_id,
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
    parser.add_argument("--item-id", type=int, action="append", default=[])
    parser.add_argument("--actor", default="admin")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--confirm-service-stopped", action="store_true")
    parser.add_argument("--expected-sha256")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--report", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    database = args.database.resolve(strict=True)
    if not database.is_file():
        raise SystemExit(f"database is not a file: {database}")
    selected = sorted({int(value) for value in args.item_id if int(value) > 0})
    if args.apply:
        if args.confirm != CONFIRMATION:
            raise SystemExit(f"--apply requires --confirm {CONFIRMATION}")
        if not args.confirm_service_stopped:
            raise SystemExit("--apply requires --confirm-service-stopped")
        if not args.expected_sha256:
            raise SystemExit("--apply requires --expected-sha256")
        if not args.backup_dir:
            raise SystemExit("--apply requires --backup-dir")
        if not selected:
            raise SystemExit("--apply requires at least one explicit --item-id")
        result = apply_reconciliation(
            database=database,
            backup_dir=args.backup_dir.resolve(),
            actor_username=args.actor,
            expected_sha256=args.expected_sha256,
            order_item_ids=selected,
        )
    else:
        result = {
            "changed": False,
            "apply": False,
            "database": str(database),
            "database_sha256": sha256_file(database),
            "checks": database_checks(database),
            "plan": inspect_plan(
                database,
                order_item_ids=selected or None,
            ),
        }
    if args.report:
        report = args.report.resolve()
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
