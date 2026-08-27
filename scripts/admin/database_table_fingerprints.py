from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _normalized_cell(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    return value


def database_fingerprints(
    database: Path,
    *,
    excluded_tables: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    resolved = database.resolve()
    excluded = excluded_tables or set()
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        tables = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            )
            if str(row[0]) not in excluded
        ]
        result: dict[str, dict[str, Any]] = {}
        for table in tables:
            quoted_table = _quote_identifier(table)
            columns = list(connection.execute(f"PRAGMA table_info({quoted_table})"))
            column_names = [str(row[1]) for row in columns]
            primary_key_columns = [
                str(row[1])
                for row in sorted(columns, key=lambda item: int(item[5] or 0))
                if int(row[5] or 0) > 0
            ]
            order_by = (
                ", ".join(_quote_identifier(name) for name in primary_key_columns)
                if primary_key_columns
                else "rowid"
            )
            digest = hashlib.sha256()
            digest.update(
                json.dumps(column_names, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
            count = 0
            for row in connection.execute(
                f"SELECT * FROM {quoted_table} ORDER BY {order_by}"
            ):
                digest.update(b"\n")
                digest.update(
                    json.dumps(
                        [_normalized_cell(value) for value in row],
                        ensure_ascii=False,
                        separators=(",", ":"),
                        default=str,
                    ).encode("utf-8")
                )
                count += 1
            result[table] = {"rows": count, "sha256": digest.hexdigest()}
        return result
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only, deterministic SQLite table fingerprint comparison."
    )
    parser.add_argument("database", type=Path)
    parser.add_argument("--compare-to", type=Path)
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--assert-absent-table", action="append", default=[])
    parser.add_argument("--protect-order-number", action="append", default=[])
    args = parser.parse_args()

    excluded = {str(value) for value in args.exclude}
    source = database_fingerprints(args.database, excluded_tables=excluded)
    payload: dict[str, Any] = {
        "database": str(args.database.resolve()),
        "excluded_tables": sorted(excluded),
        "tables": source,
    }
    if args.compare_to is not None:
        target = database_fingerprints(args.compare_to, excluded_tables=excluded)
        names = sorted(set(source) | set(target))
        differences = {
            name: {"source": source.get(name), "target": target.get(name)}
            for name in names
            if source.get(name) != target.get(name)
        }
        payload.update(
            {
                "compare_to": str(args.compare_to.resolve()),
                "matching": not differences,
                "differences": differences,
                "source_table_count": len(source),
                "target_table_count": len(target),
            }
        )
        payload.pop("tables", None)
    if args.assert_absent_table or args.protect_order_number:
        resolved = args.database.resolve()
        connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
        try:
            connection.execute("PRAGMA query_only = ON")
            payload["absent_table_checks"] = {
                name: int(
                    connection.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
                        (name,),
                    ).fetchone()[0]
                )
                == 0
                for name in args.assert_absent_table
            }
            protected_numbers = sorted({str(value) for value in args.protect_order_number})
            placeholders = ",".join("?" for _ in protected_numbers)
            protected_orders = (
                connection.execute(
                    f"SELECT id, order_number FROM sales_orders WHERE order_number IN ({placeholders}) ORDER BY id",
                    protected_numbers,
                ).fetchall()
                if protected_numbers
                else []
            )
            protected_ids = [int(row[0]) for row in protected_orders]
            item_placeholders = ",".join("?" for _ in protected_ids)
            payload["protected_orders"] = [
                {"id": int(order_id), "order_number": str(order_number)}
                for order_id, order_number in protected_orders
            ]
            payload["protected_item_count"] = (
                int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM sales_order_items WHERE order_id IN ({item_placeholders})",
                        protected_ids,
                    ).fetchone()[0]
                )
                if protected_ids
                else 0
            )
            payload["revision"] = str(
                connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            )
            payload["integrity_check"] = str(
                connection.execute("PRAGMA integrity_check").fetchone()[0]
            )
            payload["foreign_key_violations"] = len(
                connection.execute("PRAGMA foreign_key_check").fetchall()
            )
        finally:
            connection.close()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
