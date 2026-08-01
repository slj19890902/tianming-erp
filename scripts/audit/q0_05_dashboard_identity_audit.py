"""Q0-05 / P0-09A dashboard-to-workbench read-only identity audit.

The command has no apply mode.  It opens an explicit SQLite path in read-only
mode, enables SQLite query-only protection and writes only the requested JSON
report.  Business facts, audit logs and the Alembic version are never changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, exists, select, text
from sqlalchemy.orm import Session


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.dashboard import _workflow_projection_rows, dashboard_overview
from app.api.deps import customer_scope_ids, has_unrestricted_customer_access
from app.api.deliveries import pending_delivery_items
from app.api.finance import _pending_statement_groups, _statement_period
from app.api.incoming import pending_items as pending_incoming_items
from app.api.requisition import pending_requisitions
from app.api.production import get_production_tasks
from app.models.customer import Customer
from app.models.delivery import Delivery
from app.models.finance import ReturnReceipt, Statement
from app.models.user import User
from app.services.dashboard_metric_contracts import build_authoritative_snapshot


BEIJING = ZoneInfo("Asia/Shanghai")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _creator(path: Path):
    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    return connect


def _json_default(value: Any):
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"不能序列化 {type(value).__name__}")


def _legacy_snapshot(db: Session, user: User, today) -> dict:
    visible_customer_ids = (
        None
        if has_unrestricted_customer_access(user, db)
        else customer_scope_ids(user, db)
    )
    rows = _workflow_projection_rows(
        db,
        visible_customer_ids=visible_customer_ids,
        include_delivery=True,
        include_finance=True,
    )

    def due(row: dict) -> bool:
        return row["delivery_date"] is None or row["delivery_date"] <= today

    identities = {
        "pending_material": sorted(
            {
                f"order:{row['order_id']}"
                for row in rows
                if row["business_status"] == "pending_material" and due(row)
            }
        ),
        "pending_incoming": sorted(
            f"order-item:{row['item_id']}"
            for row in rows
            if row["business_status"] == "pending_incoming" and due(row)
        ),
        "pending_production": sorted(
            f"order-item:{row['item_id']}"
            for row in rows
            if row["business_status"] == "pending_production" and due(row)
        ),
        "pending_delivery": sorted(
            f"order-item:{row['item_id']}"
            for row in rows
            if row["business_status"] in {"pending_delivery", "partially_delivered"}
            and due(row)
        ),
        "pending_receipt": sorted(
            f"order-item:{row['item_id']}"
            for row in rows
            if row["business_status"] == "waiting_receipt"
        ),
        "pending_reconciliation": sorted(
            f"order-item:{row['item_id']}"
            for row in rows
            if row["business_status"] == "pending_reconciliation"
        ),
    }
    overview = dashboard_overview(db=db, user=user)
    return {
        "cards": {
            card["key"]: {
                "count": int(card.get("count") or 0),
                "amount": str(card["amount"]) if card.get("amount") is not None else None,
            }
            for card in overview.get("cards", [])
        },
        "identities": identities,
    }


def _authoritative_snapshot(
    db: Session,
    user: User,
    *,
    statement_month: str,
    as_of: str,
) -> dict:
    visible_customer_ids = (
        None
        if has_unrestricted_customer_access(user, db)
        else customer_scope_ids(user, db)
    )
    pending_material = pending_requisitions(db=db, _user=user).get("items", [])
    pending_incoming = pending_incoming_items(db=db, user=user).get("items", [])
    pending_production = get_production_tasks(
        task_status="pending",
        db=db,
        user=user,
    ).get("items", [])
    pending_delivery = pending_delivery_items(db=db, user=user).get("items", [])

    pending_receipt_query = select(Delivery.id, Delivery.customer_id).where(
        Delivery.status == "dispatched",
        ~exists(
            select(ReturnReceipt.id).where(
                ReturnReceipt.delivery_id == Delivery.id,
                ReturnReceipt.status == "confirmed",
            )
        ),
    )
    if visible_customer_ids is not None:
        pending_receipt_query = pending_receipt_query.where(
            Delivery.customer_id.in_(visible_customer_ids)
        )
    pending_receipt = [
        {"delivery_id": delivery_id, "customer_id": customer_id}
        for delivery_id, customer_id in db.execute(pending_receipt_query)
    ]

    pending_reconciliation: list[dict] = []
    customer_query = select(Customer).order_by(Customer.id)
    if visible_customer_ids is not None:
        customer_query = customer_query.where(Customer.id.in_(visible_customer_ids))
    for customer in db.scalars(customer_query).all():
        period_start, period_end = _statement_period(
            statement_month,
            customer.statement_cycle_start_day,
        )
        groups = _pending_statement_groups(
            db,
            customer.id,
            period_start=period_start,
            period_end=period_end,
        )
        pending_groups = [row for row in groups if int(row.get("pending_item_count") or 0) > 0]
        if not pending_groups:
            continue
        pending_reconciliation.append(
            {
                "customer_id": customer.id,
                "statement_month": statement_month,
                "amount": sum(
                    (Decimal(str(row.get("total_receivable_amount") or 0)) for row in pending_groups),
                    Decimal("0.00"),
                ),
                "delivery_ids": sorted(int(row["delivery_id"]) for row in pending_groups),
            }
        )

    statement_query = select(Statement).order_by(Statement.id)
    if visible_customer_ids is not None:
        statement_query = statement_query.where(
            Statement.customer_id.in_(visible_customer_ids)
        )
    statements = [
        {
            "statement_id": row.id,
            "customer_id": row.customer_id,
            "statement_month": row.statement_month,
            "status": row.status,
            "total_receivable": row.total_receivable,
            "invoiced_amount": row.invoiced_amount,
            "settled_amount": row.settled_amount,
        }
        for row in db.scalars(statement_query).all()
    ]
    return build_authoritative_snapshot(
        pending_material_rows=pending_material,
        pending_incoming_rows=pending_incoming,
        pending_production_rows=pending_production,
        pending_delivery_rows=pending_delivery,
        pending_receipt_rows=pending_receipt,
        pending_reconciliation_rows=pending_reconciliation,
        statement_rows=statements,
        statement_month=statement_month,
        as_of=as_of,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite-path", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--username", default="admin")
    parser.add_argument("--statement-month")
    args = parser.parse_args()

    database_path = args.sqlite_path.resolve(strict=True)
    output_path = args.output_json.resolve(strict=False)
    if output_path == database_path:
        parser.error("报告路径不能覆盖数据库")
    before_hash = _sha256(database_path)
    now = datetime.now(BEIJING)
    statement_month = args.statement_month or now.strftime("%Y-%m")

    engine = create_engine("sqlite://", creator=_creator(database_path))
    try:
        with Session(engine) as db:
            query_only = int(db.execute(text("PRAGMA query_only")).scalar_one())
            if query_only != 1:
                raise RuntimeError("SQLite query_only 未启用")
            user = db.scalar(select(User).where(User.username == args.username))
            if user is None:
                parser.error(f"数据库中不存在账号：{args.username}")
            version = db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            quick_check = db.execute(text("PRAGMA quick_check")).scalar_one()
            foreign_key_errors = len(
                db.execute(text("PRAGMA foreign_key_check")).all()
            )
            report = {
                "database": str(database_path),
                "database_sha256_before": before_hash,
                "revision": version,
                "quick_check": quick_check,
                "foreign_key_errors": foreign_key_errors,
                "username": user.username,
                "as_of": now.isoformat(timespec="seconds"),
                "statement_month": statement_month,
                "legacy_dashboard": _legacy_snapshot(db, user, now.date()),
                "authoritative": _authoritative_snapshot(
                    db,
                    user,
                    statement_month=statement_month,
                    as_of=now.isoformat(timespec="seconds"),
                ),
            }
            legacy_aliases = {
                "pending_payment": "unsettled_statements",
            }
            comparison = {}
            for metric_key, metric in report["authoritative"]["metrics"].items():
                legacy_key = legacy_aliases.get(metric_key, metric_key)
                legacy_card = report["legacy_dashboard"]["cards"].get(legacy_key)
                legacy_count = int((legacy_card or {}).get("count") or 0)
                authoritative_count = int(metric["count"])
                comparison[metric_key] = {
                    "legacy_card_key": legacy_key,
                    "legacy_count": legacy_count,
                    "authoritative_count": authoritative_count,
                    "count_matches": legacy_count == authoritative_count,
                    "diagnosis": (
                        "数量一致；仍须逐项核对稳定身份。"
                        if legacy_count == authoritative_count
                        else "旧首页使用订单状态投影；权威数量来自实际业务页面集合。"
                    ),
                }
            report["comparison"] = comparison
    finally:
        engine.dispose()

    after_hash = _sha256(database_path)
    report["database_sha256_after"] = after_hash
    report["database_unchanged"] = before_hash == after_hash
    if not report["database_unchanged"]:
        raise RuntimeError("只读审计前后数据库哈希发生变化")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    print(json.dumps({
        "output": str(output_path),
        "database_unchanged": True,
        "legacy_cards": report["legacy_dashboard"]["cards"],
        "authoritative_counts": {
            key: value["count"]
            for key, value in report["authoritative"]["metrics"].items()
        },
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
