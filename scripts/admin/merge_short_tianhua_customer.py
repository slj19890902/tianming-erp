from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from sqlalchemy import create_engine


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import load_settings
from app.core.database import backup_to_nas
from scripts.master_data_write_guard import reject_legacy_master_data_write_if_versioned


SOURCE_NAME = "天华"
TARGET_NAME = "苏州天华超净科技股份有限公司"
CUSTOMER_NAME_COLUMNS = {
    "customer_name",
    "detected_customer_name",
    "inferred_customer_name",
}


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _tables(connection: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def _customer_fk_columns(
    connection: sqlite3.Connection,
) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for table in _tables(connection):
        for foreign_key in connection.execute(
            f"PRAGMA foreign_key_list({_quote(table)})"
        ):
            if foreign_key[2] == "customers":
                result.append((table, foreign_key[3]))
    return result


def _customer_name_columns(
    connection: sqlite3.Connection,
) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for table in _tables(connection):
        if table == "customers":
            continue
        for column in connection.execute(f"PRAGMA table_info({_quote(table)})"):
            if column[1].lower() in CUSTOMER_NAME_COLUMNS:
                result.append((table, column[1]))
    return result


def _customer_row(
    connection: sqlite3.Connection,
    name: str,
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT id, customer_number, customer_code, name "
        "FROM customers WHERE name = ?",
        (name,),
    ).fetchone()


def analyze_merge(connection: sqlite3.Connection) -> dict:
    connection.row_factory = sqlite3.Row
    source = _customer_row(connection, SOURCE_NAME)
    target = _customer_row(connection, TARGET_NAME)
    if target is None:
        raise RuntimeError(f"目标正式客户不存在：{TARGET_NAME}")
    if source is None:
        return {
            "source_found": False,
            "target": dict(target),
            "foreign_key_updates": {},
            "customer_name_updates": {},
            "total_affected": 0,
        }

    foreign_key_updates: dict[str, int] = {}
    for table, column in _customer_fk_columns(connection):
        count = connection.execute(
            f"SELECT COUNT(*) FROM {_quote(table)} WHERE {_quote(column)} = ?",
            (source["id"],),
        ).fetchone()[0]
        if count:
            foreign_key_updates[f"{table}.{column}"] = int(count)

    customer_name_updates: dict[str, int] = {}
    for table, column in _customer_name_columns(connection):
        count = connection.execute(
            f"SELECT COUNT(*) FROM {_quote(table)} WHERE {_quote(column)} = ?",
            (SOURCE_NAME,),
        ).fetchone()[0]
        if count:
            customer_name_updates[f"{table}.{column}"] = int(count)

    return {
        "source_found": True,
        "source": dict(source),
        "target": dict(target),
        "foreign_key_updates": foreign_key_updates,
        "customer_name_updates": customer_name_updates,
        "total_affected": (
            sum(foreign_key_updates.values())
            + sum(customer_name_updates.values())
            + 1
        ),
    }


def run_merge(
    database: Path,
    *,
    apply: bool,
    backup_dir: Path | None = None,
    create_backup: bool = True,
) -> dict:
    database = database.resolve()
    if not database.is_file():
        raise FileNotFoundError(f"数据库不存在：{database}")

    if apply:
        guard_engine = create_engine(f"sqlite+pysqlite:///{database.as_posix()}")
        try:
            reject_legacy_master_data_write_if_versioned(
                guard_engine,
                script_name="scripts/admin/merge_short_tianhua_customer.py",
            )
        finally:
            guard_engine.dispose()

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        report = analyze_merge(connection)
    report["mode"] = "apply" if apply else "dry-run"
    report["database"] = str(database)
    if not apply or not report["source_found"]:
        report["changed"] = False
        return report

    backup = None
    if create_backup:
        backup = backup_to_nas(
            source_path=database,
            backup_dir=backup_dir,
            filename_suffix="_BEFORE_TIANHUA_CUSTOMER_MERGE",
            keep_regular=100,
        )

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        source = _customer_row(connection, SOURCE_NAME)
        target = _customer_row(connection, TARGET_NAME)
        if source is None:
            report["changed"] = False
            return report
        if target is None:
            raise RuntimeError(f"目标正式客户不存在：{TARGET_NAME}")
        try:
            connection.execute("BEGIN IMMEDIATE")
            for table, column in _customer_fk_columns(connection):
                connection.execute(
                    f"UPDATE {_quote(table)} SET {_quote(column)} = ? "
                    f"WHERE {_quote(column)} = ?",
                    (target["id"], source["id"]),
                )
            for table, column in _customer_name_columns(connection):
                connection.execute(
                    f"UPDATE {_quote(table)} SET {_quote(column)} = ? "
                    f"WHERE {_quote(column)} = ?",
                    (TARGET_NAME, SOURCE_NAME),
                )
            connection.execute("DELETE FROM customers WHERE id = ?", (source["id"],))
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"合并后外键检查失败：{violations[:5]}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    with sqlite3.connect(database) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        source_remaining = connection.execute(
            "SELECT COUNT(*) FROM customers WHERE name = ?",
            (SOURCE_NAME,),
        ).fetchone()[0]
        target_remaining = connection.execute(
            "SELECT COUNT(*) FROM customers WHERE name = ?",
            (TARGET_NAME,),
        ).fetchone()[0]
    if integrity.lower() != "ok" or source_remaining != 0 or target_remaining != 1:
        raise RuntimeError("合并后验证失败，请使用保护备份恢复")

    report.update(
        {
            "changed": True,
            "integrity_check": integrity,
            "source_remaining": source_remaining,
            "target_remaining": target_remaining,
            "backup": (
                {
                    "path": str(backup.path),
                    "sha256": backup.sha256,
                    "size": backup.size,
                    "integrity_check": backup.integrity_check,
                }
                if backup is not None
                else None
            ),
        }
    )
    return report


def main() -> None:
    settings = load_settings()
    parser = argparse.ArgumentParser(
        description="安全合并短名“天华”客户到正式天华超净客户"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--database", type=Path, default=settings.database_path)
    parser.add_argument("--backup-dir", type=Path, default=settings.backup_dir)
    args = parser.parse_args()
    result = run_merge(
        args.database,
        apply=args.apply,
        backup_dir=args.backup_dir,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
