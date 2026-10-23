"""Owned run_task acceptance; never connects to a formal running database.

Invoke through scripts/uat/run_task.py with a documented sanitized source.
Evidence survives in the runner job; successful database copies are recycled.
The business amendment is exercised only inside that owned disposable copy.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
BASE, TARGET = "fa1023oc", "fb1023df"
NEW_TABLES = {"delivery_fulfillment_revisions", "delivery_fulfillment_segments"}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def file_digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def snapshot(path, *, audit_max_rowid=None):
    result = {"tables": {}, "schema": []}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        result["revision"] = db.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        result["schema"] = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name!='alembic_version' ORDER BY name"):
            quoted = '"' + table.replace('"', '""') + '"'
            columns = [row[1] for row in db.execute("PRAGMA table_info(" + quoted + ")")]
            where = " WHERE rowid<=?" if table == "operation_logs" and audit_max_rowid is not None else ""
            cursor = db.execute("SELECT * FROM " + quoted + where + " ORDER BY rowid", (audit_max_rowid,) if where else ())
            hasher, count = hashlib.sha256(), 0
            for row in cursor:
                hasher.update(digest(row).encode() + b"\n")
                count += 1
            result["tables"][table] = dict(count=count, columns=columns, sha256=hasher.hexdigest())
        result["integrity"] = db.execute("PRAGMA integrity_check").fetchall()
        result["foreign_key_errors"] = db.execute("PRAGMA foreign_key_check").fetchall()
    assert result["integrity"] == [("ok",)]
    assert not result["foreign_key_errors"]
    return result


def assert_preserved(before, after):
    assert all(after["tables"].get(name) == value for name, value in before["tables"].items()), "Original table facts changed"
    assert all(row in after["schema"] for row in before["schema"]), "Original schema object changed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-input", type=Path, required=True)
    parser.add_argument("--source-proof", type=Path, required=True)
    parser.add_argument("--source-kind", choices=("credential-sanitized-formal-copy", "synthetic-fixture"), required=True)
    args = parser.parse_args()
    from app.core.uat_isolation import validate_uat_environment
    layout = validate_uat_environment()
    root = Path(layout["root"]).resolve()
    path = Path(layout["paths"]["database"]).resolve()
    assert os.environ["ERP_ENVIRONMENT"] == "test" and root.name == "copies"
    assert root in path.parents and root.parent.name.startswith("task-")
    assert not root.is_symlink() and not path.is_symlink()
    output = root.parent / "delivery-fulfillment-acceptance.json"
    report = dict(status="running", source_kind=args.source_kind, formal_writes=False,
                  database_before_sha256=file_digest(path), evidence_input_sha256=file_digest(args.evidence_input),
                  source_proof_sha256=file_digest(args.source_proof), script_sha256=file_digest(Path(__file__)),
                  base=BASE, target=TARGET, artifacts_verified="database_and_owned_map_only", business_pii_anonymized=False,
                  signed_native_runtime_verified=False)
    try:
        from alembic import command
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        config = Config(str(PROJECT / "alembic.ini"))
        config.cmd_opts = SimpleNamespace(x=["expected_database_path=" + str(path)])
        assert ScriptDirectory.from_config(config).get_heads() == [TARGET]
        before = snapshot(path)
        assert before["revision"] == BASE and not NEW_TABLES.intersection(before["tables"])
        report["original_tables"] = len(before["tables"])
        report["original_table_fingerprints"] = before["tables"]
        report["original_schema_sha256"] = digest(before["schema"])
        map_path = Path(layout["paths"]["layout_runtime_file"])
        map_before = file_digest(map_path)
        report["roundtrip"] = []
        for operation, revision in ((command.upgrade, TARGET), (command.downgrade, BASE), (command.upgrade, TARGET)):
            operation(config, revision)
            current = snapshot(path)
            assert current["revision"] == revision
            assert_preserved(before, current)
            if revision == BASE:
                assert current == before
            else:
                assert set(current["tables"]) - set(before["tables"]) == NEW_TABLES
                assert all(current["tables"][name]["count"] == 0 for name in NEW_TABLES)
            report["roundtrip"].append(dict(revision=revision, database_sha256=file_digest(path), old_facts_preserved=True))

        # Qualify and append through the actual service in the owned copy; no
        # raw business SQL or authorization/session bypass is used.
        from sqlalchemy import select
        from sqlalchemy.orm import Session
        from app.core.database import create_sqlite_engine
        from app.models.user import User
        from app.services import delivery_fulfillment_facts as service
        from app.services.delivery_fulfillment_projection import actual_fulfillment_rows
        from app.services.material_cost_lineage import material_cost_coverage_report
        source = json.loads(args.evidence_input.read_text())
        delivery_id = int(source["delivery_id"])
        payload = service.FulfillmentRequest.model_validate(source["payload"])
        with closing(sqlite3.connect(path)) as db:
            audit_max = db.execute("SELECT COALESCE(max(rowid),0) FROM operation_logs").fetchone()[0]
        engine = create_sqlite_engine(path)
        with Session(engine) as db:
            actor = db.scalar(select(User).where(User.role == "admin", User.is_active.is_(True)).order_by(User.id))
            assert actor is not None, "No persisted active administrator in isolated source"
            preview = service.preview_delivery_fulfillment(db, actor, delivery_id, payload)
            request = service.FulfillmentSubmitRequest(**payload.model_dump(),
                expected_revision=preview["expected_revision"], reviewed_hash=preview["reviewed_hash"],
                operation_key="isolated-fulfillment-acceptance-" + root.parent.name)
            result = service.append_delivery_fulfillment(db, actor, delivery_id, request)
            db.commit()
            replay = service.append_delivery_fulfillment(db, actor, delivery_id, request)
            assert replay["replayed"] and replay["revision_id"] == result["revision_id"]
            db.commit()
            rows = actual_fulfillment_rows(db, delivery_ids=[delivery_id])
            expected = sorted((s.delivery_item_id, s.actual_date, s.quantity) for s in payload.segments)
            assert sorted((r["delivery_item_id"], r["actual_date"], r["quantity"]) for r in rows) == expected
            monthly, split_ids = defaultdict(int), {r["delivery_item_id"] for r in rows if r["cost_allocation_status"] == "pending_split_source_allocation"}
            for row in rows:
                monthly[row["actual_date"].strftime("%Y-%m")] += row["quantity"]
            for month in monthly:
                gaps, lines = [], []
                material_cost_coverage_report(db, month=month, visible_customer_ids={payload.customer_id}, _gap_collector=gaps, _line_collector=lines)
                for line in lines:
                    if line["delivery_item_id"] in split_ids:
                        assert not line["management_cost_complete"]
                        assert line["actual_material_cost"] == line["management_material_cost"] == 0
                assert not split_ids.intersection(g["item"].id for g in gaps)
            report["amendment"] = dict(delivery_id=delivery_id, revision_id=result["revision_id"],
                idempotent_replay=True, monthly_physical_quantities=dict(monthly), split_cost_pending_item_ids=sorted(split_ids),
                reviewed_hash=preview["reviewed_hash"])
        engine.dispose()
        after = snapshot(path, audit_max_rowid=audit_max)
        assert_preserved(before, after)
        with closing(sqlite3.connect(path)) as db:
            assert db.execute("SELECT count(*) FROM operation_logs WHERE rowid>?", (audit_max,)).fetchone() == (1,)
        populated_sha = file_digest(path)
        try:
            command.downgrade(config, BASE)
        except RuntimeError as error:
            assert "refuse lossy downgrade" in str(error)
        else:
            raise AssertionError("Populated facts allowed lossy downgrade")
        assert file_digest(path) == populated_sha
        assert file_digest(map_path) == map_before
        report.update(status="passed", populated_downgrade_refused=True, refused_database_bytes_unchanged=True,
                      original_facts_preserved_after_append=True, map_sha256=map_before,
                      database_final_sha256=populated_sha, final_revision=snapshot(path)["revision"])
    except BaseException as error:
        # Do not serialize source rows, credentials, full preview or SQL params.
        report.update(status="failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
        print(json.dumps({key: report[key] for key in ("status", "source_kind", "base", "target", "formal_writes")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
