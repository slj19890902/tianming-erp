"""Explicit, audited maintenance for the approved August/September scope.

Default is read-only preview. Apply requires the exact fresh preview hash,
an active administrator, a new verified backup, and a scoped reason.
Never creates orders, procurement prices, inventory or supplier payables.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from app.models.user import User
from app.services.material_cost_supplement import adopt, preview, canonical


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--database", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--expected-preview")
    p.add_argument("--actor-id", type=int)
    p.add_argument("--batch-id")
    p.add_argument("--reason")
    p.add_argument("--backup")
    args = p.parse_args()
    target = Path(args.database).resolve(strict=True)
    output = Path(args.output).resolve()
    if output == target or output.exists():
        raise SystemExit("Output must be a new separate report file")
    if args.apply and not all([args.expected_preview, args.actor_id, args.batch_id, args.reason, args.backup]):
        raise SystemExit("Apply requires preview, administrator, batch, reason and new backup")
    backup = Path(args.backup).resolve() if args.backup else None
    if backup and (backup == target or backup == output or backup.exists()):
        raise SystemExit("Backup must be a new independent path")

    def connect():
        c = sqlite3.connect(target.as_uri() + ("?mode=rw" if args.apply else "?mode=ro"), uri=True, timeout=30)
        c.execute("PRAGMA foreign_keys=ON")
        if not args.apply:
            c.execute("PRAGMA query_only=ON")
        return c

    engine = create_engine("sqlite+pysqlite://", creator=connect)
    with Session(engine) as db:
        db.execute(text("BEGIN IMMEDIATE" if args.apply else "BEGIN"))
        assert db.execute(text("PRAGMA integrity_check")).scalars().all() == ["ok"]
        assert db.execute(text("PRAGMA foreign_key_check")).all() == []
        assert db.execute(text("SELECT version_num FROM alembic_version")).scalars().all() == ["ru10v8x9z69"]
        months = ["2026-08", "2026-09"]
        if args.apply:
            # The write lock freezes all competing writers during backup and apply.
            with sqlite3.connect(target.as_uri()+"?mode=ro", uri=True) as source, sqlite3.connect(backup) as dest:
                source.backup(dest)
                assert dest.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
                assert dest.execute("PRAGMA foreign_key_check").fetchall() == []
            digest = hashlib.sha256()
            with backup.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            backup_sha = digest.hexdigest()
            # Compare every business table byte-for-byte inside the transaction.
            def business_fingerprints():
                tables = db.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")).scalars().all()
                hashes = {}
                for table in tables:
                    if table in {"finance_material_cost_supplements", "operation_logs"}:
                        continue
                    quoted = '"' + table.replace('"', '""') + '"'
                    digest = hashlib.sha256()
                    for row in db.execute(text(f"SELECT * FROM {quoted} ORDER BY rowid")):
                        digest.update(canonical(list(row)).encode("utf-8") + b"\n")
                    hashes[table] = digest.hexdigest()
                return hashes
            before = business_fingerprints()
            result = adopt(db, months=months, user=db.get(User, args.actor_id), expected_preview=args.expected_preview,
                           batch_id=args.batch_id, reason=args.reason)
            after = business_fingerprints()
            assert before == after, "Business facts changed outside supplement/audit tables"
            assert db.execute(text("PRAGMA foreign_key_check")).all() == []
            result.update(database=str(target), backup=str(backup), backup_sha256=backup_sha,
                          unchanged_business_tables=len(before), integrity="ok", foreign_key_violations=0)
            db.commit()
        else:
            result = preview(db, months)
            result["database"] = str(target)
            result["read_only"] = True
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(canonical({k: v for k,v in result.items() if k not in {"proposals", "missing"}}))
        if not args.apply:
            print(canonical(dict(proposals=len(result["proposals"]), missing=result["missing"])))


if __name__ == "__main__":
    main()
