"""Compare layered home reads with full home on a managed read-only copy.

Stores hashes/counts only. Uses the existing read guard, never starts ERP, and
does not measure authentication, HTTP, network, or browser rendering.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.benchmarks.erp3_read_baseline import (
    ReadOnlySQLiteGuard, file_sha256, managed_database, payload_summary,
)


def measure(database, guard, repeats):
    import sqlite3
    from sqlalchemy import create_engine, event, select
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session, selectinload
    from sqlalchemy.pool import NullPool
    from fastapi.encoders import jsonable_encoder
    from fastapi import HTTPException
    from app.core import time_contract
    frozen = datetime(2026, 10, 22, 4, tzinfo=timezone.utc)
    time_contract._utc_now_aware = lambda: frozen
    from app.api import dashboard
    from app.models.user import User

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen.astimezone(tz) if tz else frozen.replace(tzinfo=None)

    dashboard.datetime = FixedDatetime
    engine = create_engine("sqlite+pysqlite://", creator=lambda: sqlite3.connect(database), poolclass=NullPool)
    with Session(engine, autoflush=False) as db:
        users = db.scalars(select(User).where(User.is_active.is_(True)).options(
            selectinload(User.permission_overrides)).order_by(User.id)).all()
        contexts = []
        for label, role, scope in [("admin_all", "admin", "all"), ("sales_selected", "sales", "selected"),
                                    ("workshop_all", "workshop", "all")]:
            user = next(u for u in users if u.role == role and u.customer_access_mode == scope)
            contexts.append((label, user))
        contexts.append(("synthetic_boss", User(id=-9001, username="readonly-boss", role="boss",
                        customer_access_mode="all", is_active=True, permission_overrides=[])))
        contexts.append(("synthetic_empty_workshop", User(id=-9002, username="readonly-empty", role="workshop",
                        customer_access_mode="selected", is_active=True, permission_overrides=[])))
        revision = db.connection().exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()

    sql_count = 0

    def record(*_args):
        nonlocal sql_count
        sql_count += 1

    event.listen(Engine, "before_cursor_execute", record)
    output = []
    try:
        for label, user in contexts:
            row = {"context": label, "reads": {}, "checks": {}}
            payloads = {}
            for name in ["full", "summary", "stock", "production", "delivery", "finance", "approval"]:
                runs = []
                for _ in range(repeats):
                    sql_count = 0
                    started = time.perf_counter()
                    with Session(engine, autoflush=False) as db:
                        try:
                            value = (dashboard.dashboard_overview(db=db, user=user, mode=name)
                                     if name in {"full", "summary"}
                                     else dashboard.dashboard_section(group=name, db=db, user=user))
                            value = jsonable_encoder(value)
                        except HTTPException as error:
                            if error.status_code != 403 or name in {"full", "summary"}:
                                raise
                            value = {"permission_denied": True}
                    runs.append({"sql_count": sql_count, "elapsed_ms": round((time.perf_counter()-started)*1000, 3),
                                 "payload": payload_summary(value)})
                    payloads[name] = value
                row["reads"][name] = {"runs": runs, "median_ms": statistics.median(r["elapsed_ms"] for r in runs),
                                       "stable": len({r["payload"]["sha256"] for r in runs}) == 1}

            full, summary = payloads["full"], payloads["summary"]
            available = summary["available_groups"]
            cards = {card["key"]: card for card in full["cards"]}
            combined_cards, combined_tasks, duplicate_tasks, duplicate_cards = {}, {}, set(), set()
            deferred_fields = {"count", "amount", "waiting_count", "due_waiting_count"}
            row["checks"]["summary_does_not_claim_loaded_counts"] = all(
                value is None for card in summary["cards"] for key, value in card.items() if key in deferred_fields)
            metadata = lambda values: {card["key"]: {key: value for key, value in card.items()
                                                    if key not in deferred_fields} for card in values}
            row["checks"]["summary_preserves_card_metadata"] = metadata(summary["cards"]) == metadata(full["cards"])
            row["checks"]["summary_card_keys_unique"] = len(summary["cards"]) == len(metadata(summary["cards"]))
            row["checks"]["summary_has_no_derived_tasks"] = not summary["workbench"]["tasks"]
            for group in ["stock", "production", "delivery", "finance", "approval"]:
                value = payloads[group]
                permitted = group in available
                row["checks"][group + "_permission"] = permitted != bool(value.get("permission_denied"))
                if not permitted:
                    continue
                for card in value["cards"]:
                    if card["key"] in combined_cards:
                        duplicate_cards.add(card["key"])
                    combined_cards[card["key"]] = card
                for task in value["workbench"]["tasks"]:
                    key = task["id"]
                    if key in combined_tasks:
                        duplicate_tasks.add(key)
                    combined_tasks[key] = task
                if group == "stock":
                    row["checks"]["stock_warnings_equal"] = value.get("low_stock_warnings", []) == full.get("low_stock_warnings", [])
            row["checks"]["all_cards_equal"] = combined_cards == cards
            row["checks"]["no_duplicate_cards_across_groups"] = not duplicate_cards
            row["checks"]["all_tasks_equal"] = combined_tasks == {task["id"]: task for task in full["workbench"]["tasks"]}
            row["checks"]["no_duplicate_tasks_across_groups"] = not duplicate_tasks
            row["checks"]["all_repeats_stable"] = all(read["stable"] for read in row["reads"].values())
            row["task_count"] = len(combined_tasks)
            row["passed"] = all(row["checks"].values())
            output.append(row)
    finally:
        event.remove(Engine, "before_cursor_execute", record)
        engine.dispose()
    return {"revision": revision, "contexts": output, "passed": all(row["passed"] for row in output)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=3)
    args = parser.parse_args()
    database = managed_database()
    before = file_sha256(database)
    with ReadOnlySQLiteGuard(database) as guard:
        result = measure(database, guard, args.repeats)
        result["denied_operations"] = guard.denied
    project = Path(__file__).resolve().parents[2]
    result.update(schema="erp3.layered-home-readonly.v1", repeats=args.repeats,
                  as_of="2026-10-22T12:00:00+08:00",
                  code_sha256={name: file_sha256(project / name) for name in (
                      "scripts/benchmarks/erp3_layered_home.py", "app/api/dashboard.py",
                      "app/services/dashboard_workbench.py", "app/services/order_replenishment_supply.py")},
                  database_sha256_before=before, database_sha256_after=file_sha256(database),
                  scope="Historical managed snapshot; handler/ORM/JSON only, not production/browser performance")
    result["passed"] = result["passed"] and result["denied_operations"] == 0 and before == result["database_sha256_after"]
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"passed": result["passed"], "contexts": len(result["contexts"]), "denied_operations": result["denied_operations"]}))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
