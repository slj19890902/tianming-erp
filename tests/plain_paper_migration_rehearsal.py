"""Explicit read-only source -> isolated copy; never run migrations on source."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3


def digest_tables(path):
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        result = {}
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            if name == "alembic_version":
                continue
            digest = hashlib.sha256()
            count = 0
            for row in db.execute('SELECT * FROM "' + name.replace('"', '""') + '" ORDER BY rowid'):
                digest.update(repr(row).encode("utf-8"))
                count += 1
            result[name] = (count, digest.hexdigest())
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--copy", type=Path, required=True)
    args = parser.parse_args()
    source, target = args.source.resolve(strict=True), args.copy.resolve()
    if source == target or target.exists() or "plain-paper-isolated" not in target.name:
        raise RuntimeError("需要全新的明确隔离副本路径")
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    os.environ["ERP_DATABASE_PATH"] = str(target)
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    cfg = Config("alembic.ini")
    cfg.cmd_opts = argparse.Namespace(x=["expected_database_path=" + str(target)])
    assert ScriptDirectory.from_config(cfg).get_heads() == ["mr0914"]
    before = digest_tables(target)
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchall() == [("mq0912",)]
        before_schema = db.execute("SELECT type,name,sql FROM sqlite_master WHERE tbl_name='semi_finished_inventory_details' AND type IN ('index','trigger') ORDER BY type,name").fetchall()
    for revision in ("mr0914", "mq0912", "mr0914"):
        (command.downgrade if revision == "mq0912" else command.upgrade)(cfg, revision)
        assert digest_tables(target) == before, "迁移改变了业务数据"
        with sqlite3.connect(target) as db:
            assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert not db.execute("PRAGMA foreign_key_check").fetchall()
            assert db.execute("SELECT type,name,sql FROM sqlite_master WHERE tbl_name='semi_finished_inventory_details' AND type IN ('index','trigger') ORDER BY type,name").fetchall() == before_schema
    print(json.dumps(dict(result="passed", copy=str(target), tables=len(before),
        business_rows=sum(row[0] for row in before.values()), source_open_mode="ro", head="mr0914",
        checks=["upgrade", "downgrade", "reupgrade", "all_business_table_hashes", "indexes_triggers", "integrity", "foreign_keys"])))


if __name__ == "__main__":
    main()
