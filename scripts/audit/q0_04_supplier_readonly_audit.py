"""Q0-04 / P0-08A supplier-reference read-only SQLite audit.

This command has no apply mode.  It requires an explicit SQLite database path,
opens the database through ``mode=ro``, enables ``PRAGMA query_only=ON`` and
installs an SQLite authorizer that rejects write/schema actions.  The only
files it writes are the explicitly requested JSON and Markdown reports.

The audit is intentionally schema-adaptive so an older household copy can be
used as preliminary evidence without pretending it is the current factory
database.  A factory release decision must use a fresh, isolated copy of the
factory database and rerun this same command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote


DEFAULT_TARGETS = ("佳丰",)
SQLITE_HEADER = b"SQLite format 3\x00"
MAX_DISTINCT_VALUES = 250
MAX_FOREIGN_KEY_ERRORS = 200

SUPPLIER_TEXT_COLUMNS = {
    "supplier",
    "supplier_name",
    "normalized_supplier_name",
    "snapshot_supplier_name",
    "selected_supplier_name_snapshot",
    "snapshot_component_supplier_name",
    "new_supplier",
    "old_supplier",
}
AUDIT_TEXT_COLUMNS = {
    "detail",
    "details",
    "detail_json",
    "details_json",
    "before_values",
    "after_values",
    "metadata_json",
    "description",
}
SUPPLIER_ID_COLUMNS = {"supplier_id"}
MATERIAL_ID_COLUMNS = {
    "material_id",
    "actual_material_id",
    "selected_material_id",
    "snapshot_component_material_id",
}

WRITE_AUTHORIZER_ACTIONS = {
    sqlite3.SQLITE_CREATE_INDEX,
    sqlite3.SQLITE_CREATE_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_INDEX,
    sqlite3.SQLITE_CREATE_TEMP_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
    sqlite3.SQLITE_CREATE_TEMP_VIEW,
    sqlite3.SQLITE_CREATE_TRIGGER,
    sqlite3.SQLITE_CREATE_VIEW,
    sqlite3.SQLITE_DELETE,
    sqlite3.SQLITE_DROP_INDEX,
    sqlite3.SQLITE_DROP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_INDEX,
    sqlite3.SQLITE_DROP_TEMP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_TRIGGER,
    sqlite3.SQLITE_DROP_TEMP_VIEW,
    sqlite3.SQLITE_DROP_TRIGGER,
    sqlite3.SQLITE_DROP_VIEW,
    sqlite3.SQLITE_INSERT,
    sqlite3.SQLITE_UPDATE,
    sqlite3.SQLITE_ALTER_TABLE,
    sqlite3.SQLITE_REINDEX,
    sqlite3.SQLITE_ANALYZE,
    sqlite3.SQLITE_ATTACH,
    sqlite3.SQLITE_DETACH,
}


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _is_symlink_path(path: Path) -> bool:
    candidate = path.absolute()
    return any(
        parent.exists() and parent.is_symlink()
        for parent in (candidate, *candidate.parents)
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _sidecar_state(database: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for suffix in ("-journal", "-wal", "-shm"):
        candidate = Path(str(database) + suffix)
        result[suffix] = {
            "exists": candidate.exists(),
            "size": candidate.stat().st_size if candidate.exists() else None,
            "mtime_ns": candidate.stat().st_mtime_ns if candidate.exists() else None,
        }
    return result


def _readonly_authorizer(
    action: int,
    _arg1: str | None,
    _arg2: str | None,
    _database_name: str | None,
    _trigger_name: str | None,
) -> int:
    if action in WRITE_AUTHORIZER_ACTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _connect_readonly(database: Path) -> sqlite3.Connection:
    if not database.is_file():
        raise ValueError(f"database must be an existing regular file: {database}")
    if _is_symlink_path(database):
        raise ValueError(f"symbolic-link database paths are refused: {database}")
    with database.open("rb") as handle:
        header = handle.read(len(SQLITE_HEADER))
    if header != SQLITE_HEADER:
        raise ValueError(f"file is not an SQLite 3 database: {database}")

    uri_path = quote(database.resolve().as_posix(), safe="/:")
    connection = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
        connection.close()
        raise RuntimeError("could not enable SQLite query_only mode")
    connection.set_authorizer(_readonly_authorizer)
    return connection


def _table_names(connection: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def _columns(
    connection: sqlite3.Connection, table: str
) -> dict[str, dict[str, Any]]:
    return {
        str(row[1]): {
            "type": str(row[2] or ""),
            "notnull": bool(row[3]),
            "primary_key": bool(row[5]),
        }
        for row in connection.execute(
            f"PRAGMA table_info({_quote_identifier(table)})"
        )
    }


def _foreign_keys(
    connection: sqlite3.Connection, table: str
) -> list[dict[str, str]]:
    return [
        {
            "from": str(row[3]),
            "to_table": str(row[2]),
            "to_column": str(row[4]),
            "on_update": str(row[5]),
            "on_delete": str(row[6]),
            "match": str(row[7]),
        }
        for row in connection.execute(
            f"PRAGMA foreign_key_list({_quote_identifier(table)})"
        )
    ]


def _category(table: str) -> str:
    lowered = table.casefold()
    if "incoming" in lowered or "receive" in lowered or "receipt" in lowered:
        return "来料/收料"
    if "inventory" in lowered or "stock" in lowered or "warehouse" in lowered:
        return "库存"
    if "requisition" in lowered or "purchase" in lowered:
        return "报料/采购"
    if "price" in lowered or "quote" in lowered or "quotation" in lowered:
        return "报价/价格"
    if "material" in lowered or "paper" in lowered:
        return "材质"
    if "supplier" in lowered:
        return "供应商主档/规则"
    if "order" in lowered or "legacy" in lowered:
        return "订单/历史"
    if "operation_log" in lowered or "audit" in lowered:
        return "审计"
    return "其他"


def _matches_target(value: Any, targets: Iterable[str]) -> bool:
    normalized = str(value or "").strip().casefold()
    return bool(normalized) and any(
        target.strip().casefold() in normalized
        for target in targets
        if target.strip()
    )


def _parse_legacy_note(note: str | None) -> dict[str, Any]:
    text = str(note or "")
    fields: dict[str, Any] = {}
    for key in ("legacy_supplier_id", "supplier_code", "short_name", "quote_uom"):
        match = re.search(rf"\b{key}=([^;]*)", text)
        if match and match.group(1).strip():
            value: Any = match.group(1).strip()
            if key == "legacy_supplier_id" and value.isdigit():
                value = int(value)
            fields[key] = value
    return fields


def _legacy_supplier_rows(
    connection: sqlite3.Connection,
    schema: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    if "suppliers" not in schema:
        return {
            "table_exists": False,
            "classification": "legacy_history_table_readonly",
            "label": "旧系统历史供应商表（只读保留）",
            "columns": [],
            "rows": [],
            "truncated": False,
        }

    available = schema["suppliers"]
    selected = [
        column
        for column in (
            "id",
            "standard_name",
            "name",
            "supplier_name",
            "display_name",
            "display_short_name",
            "short_name",
            "aliases",
            "alias",
            "name_aliases",
            "business_code",
            "supplier_code",
            "code",
            "is_active",
            "status",
            "payment_note",
        )
        if column in available
    ]
    if not selected:
        return {
            "table_exists": True,
            "classification": "legacy_history_table_readonly",
            "label": "旧系统历史供应商表（只读保留）",
            "columns": sorted(available),
            "rows": [],
            "truncated": False,
        }

    sql_columns = ", ".join(_quote_identifier(column) for column in selected)
    order = _quote_identifier("id") if "id" in selected else "rowid"
    raw_rows = connection.execute(
        f"SELECT {sql_columns} FROM suppliers ORDER BY {order} LIMIT 501"
    ).fetchall()
    truncated = len(raw_rows) > 500
    rows: list[dict[str, Any]] = []
    for raw in raw_rows[:500]:
        source = dict(raw)
        legacy = _parse_legacy_note(source.pop("payment_note", None))
        standard_name = next(
            (
                source.get(column)
                for column in ("standard_name", "name", "supplier_name")
                if source.get(column)
            ),
            None,
        )
        display_name = next(
            (
                source.get(column)
                for column in ("display_name", "display_short_name", "short_name")
                if source.get(column)
            ),
            legacy.get("short_name"),
        )
        business_code = next(
            (
                source.get(column)
                for column in ("business_code", "supplier_code", "code")
                if source.get(column)
            ),
            legacy.get("supplier_code"),
        )
        aliases: list[str] = []
        for column in ("aliases", "alias", "name_aliases"):
            raw_aliases = str(source.get(column) or "").strip()
            if raw_aliases:
                aliases.extend(
                    value.strip()
                    for value in re.split(r"[,，;；|\n]+", raw_aliases)
                    if value.strip()
                )
        if display_name and display_name != standard_name:
            aliases.append(str(display_name))
        rows.append(
            {
                "id": source.get("id"),
                "standard_name": standard_name,
                "display_name": display_name,
                "aliases": sorted(set(aliases)),
                "business_code": business_code,
                "legacy_supplier_id": legacy.get("legacy_supplier_id"),
                "quote_uom": legacy.get("quote_uom"),
                "is_active": source.get("is_active"),
                "status": source.get("status"),
            }
        )
    return {
        "table_exists": True,
        "classification": "legacy_history_table_readonly",
        "label": "旧系统历史供应商表（只读保留）",
        "columns": sorted(available),
        "rows": rows,
        "truncated": truncated,
    }


def _p0_08_supplier_master_state(
    connection: sqlite3.Connection,
    schema: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    records_table = "supplier_master_records"
    aliases_table = "supplier_master_aliases"
    records_exists = records_table in schema
    aliases_exists = aliases_table in schema
    if records_exists and aliases_exists:
        migration_status = "migrated"
        status_label = "P0-08 新主档结构已存在"
    elif not records_exists and not aliases_exists:
        migration_status = "not_migrated"
        status_label = "P0-08 未迁移"
    else:
        migration_status = "partial_schema"
        status_label = "P0-08 结构不完整"

    record_columns = sorted(schema.get(records_table, {}))
    alias_columns = sorted(schema.get(aliases_table, {}))
    rows: list[dict[str, Any]] = []
    if records_exists:
        selected = [
            column
            for column in (
                "id",
                "standard_name",
                "display_name",
                "business_code",
                "sort_order",
                "is_active",
                "version",
            )
            if column in schema[records_table]
        ]
        if selected:
            qcolumns = ", ".join(_quote_identifier(column) for column in selected)
            order = _quote_identifier("id") if "id" in selected else "rowid"
            raw_rows = connection.execute(
                f"SELECT {qcolumns} FROM {records_table} "
                f"ORDER BY {order} LIMIT 501"
            ).fetchall()
            for raw in raw_rows[:500]:
                rows.append(dict(raw))
            records_truncated = len(raw_rows) > 500
        else:
            records_truncated = False
    else:
        records_truncated = False

    aliases: list[dict[str, Any]] = []
    if aliases_exists:
        selected = [
            column
            for column in ("id", "supplier_id", "alias_name")
            if column in schema[aliases_table]
        ]
        if selected:
            qcolumns = ", ".join(_quote_identifier(column) for column in selected)
            order = _quote_identifier("id") if "id" in selected else "rowid"
            raw_aliases = connection.execute(
                f"SELECT {qcolumns} FROM {aliases_table} "
                f"ORDER BY {order} LIMIT 1001"
            ).fetchall()
            aliases = [dict(raw) for raw in raw_aliases[:1000]]
            aliases_truncated = len(raw_aliases) > 1000
        else:
            aliases_truncated = False
    else:
        aliases_truncated = False

    return {
        "records_table_exists": records_exists,
        "aliases_table_exists": aliases_exists,
        "migration_status": migration_status,
        "status_label": status_label,
        "record_columns": record_columns,
        "alias_columns": alias_columns,
        "record_count": (
            int(connection.execute(
                f"SELECT COUNT(*) FROM {records_table}"
            ).fetchone()[0])
            if records_exists
            else 0
        ),
        "alias_count": (
            int(connection.execute(
                f"SELECT COUNT(*) FROM {aliases_table}"
            ).fetchone()[0])
            if aliases_exists
            else 0
        ),
        "records": rows,
        "aliases": aliases,
        "records_truncated": records_truncated,
        "aliases_truncated": aliases_truncated,
    }


def _foreign_keys_referencing_table(
    connection: sqlite3.Connection,
    schema: dict[str, dict[str, dict[str, Any]]],
    target_table: str,
) -> list[dict[str, str]]:
    references: list[dict[str, str]] = []
    for table in sorted(schema):
        for row in _foreign_keys(connection, table):
            if row["to_table"] != target_table:
                continue
            references.append(
                {
                    "from_table": table,
                    "from_column": row["from"],
                    "to_table": target_table,
                    "to_column": row["to_column"],
                    "on_update": row["on_update"],
                    "on_delete": row["on_delete"],
                }
            )
    return references


def _is_supplier_text_column(table: str, column: str) -> bool:
    lowered = column.casefold()
    if table == "suppliers" and lowered in {
        "standard_name",
        "name",
        "supplier_name",
        "display_name",
        "display_short_name",
        "short_name",
        "aliases",
        "alias",
        "name_aliases",
    }:
        return True
    if table == "supplier_master_records" and lowered in {
        "standard_name",
        "display_name",
    }:
        return True
    if table == "supplier_master_aliases" and lowered == "alias_name":
        return True
    return lowered in SUPPLIER_TEXT_COLUMNS or lowered.endswith("_supplier_name")


def _supplier_name_distribution(
    connection: sqlite3.Connection,
    schema: dict[str, dict[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    distribution: list[dict[str, Any]] = []
    for table in sorted(schema):
        for column in sorted(schema[table]):
            if not _is_supplier_text_column(table, column):
                continue
            qtable = _quote_identifier(table)
            qcolumn = _quote_identifier(column)
            rows = connection.execute(
                f"SELECT CAST({qcolumn} AS TEXT) AS value, COUNT(*) AS row_count "
                f"FROM {qtable} "
                f"WHERE {qcolumn} IS NOT NULL "
                f"AND trim(CAST({qcolumn} AS TEXT)) <> '' "
                f"GROUP BY CAST({qcolumn} AS TEXT) "
                f"ORDER BY row_count DESC, value LIMIT ?",
                (MAX_DISTINCT_VALUES + 1,),
            ).fetchall()
            distribution.append(
                {
                    "category": _category(table),
                    "table": table,
                    "column": column,
                    "values": [
                        {"value": str(row[0]), "row_count": int(row[1])}
                        for row in rows[:MAX_DISTINCT_VALUES]
                    ],
                    "truncated": len(rows) > MAX_DISTINCT_VALUES,
                }
            )
    return distribution


def _matching_text_count(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    targets: list[str],
    *,
    emit_values: bool,
) -> tuple[int, list[dict[str, Any]]]:
    qtable = _quote_identifier(table)
    qcolumn = _quote_identifier(column)
    clauses = [
        f"instr(lower(CAST({qcolumn} AS TEXT)), lower(?)) > 0"
        for _ in targets
    ]
    if not clauses:
        return 0, []
    where = " OR ".join(clauses)
    count = int(
        connection.execute(
            f"SELECT COUNT(*) FROM {qtable} WHERE {where}",
            tuple(targets),
        ).fetchone()[0]
    )
    if not count or not emit_values:
        return count, []
    rows = connection.execute(
        f"SELECT CAST({qcolumn} AS TEXT) AS value, COUNT(*) AS row_count "
        f"FROM {qtable} WHERE {where} "
        f"GROUP BY CAST({qcolumn} AS TEXT) "
        f"ORDER BY row_count DESC, value LIMIT 50",
        tuple(targets),
    ).fetchall()
    return count, [
        {"value": str(row[0]), "row_count": int(row[1])}
        for row in rows
    ]


def _target_legacy_supplier_ids(
    legacy_supplier_table: dict[str, Any], targets: list[str]
) -> tuple[list[int], list[int]]:
    table_ids: set[int] = set()
    legacy_ids: set[int] = set()
    for row in legacy_supplier_table.get("rows", []):
        values = [
            row.get("standard_name"),
            row.get("display_name"),
            *(row.get("aliases") or []),
        ]
        if any(_matches_target(value, targets) for value in values):
            if isinstance(row.get("id"), int):
                table_ids.add(int(row["id"]))
            if isinstance(row.get("legacy_supplier_id"), int):
                legacy_ids.add(int(row["legacy_supplier_id"]))
    return sorted(table_ids), sorted(legacy_ids)


def _target_p0_08_supplier_ids(
    supplier_master: dict[str, Any], targets: list[str]
) -> list[int]:
    aliases_by_supplier: dict[int, list[str]] = defaultdict(list)
    for alias in supplier_master.get("aliases", []):
        supplier_id = alias.get("supplier_id")
        alias_name = alias.get("alias_name")
        if isinstance(supplier_id, int) and alias_name:
            aliases_by_supplier[supplier_id].append(str(alias_name))

    ids: set[int] = set()
    for row in supplier_master.get("records", []):
        row_id = row.get("id")
        values = [
            row.get("standard_name"),
            row.get("display_name"),
            *aliases_by_supplier.get(row_id, []),
        ]
        if isinstance(row_id, int) and any(
            _matches_target(value, targets) for value in values
        ):
            ids.add(row_id)
    return sorted(ids)


def _target_material_ids(
    connection: sqlite3.Connection,
    schema: dict[str, dict[str, dict[str, Any]]],
    targets: list[str],
) -> list[int]:
    if not {"id", "supplier_name"}.issubset(schema.get("materials", {})):
        return []
    clauses = [
        "instr(lower(CAST(supplier_name AS TEXT)), lower(?)) > 0"
        for _ in targets
    ]
    rows = connection.execute(
        "SELECT id FROM materials WHERE " + " OR ".join(clauses),
        tuple(targets),
    ).fetchall()
    return sorted({int(row[0]) for row in rows})


def _numeric_reference_count(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    ids: list[int],
) -> tuple[int, dict[str, int]]:
    if not ids:
        return 0, {}
    placeholders = ", ".join("?" for _ in ids)
    qtable = _quote_identifier(table)
    qcolumn = _quote_identifier(column)
    rows = connection.execute(
        f"SELECT {qcolumn}, COUNT(*) FROM {qtable} "
        f"WHERE {qcolumn} IN ({placeholders}) GROUP BY {qcolumn}",
        tuple(ids),
    ).fetchall()
    by_id = {str(row[0]): int(row[1]) for row in rows}
    return sum(by_id.values()), by_id


def _direct_references(
    connection: sqlite3.Connection,
    schema: dict[str, dict[str, dict[str, Any]]],
    targets: list[str],
    p0_08_master_ids: list[int],
    legacy_table_ids: list[int],
    legacy_external_ids: list[int],
    material_ids: list[int],
) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    for table in sorted(schema):
        foreign_keys = _foreign_keys(connection, table)
        fk_by_column = {
            row["from"]: (row["to_table"], row["to_column"])
            for row in foreign_keys
        }
        for column in sorted(schema[table]):
            lowered = column.casefold()
            if _is_supplier_text_column(table, column):
                count, values = _matching_text_count(
                    connection,
                    table,
                    column,
                    targets,
                    emit_values=True,
                )
                if count:
                    references.append(
                        {
                            "category": _category(table),
                            "table": table,
                            "column": column,
                            "kind": "supplier_text",
                            "row_count": count,
                            "matched_values": values,
                        }
                    )
                continue

            if lowered in AUDIT_TEXT_COLUMNS and (
                "operation_log" in table.casefold() or "audit" in table.casefold()
            ):
                count, _ = _matching_text_count(
                    connection,
                    table,
                    column,
                    targets,
                    emit_values=False,
                )
                if count:
                    references.append(
                        {
                            "category": "审计",
                            "table": table,
                            "column": column,
                            "kind": "audit_text_redacted",
                            "row_count": count,
                            "matched_values": [],
                        }
                    )
                continue

            if lowered in SUPPLIER_ID_COLUMNS:
                target_ids = list(p0_08_master_ids)
                id_namespace = "p0_08_supplier_master_id"
                fk_target = fk_by_column.get(column)
                if fk_target and fk_target[0] == "supplier_master_records":
                    target_ids = list(p0_08_master_ids)
                elif fk_target and fk_target[0] == "suppliers":
                    target_ids = list(legacy_table_ids)
                    id_namespace = "legacy_supplier_table_id"
                elif table.startswith("legacy_"):
                    target_ids = sorted(
                        set(legacy_external_ids or legacy_table_ids)
                    )
                    id_namespace = "legacy_external_supplier_id"
                else:
                    target_ids = sorted(
                        set(
                            p0_08_master_ids
                            + legacy_table_ids
                            + legacy_external_ids
                        )
                    )
                    id_namespace = "ambiguous_supplier_id"
                count, by_id = _numeric_reference_count(
                    connection, table, column, target_ids
                )
                if count:
                    references.append(
                        {
                            "category": _category(table),
                            "table": table,
                            "column": column,
                            "kind": id_namespace,
                            "row_count": count,
                            "matched_ids": by_id,
                        }
                    )
                continue

            is_material_fk = (
                fk_by_column.get(column, (None, None))[0] == "materials"
            )
            if lowered in MATERIAL_ID_COLUMNS or is_material_fk:
                count, by_id = _numeric_reference_count(
                    connection, table, column, material_ids
                )
                if count:
                    references.append(
                        {
                            "category": _category(table),
                            "table": table,
                            "column": column,
                            "kind": "material_linked_supplier",
                            "row_count": count,
                            "matched_ids": by_id,
                        }
                    )
    return references


def _has_columns(
    schema: dict[str, dict[str, dict[str, Any]]],
    table: str,
    required: Iterable[str],
) -> bool:
    return set(required).issubset(schema.get(table, {}))


def _relationship_references(
    connection: sqlite3.Connection,
    schema: dict[str, dict[str, dict[str, Any]]],
    targets: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Count known incoming/inventory references that inherit a supplier."""

    checks = [
        {
            "name": "incoming_by_requisition",
            "category": "来料/收料",
            "requirements": {
                "incoming_receipt_items": {"requisition_id"},
                "material_requisitions": {"id", "supplier_name"},
            },
            "sql": (
                "SELECT COUNT(*) FROM incoming_receipt_items child "
                "JOIN material_requisitions parent "
                "ON parent.id=child.requisition_id "
                "WHERE {target_where}"
            ),
            "target_expression": "parent.supplier_name",
        },
        {
            "name": "incoming_by_supplier_order",
            "category": "来料/收料",
            "requirements": {
                "incoming_receipt_items": {"supplier_order_id"},
                "supplier_requisition_orders": {"id", "supplier_name"},
            },
            "sql": (
                "SELECT COUNT(*) FROM incoming_receipt_items child "
                "JOIN supplier_requisition_orders parent "
                "ON parent.id=child.supplier_order_id "
                "WHERE {target_where}"
            ),
            "target_expression": "parent.supplier_name",
        },
        {
            "name": "inventory_movements_by_requisition",
            "category": "库存",
            "requirements": {
                "inventory_movements": {"related_requisition_id"},
                "material_requisitions": {"id", "supplier_name"},
            },
            "sql": (
                "SELECT COUNT(*) FROM inventory_movements child "
                "JOIN material_requisitions parent "
                "ON parent.id=child.related_requisition_id "
                "WHERE {target_where}"
            ),
            "target_expression": "parent.supplier_name",
        },
        {
            "name": "inventory_movements_by_supplier_order",
            "category": "库存",
            "requirements": {
                "inventory_movements": {"related_supplier_order_id"},
                "supplier_requisition_orders": {"id", "supplier_name"},
            },
            "sql": (
                "SELECT COUNT(*) FROM inventory_movements child "
                "JOIN supplier_requisition_orders parent "
                "ON parent.id=child.related_supplier_order_id "
                "WHERE {target_where}"
            ),
            "target_expression": "parent.supplier_name",
        },
    ]

    results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for check in checks:
        missing: list[str] = []
        for table, required in check["requirements"].items():
            if table not in schema:
                missing.append(f"{table} (table)")
                continue
            missing.extend(
                f"{table}.{column}"
                for column in sorted(required - set(schema[table]))
            )
        if missing:
            skipped.append({"name": check["name"], "missing": missing})
            continue
        clauses = [
            f"instr(lower(CAST({check['target_expression']} AS TEXT)), "
            "lower(?)) > 0"
            for _ in targets
        ]
        sql = check["sql"].format(target_where=" OR ".join(clauses))
        count = int(connection.execute(sql, tuple(targets)).fetchone()[0])
        results.append(
            {
                "name": check["name"],
                "category": check["category"],
                "row_count": count,
            }
        )
    return results, skipped


def _database_checks(connection: sqlite3.Connection, tables: list[str]) -> dict[str, Any]:
    alembic = (
        [
            str(row[0])
            for row in connection.execute(
                "SELECT version_num FROM alembic_version ORDER BY version_num"
            )
        ]
        if "alembic_version" in tables
        else []
    )
    integrity_rows = [
        str(row[0]) for row in connection.execute("PRAGMA integrity_check")
    ]
    fk_rows = [
        {
            "table": str(row[0]),
            "rowid": row[1],
            "parent": str(row[2]),
            "fkid": row[3],
        }
        for row in connection.execute("PRAGMA foreign_key_check").fetchall()
    ]
    return {
        "alembic_revisions": alembic,
        "integrity_check": integrity_rows[:100],
        "integrity_check_truncated": len(integrity_rows) > 100,
        "foreign_key_error_count": len(fk_rows),
        "foreign_key_errors": fk_rows[:MAX_FOREIGN_KEY_ERRORS],
        "foreign_key_errors_truncated": len(fk_rows) > MAX_FOREIGN_KEY_ERRORS,
    }


def audit_database(
    database: Path,
    *,
    source_label: str,
    targets: list[str],
    context_notes: list[str] | None = None,
) -> dict[str, Any]:
    database = database.resolve()
    stat_before = database.stat()
    sha_before = _sha256(database)
    sidecars_before = _sidecar_state(database)

    connection = _connect_readonly(database)
    try:
        tables = _table_names(connection)
        schema = {table: _columns(connection, table) for table in tables}
        legacy_supplier_table = _legacy_supplier_rows(connection, schema)
        legacy_supplier_table["preservation_rule"] = (
            "The suppliers table is a legacy history table. P0-08 must not "
            "overwrite, rename or clear it."
        )
        legacy_supplier_table["foreign_key_references"] = (
            _foreign_keys_referencing_table(connection, schema, "suppliers")
        )
        p0_08_supplier_master = _p0_08_supplier_master_state(
            connection, schema
        )
        legacy_table_ids, legacy_external_ids = _target_legacy_supplier_ids(
            legacy_supplier_table, targets
        )
        p0_08_master_ids = _target_p0_08_supplier_ids(
            p0_08_supplier_master, targets
        )
        material_ids = _target_material_ids(connection, schema, targets)
        direct_references = _direct_references(
            connection,
            schema,
            targets,
            p0_08_master_ids,
            legacy_table_ids,
            legacy_external_ids,
            material_ids,
        )
        relationship_references, skipped_relationships = _relationship_references(
            connection, schema, targets
        )
        result: dict[str, Any] = {
            "audit": {
                "task": "Q0-04 / P0-08A",
                "collected_at_utc": datetime.now(timezone.utc).isoformat(),
                "source_label": source_label,
                "context_notes": list(context_notes or []),
                "target_terms": targets,
                "read_only": True,
                "factory_currentness_confirmed": False,
                "warning": (
                    "This evidence does not confirm the 2026-07-30 factory formal "
                    "database. Rerun against a fresh isolated factory copy before "
                    "migration, supplier deactivation or release."
                ),
            },
            "database": {
                "path": str(database),
                "size": stat_before.st_size,
                "mtime_utc": datetime.fromtimestamp(
                    stat_before.st_mtime, tz=timezone.utc
                ).isoformat(),
                "sha256_before": sha_before,
                "sqlite_version": sqlite3.sqlite_version,
                "uri_mode": "ro",
                "query_only": bool(
                    connection.execute("PRAGMA query_only").fetchone()[0]
                ),
                "write_authorizer_installed": True,
                "sidecars_before": sidecars_before,
            },
            "checks": _database_checks(connection, tables),
            "schema": {
                "table_count": len(tables),
                "tables": tables,
                "supplier_relevant_columns": [
                    {
                        "table": table,
                        "columns": [
                            column
                            for column in sorted(schema[table])
                            if (
                                "supplier" in column.casefold()
                                or column.casefold() in MATERIAL_ID_COLUMNS
                                or _foreign_key_targets_materials(
                                    connection, table, column
                                )
                            )
                        ],
                    }
                    for table in sorted(schema)
                    if any(
                        (
                            "supplier" in column.casefold()
                            or column.casefold() in MATERIAL_ID_COLUMNS
                            or _foreign_key_targets_materials(
                                connection, table, column
                            )
                        )
                        for column in schema[table]
                    )
                ],
            },
            "legacy_supplier_table": legacy_supplier_table,
            "p0_08_supplier_master": p0_08_supplier_master,
            "supplier_name_distribution": _supplier_name_distribution(
                connection, schema
            ),
            "target_reference_summary": {
                "p0_08_supplier_master_ids": p0_08_master_ids,
                "legacy_supplier_table_ids": legacy_table_ids,
                "legacy_external_supplier_ids": legacy_external_ids,
                "material_ids": material_ids,
                "direct_references": direct_references,
                "relationship_references": relationship_references,
                "skipped_relationship_checks": skipped_relationships,
                "total_direct_rows": sum(
                    int(row["row_count"]) for row in direct_references
                ),
                "total_relationship_rows": sum(
                    int(row["row_count"]) for row in relationship_references
                ),
                "counting_note": (
                    "Counts are reference hits, not deduplicated business rows. "
                    "The same row can appear through more than one supplier or "
                    "material-linked column."
                ),
            },
        }
    finally:
        connection.close()

    stat_after = database.stat()
    sha_after = _sha256(database)
    sidecars_after = _sidecar_state(database)
    result["database"].update(
        {
            "sha256_after": sha_after,
            "sidecars_after": sidecars_after,
            "unchanged_during_audit": (
                sha_before == sha_after
                and stat_before.st_size == stat_after.st_size
                and stat_before.st_mtime_ns == stat_after.st_mtime_ns
                and sidecars_before == sidecars_after
            ),
        }
    )
    return result


def _foreign_key_targets_materials(
    connection: sqlite3.Connection, table: str, column: str
) -> bool:
    return any(
        row["from"] == column and row["to_table"] == "materials"
        for row in _foreign_keys(connection, table)
    )


def _markdown_table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    if not rows:
        return ["_无记录。_"]
    output = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    for row in rows:
        safe = [
            str(value if value is not None else "")
            .replace("|", "\\|")
            .replace("\r", " ")
            .replace("\n", " ")
            for value in row
        ]
        output.append("| " + " | ".join(safe) + " |")
    return output


def render_markdown(report: dict[str, Any]) -> str:
    database = report["database"]
    checks = report["checks"]
    legacy_supplier_table = report["legacy_supplier_table"]
    p0_08_supplier_master = report["p0_08_supplier_master"]
    summary = report["target_reference_summary"]
    lines = [
        "# Q0-04 / P0-08A 供应商只读审计",
        "",
        "> **证据边界：本报告不能证明 2026-07-30 工厂当前正式库。**",
        "> 正式迁移、停用佳丰或发布前，必须在工厂最新正式库的隔离副本上重跑同一脚本。",
        "",
        "## 数据源与只读门禁",
        "",
        f"- 来源标签：`{report['audit']['source_label']}`",
        f"- 数据库：`{database['path']}`",
        f"- 大小：`{database['size']}` 字节",
        f"- 修改时间（UTC）：`{database['mtime_utc']}`",
        f"- SHA-256：`{database['sha256_before']}`",
        f"- SQLite：`{database['sqlite_version']}`",
        f"- 连接：URI `mode={database['uri_mode']}`",
        f"- `query_only`：`{database['query_only']}`",
        f"- 写操作 authorizer：`{database['write_authorizer_installed']}`",
        f"- 审计前后数据库及 sidecar 未变化：`{database['unchanged_during_audit']}`",
        "",
        "## 数据库检查",
        "",
        f"- Alembic：`{', '.join(checks['alembic_revisions']) or '未发现'}`",
        f"- `integrity_check`：`{', '.join(checks['integrity_check'])}`",
        f"- 外键异常：`{checks['foreign_key_error_count']}`",
        f"- 表数量：`{report['schema']['table_count']}`",
        "",
        "## P0-08 新供应商主档迁移状态",
        "",
        f"- 状态：**{p0_08_supplier_master['status_label']}**",
        "- `supplier_master_records`："
        f"`{p0_08_supplier_master['records_table_exists']}`",
        "- `supplier_master_aliases`："
        f"`{p0_08_supplier_master['aliases_table_exists']}`",
        f"- 新主档记录：`{p0_08_supplier_master['record_count']}`",
        f"- 新主档别名：`{p0_08_supplier_master['alias_count']}`",
        "",
    ]
    lines.extend(
        _markdown_table(
            ["ID", "标准名", "显示简称", "业务代码", "排序", "启用", "版本"],
            [
                [
                    row.get("id"),
                    row.get("standard_name"),
                    row.get("display_name"),
                    row.get("business_code"),
                    row.get("sort_order"),
                    row.get("is_active"),
                    row.get("version"),
                ]
                for row in p0_08_supplier_master["records"]
            ],
        )
    )
    lines.extend(
        [
            "",
            "## 旧系统历史供应商表（只读保留）",
            "",
            "> `suppliers` 是旧系统历史供应商表，不是 P0-08 新供应商主档。"
            "新迁移必须新建独立表，禁止覆盖、重命名或清空该旧表。",
            "",
            f"- `suppliers` 表存在：`{legacy_supplier_table['table_exists']}`",
            f"- 字段：`{', '.join(legacy_supplier_table['columns'])}`",
            "- 指向该旧表的外键数量："
            f"`{len(legacy_supplier_table['foreign_key_references'])}`",
            "",
        ]
    )
    lines.extend(
        _markdown_table(
            ["ID", "标准名", "显示名/简称", "别名", "业务代码", "旧系统ID", "启用"],
            [
                [
                    row.get("id"),
                    row.get("standard_name"),
                    row.get("display_name"),
                    "、".join(row.get("aliases") or []),
                    row.get("business_code"),
                    row.get("legacy_supplier_id"),
                    row.get("is_active")
                    if row.get("is_active") is not None
                    else row.get("status"),
                ]
                for row in legacy_supplier_table["rows"]
            ],
        )
    )
    lines.extend(["", "### 指向旧 `suppliers` 表的外键", ""])
    lines.extend(
        _markdown_table(
            ["引用表", "引用字段", "目标字段", "ON UPDATE", "ON DELETE"],
            [
                [
                    row["from_table"],
                    row["from_column"],
                    row["to_column"],
                    row["on_update"],
                    row["on_delete"],
                ]
                for row in legacy_supplier_table["foreign_key_references"]
            ],
        )
    )
    lines.extend(
        [
            "",
            "## 数据库中可识别的供应商名称",
            "",
        ]
    )
    distribution_rows: list[list[Any]] = []
    for item in report["supplier_name_distribution"]:
        for value in item["values"]:
            distribution_rows.append(
                [
                    item["category"],
                    item["table"],
                    item["column"],
                    value["value"],
                    value["row_count"],
                ]
            )
    lines.extend(
        _markdown_table(
            ["类别", "表", "字段", "名称/别名值", "行数"],
            distribution_rows,
        )
    )
    lines.extend(
        [
            "",
            f"## 目标供应商引用：{'、'.join(report['audit']['target_terms'])}",
            "",
            "- 匹配的 P0-08 新供应商主档 ID："
            f"`{summary['p0_08_supplier_master_ids']}`",
            "- 匹配的旧 `suppliers` 表 ID："
            f"`{summary['legacy_supplier_table_ids']}`",
            "- 匹配的旧来源系统供应商 ID："
            f"`{summary['legacy_external_supplier_ids']}`",
            f"- 匹配的材质 ID：`{summary['material_ids']}`",
            f"- 直接/材质关联命中合计（非去重）：`{summary['total_direct_rows']}`",
            f"- 已知关系链命中合计（非去重）：`{summary['total_relationship_rows']}`",
            "- 计数说明：同一业务行可能通过供应商文本和材质关联等多个字段重复命中；"
            "正式判断应查看下方逐表逐字段结果。",
            "",
        ]
    )
    reference_rows: list[list[Any]] = []
    for row in summary["direct_references"]:
        matched = row.get("matched_values") or row.get("matched_ids") or ""
        reference_rows.append(
            [
                row["category"],
                row["table"],
                row["column"],
                row["kind"],
                row["row_count"],
                json.dumps(matched, ensure_ascii=False),
            ]
        )
    lines.extend(
        _markdown_table(
            ["类别", "表", "字段", "依据", "行数", "匹配值/ID"],
            reference_rows,
        )
    )
    lines.extend(["", "### 已知关系链检查", ""])
    lines.extend(
        _markdown_table(
            ["类别", "检查", "行数"],
            [
                [row["category"], row["name"], row["row_count"]]
                for row in summary["relationship_references"]
            ],
        )
    )
    lines.extend(["", "### 因旧副本缺表/缺字段而跳过", ""])
    lines.extend(
        _markdown_table(
            ["检查", "缺少结构"],
            [
                [row["name"], "、".join(row["missing"])]
                for row in summary["skipped_relationship_checks"]
            ],
        )
    )
    lines.extend(
        [
            "",
            "## 结论边界",
            "",
            "- 该结果仅用于 P0-08A 家庭预审和完善工厂重跑清单。",
            "- 不能据此在正式库停用、删除、改名或替换任何供应商。",
            "- 不能把佳丰的材质、价格、报料、来料或库存自动转给胜源/森林阳光。",
            "- `suppliers` 是仍被历史业务外键引用的旧表；P0-08 新迁移只能新建"
            "`supplier_master_records` / `supplier_master_aliases`，不得覆盖旧表。",
            "- 工厂端须对最新正式库先制作隔离副本，再用相同命令重跑并保存新的 SHA-256、"
            "Alembic、完整性、外键和引用报告。",
            "",
            "## 工厂隔离副本重跑示例",
            "",
            "```powershell",
            "python scripts\\audit\\q0_04_supplier_readonly_audit.py `",
            '  --database "<工厂最新正式库的隔离副本.sqlite3>" `',
            '  --source-label "工厂最新正式库隔离副本；填写复制时间和正式版本 SHA" `',
            "  --json-output "
            "docs\\migration_reports\\Q0-04_supplier_audit_factory_copy.json `",
            "  --markdown-output "
            "docs\\migration_reports\\Q0-04_supplier_audit_factory_copy.md",
            "```",
            "",
        ]
    )
    if report["audit"].get("context_notes"):
        lines.extend(["## 辅助附注（不属于主证据）", ""])
        lines.extend(
            f"- {note}" for note in report["audit"]["context_notes"]
        )
        lines.append("")
    return "\n".join(lines)


def _validated_output(path: Path, database: Path) -> Path:
    resolved = path.resolve()
    if resolved == database.resolve():
        raise ValueError("report output must not overwrite the database")
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only supplier/reference audit for Q0-04 / P0-08A. "
            "There is no apply mode."
        )
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--source-label", required=True)
    parser.add_argument(
        "--target",
        action="append",
        default=None,
        help="Supplier term to audit; repeatable. Default: 佳丰",
    )
    parser.add_argument(
        "--context-note",
        action="append",
        default=None,
        help="Optional report note; repeatable. It is not counted as audit evidence.",
    )
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    args = parser.parse_args()

    database = args.database.resolve()
    json_output = _validated_output(args.json_output, database)
    markdown_output = _validated_output(args.markdown_output, database)
    if json_output == markdown_output:
        parser.error("--json-output and --markdown-output must be different files")

    targets = [
        value.strip()
        for value in (args.target or list(DEFAULT_TARGETS))
        if value.strip()
    ]
    if not targets:
        parser.error("at least one non-empty --target is required")

    report = audit_database(
        database,
        source_label=args.source_label.strip(),
        targets=targets,
        context_notes=[
            note.strip()
            for note in (args.context_note or [])
            if note.strip()
        ],
    )
    json_output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_output.write_text(render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "database": str(database),
                "read_only": True,
                "unchanged_during_audit": report["database"][
                    "unchanged_during_audit"
                ],
                "json_output": str(json_output),
                "markdown_output": str(markdown_output),
                "warning": report["audit"]["warning"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
