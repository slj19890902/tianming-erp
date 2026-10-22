"""Measure read handlers on a run_task-owned copy; never start the application.

Run through scripts/uat/run_task.py after bringing ONLY its owned copy to the
candidate schema. Output contains counts, timings and hashes, never payloads.
This is an in-process handler baseline, not HTTP/browser/production latency.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import sqlite3
import statistics
import sys
import time
import traceback
from urllib.parse import unquote, urlsplit


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class ReadOnlySQLiteGuard:
    """Route all stdlib SQLite connects to one inode in enforced mode=ro.

    Authorizer is an independent second barrier, also detecting attempts caught
    internally by a handler. No raw SQL, parameters or error details are kept.
    """

    def __init__(self, database):
        self.database = Path(database).resolve(strict=True)
        self.identity = (self.database.stat().st_dev, self.database.stat().st_ino)
        self.denied = 0
        self.connections = []
        self._original = sqlite3.connect
        self._dbapi_original = sqlite3.dbapi2.connect

    def authorize(self, action, arg1, arg2, database, source):
        allowed = {
            sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
            sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT,
            sqlite3.SQLITE_RECURSIVE,
        }
        if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() == "load_extension":
            pass
        elif action == sqlite3.SQLITE_PRAGMA:
            # A small read-only introspection allowlist; arguments only for
            # table metadata, never assignment of writable SQLite settings.
            name = (arg1 or "").lower()
            if name in {"table_info", "table_xinfo", "index_list", "index_info", "foreign_key_list"}:
                return sqlite3.SQLITE_OK
            if arg2 is None and name in {"query_only", "foreign_keys", "read_uncommitted", "integrity_check", "foreign_key_check"}:
                return sqlite3.SQLITE_OK
        elif action in allowed:
            return sqlite3.SQLITE_OK
        self.denied += 1
        return sqlite3.SQLITE_DENY

    def connect(self, database, *args, **kwargs):
        raw = os.fspath(database)
        if raw.startswith("file:"):
            parsed = urlsplit(raw)
            if parsed.netloc:
                self.denied += 1
                raise sqlite3.DatabaseError("readonly guard rejected connection")
            raw = unquote(parsed.path)
        requested = Path(raw).resolve()
        if requested != self.database or not requested.is_file():
            self.denied += 1
            raise sqlite3.DatabaseError("readonly guard rejected connection")
        if (requested.stat().st_dev, requested.stat().st_ino) != self.identity:
            self.denied += 1
            raise sqlite3.DatabaseError("readonly guard database identity changed")
        kwargs["uri"] = True
        conn = self._original(self.database.as_uri() + "?mode=ro", *args, **kwargs)
        conn.execute("PRAGMA query_only=ON")
        conn.set_authorizer(self.authorize)
        self.connections.append(conn)
        return conn

    def __enter__(self):
        sqlite3.connect = sqlite3.dbapi2.connect = self.connect
        return self

    def __exit__(self, *exc):
        sqlite3.connect = self._original
        sqlite3.dbapi2.connect = self._dbapi_original
        for conn in self.connections:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def managed_database():
    """Validate the runner layout, without startup or process attestation."""
    if not os.environ.get("ERP_UAT_ROOT"):
        raise RuntimeError("requires scripts/uat/run_task.py managed environment")
    from app.core.uat_isolation import validate_uat_environment
    manifest = validate_uat_environment()
    root = Path(manifest["root"])
    receipt_path = root.parent / "result.json"
    receipt = json.loads(receipt_path.read_text())
    if receipt.get("schema") != "tianming.test-task.v1" or receipt.get("status") != "running":
        raise RuntimeError("requires a currently running managed task")
    if Path(receipt["copies"]).resolve() != root.resolve():
        raise RuntimeError("task ownership mismatch")
    database = Path(manifest["database"]).resolve(strict=True)
    if database == Path(receipt["source"]).resolve():
        raise RuntimeError("source cannot be a benchmark target")
    return database


def canonical_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def payload_summary(value):
    encoded = canonical_bytes(value)
    counts = {}
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, list):
                counts[key + "_count"] = len(item)
            elif key in {"total", "page", "page_size", "total_pages", "unfinished_total"} and isinstance(item, (int, float)):
                counts[key] = item
    return {"sha256": hashlib.sha256(encoded).hexdigest(), "bytes": len(encoded), "counts": counts}


def handler_arguments(handler, overrides):
    """Resolve FastAPI Query defaults explicitly for direct handler calls."""
    from fastapi.params import Param, Depends
    result = {}
    for name, param in inspect.signature(handler).parameters.items():
        default = param.default
        if isinstance(default, Param):
            result[name] = default.default
        elif not isinstance(default, Depends) and default is not inspect.Parameter.empty:
            result[name] = default
    result.update(overrides)
    return result


def measure(args, database, guard):
    from sqlalchemy import create_engine, event, select
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session, selectinload
    from sqlalchemy.pool import NullPool
    from fastapi.encoders import jsonable_encoder
    from app.core import time_contract
    frozen = datetime.fromisoformat(args.as_of).astimezone(timezone.utc)
    time_contract._utc_now_aware = lambda: frozen
    from app.api import dashboard, orders, products, incoming
    from app.api.deps import has_permission
    from app.models.user import User

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen.astimezone(tz) if tz else frozen.replace(tzinfo=None)
    dashboard.datetime = FixedDatetime

    engine = create_engine("sqlite+pysqlite://", creator=lambda: sqlite3.connect(database, check_same_thread=False), poolclass=NullPool)
    with Session(engine, autoflush=False) as db:
        active = db.scalars(select(User).where(User.is_active.is_(True)).options(selectinload(User.permission_overrides)).order_by(User.id)).all()
        candidates = [("admin_all", "admin", "all"), ("sales_selected", "sales", "selected"), ("workshop_all", "workshop", "all")]
        users = []
        for label, role, scope in candidates:
            found = next((u for u in active if u.role == role and u.customer_access_mode == scope), None)
            if found is None:
                raise RuntimeError("required representative permission context missing: " + label)
            # Eager-loaded overrides remain available after this session closes.
            users.append((label, found))
        # An empty customer scope is a synthetic read-only boundary case. It is
        # never persisted and does not claim authentication or login coverage.
        empty = User(id=min([u.id for u in active] + [0]) - 1, username="benchmark-empty", role="workshop", customer_access_mode="selected", is_active=True, permission_overrides=[])
        users.append(("workshop_empty_scope", empty))
        revision = db.connection().exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
        table_counts = {name: db.connection().exec_driver_sql(f'SELECT count(*) FROM "{name}"').scalar_one() for name in ("customers", "products", "sales_orders", "sales_order_items", "inventory_lots", "inventory_movements")}

    cases = [
        ("dashboard_overview", "dashboard.view", dashboard.dashboard_overview, {}),
        ("orders_summary", "orders.view", orders.list_orders, {"detail_level": "summary", "include_unfinished_total": False, "scope": "all", "page_size": 50}),
        ("orders_derived_summary", "orders.view", orders.list_orders, {"detail_level": "summary", "include_unfinished_total": False, "scope": "active", "stage": ["pending_material"], "page_size": 50}),
        ("products_summary", "products.view", products.list_products, {"response_mode": "summary", "page_size": 25}),
        ("incoming_history", "incoming.view", incoming.history_received_items, {"page_size": 25}),
    ]
    current = None
    sql_templates = {}
    sql_callers = {}
    def before_cursor(conn, cursor, statement, parameters, context, executemany):
        if current is not None:
            current["sql_count"] += 1
            signature = hashlib.sha256(" ".join(statement.split()).encode()).hexdigest()
            current["sql_shape_counts"][signature] += 1
            if signature not in sql_templates:
                # Engine events receive unexpanded bind placeholders. Also
                # redact SQL literals in hand-written statements.
                template = re.sub(r"'(?:''|[^'])*'", "?", " ".join(statement.split()))
                sql_templates[signature] = re.sub(r"\b\d+(?:\.\d+)?\b", "?", template)
                if args.sql_callers:
                    sql_callers[signature] = [{"file": frame.filename.split("/app/", 1)[-1], "line": frame.lineno, "function": frame.name} for frame in traceback.extract_stack() if "/app/" in frame.filename][-8:]
    event.listen(Engine, "before_cursor_execute", before_cursor)
    results = []
    try:
        for label, user in users:
            for name, permission, handler, overrides in cases:
                row = {"context": label, "case": name, "permission_allowed": has_permission(user, permission), "runs": []}
                results.append(row)
                if not row["permission_allowed"]:
                    row["status"] = "permission_denied_expected"
                    continue
                for run in range(args.repeats):
                    current = {"sql_count": 0, "sql_shape_counts": Counter()}
                    denied_before = guard.denied
                    start = time.perf_counter()
                    try:
                        with Session(engine, autoflush=False) as db:
                            value = handler(**handler_arguments(handler, dict(overrides, db=db, user=user)))
                            payload = payload_summary(jsonable_encoder(value))
                        if guard.denied != denied_before:
                            raise RuntimeError("handler attempted a prohibited database operation")
                        current.update(status="passed", payload=payload)
                    except Exception as error:
                        current.update(status="failed", error_type=type(error).__name__, error_sha256=hashlib.sha256(str(error).encode()).hexdigest(), error_frames=[{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name} for frame in traceback.extract_tb(error.__traceback__)])
                    current["elapsed_ms"] = round((time.perf_counter() - start) * 1000, 3)
                    current["denied_operations"] = guard.denied - denied_before
                    row["runs"].append(current)
                    current = None
                row["status"] = "passed" if all(r["status"] == "passed" for r in row["runs"]) else "failed"
                row["median_ms"] = statistics.median(r["elapsed_ms"] for r in row["runs"])
                if row["status"] == "passed":
                    row["stable_payload"] = len({r["payload"]["sha256"] for r in row["runs"]}) == 1
                    if not row["stable_payload"]:
                        row["status"] = "failed"
    finally:
        event.remove(Engine, "before_cursor_execute", before_cursor)
        engine.dispose()
    return {"revision": revision, "table_counts": table_counts, "cases": results, "sql_templates": sql_templates, "sql_callers": sql_callers}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repeats", type=int, default=3, choices=range(1, 11))
    parser.add_argument("--as-of", default="2026-10-22T12:00:00+08:00")
    parser.add_argument("--sql-callers", action="store_true", help="Diagnostic stack sampling; do not compare its timing with ordinary runs")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(project))
    database = managed_database()
    before = file_sha256(database)
    result = {"schema": "erp3.read-baseline.v1", "scope": "managed historical snapshot, direct handlers; no HTTP/authentication/startup", "as_of": args.as_of, "repeats": args.repeats, "database_sha256_before": before}
    result["diagnostic_stack_sampling"] = args.sql_callers
    result["code_sha256"] = {name: file_sha256(project / name) for name in ("app/api/dashboard.py", "app/api/orders.py", "app/api/products.py", "app/api/incoming.py", "app/services/order_number_display.py", "scripts/benchmarks/erp3_read_baseline.py")}
    with ReadOnlySQLiteGuard(database) as guard:
        result.update(measure(args, database, guard))
        result["denied_operations"] = guard.denied
    result["database_sha256_after"] = file_sha256(database)
    result["database_unchanged"] = before == result["database_sha256_after"]
    result["passed"] = result["database_unchanged"] and result["denied_operations"] == 0 and all(c["status"] in {"passed", "permission_denied_expected"} for c in result["cases"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"passed": result["passed"], "cases": len(result["cases"]), "denied_operations": result["denied_operations"]}))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
