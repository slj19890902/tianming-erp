from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


QUANTITY_TABLES = (
    "product_bom_components",
    "sales_order_item_bom_components",
    "requisition_item_bom_sources",
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create and inspect an isolated SQLite copy for N039 migration rehearsal."
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()

    source_path = args.source.resolve()
    target_path = args.target.resolve()
    if source_path == target_path:
        raise SystemExit("source and target must be different files")
    if not source_path.is_file():
        raise SystemExit(f"source database does not exist: {source_path}")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        raise SystemExit(f"target database already exists: {target_path}")

    source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(target_path)
    try:
        with target:
            source.backup(target)
    finally:
        source.close()
        target.close()

    copied = sqlite3.connect(target_path)
    try:
        version = copied.execute("SELECT version_num FROM alembic_version").fetchall()
        integrity = copied.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = copied.execute("PRAGMA foreign_key_check").fetchall()
        tables = {
            row[0]
            for row in copied.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        print(f"copy={target_path}")
        print(f"alembic={version}")
        print(f"integrity={integrity}")
        print(f"foreign_key_errors={len(foreign_keys)}")
        for table in QUANTITY_TABLES:
            if table not in tables:
                continue
            total = copied.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            invalid = copied.execute(
                f"SELECT count(*) FROM {table} "
                "WHERE quantity_per_set <= 0 "
                "OR quantity_per_set <> round(quantity_per_set, 0)"
            ).fetchone()[0]
            print(f"{table}: rows={total}, invalid_quantity_per_set={invalid}")
    finally:
        copied.close()


if __name__ == "__main__":
    main()
