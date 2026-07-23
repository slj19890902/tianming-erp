"""N081 Phase 0 read-only inventory readiness export.

The command has no apply mode. It requires an explicit SQLite path, opens it
with ``mode=ro`` plus ``PRAGMA query_only=ON``, and writes review files only to
the requested output directory.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote


REQUIRED_COLUMNS = {
    "warehouse_locations": {
        "id",
        "location_code",
        "location_name",
        "warehouse_type",
        "is_active",
        "warehouse_floor",
        "area_code",
        "storage_type",
        "is_temporary",
        "placement_status",
    },
    "inventory_lots": {
        "id",
        "lot_number",
        "inventory_type",
        "warehouse_location_id",
        "quantity_available",
        "quantity_reserved",
        "unit",
        "status",
        "source_type",
        "stock_date",
    },
    "inventory_pallets": {
        "id",
        "pallet_code",
        "location_id",
        "status",
        "is_current",
        "needs_relocation",
    },
    "inventory_pallet_items": {
        "id",
        "pallet_id",
        "inventory_lot_id",
        "customer_id",
        "product_id",
        "inventory_code",
        "item_type",
        "quantity",
        "unit",
        "match_status",
    },
    "finished_goods_inventory_details": {
        "inventory_lot_id",
        "owner_customer_id",
        "is_general",
        "product_id",
        "inventory_code_snapshot",
    },
    "semi_finished_inventory_details": {
        "inventory_lot_id",
        "owner_customer_id",
        "material_code_snapshot",
        "sheet_type",
    },
}

ISSUE_COLUMNS = (
    "issue",
    "entity_type",
    "entity_id",
    "location_id",
    "location_code",
    "pallet_id",
    "pallet_code",
    "lot_id",
    "lot_number",
    "inventory_type",
    "item_type",
    "match_status",
    "quantity",
    "unit",
    "details",
)

LOCATION_COLUMNS = (
    "location_id",
    "location_code",
    "location_name",
    "warehouse_type",
    "is_active",
    "warehouse_floor",
    "area_code",
    "storage_type",
    "is_temporary",
    "placement_status",
    "has_floor3_layout",
    "current_pallet_id",
    "current_pallet_code",
    "active_lot_count",
)


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


def _connect_readonly(database: Path) -> sqlite3.Connection:
    if not database.is_file():
        raise ValueError(f"database must be an existing regular file: {database}")
    if _is_symlink_path(database):
        raise ValueError(f"symbolic-link database paths are refused: {database}")
    uri_path = str(database.resolve()).replace("\\", "/")
    connection = sqlite3.connect(
        f"file:{quote(uri_path, safe='/:')}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
        connection.close()
        raise RuntimeError("could not enable SQLite query_only mode")
    return connection


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    }


def _require_schema(connection: sqlite3.Connection) -> set[str]:
    tables = _table_names(connection)
    missing_tables = sorted(set(REQUIRED_COLUMNS) - tables)
    if missing_tables:
        raise ValueError(f"database is missing tables: {', '.join(missing_tables)}")
    missing_columns: list[str] = []
    for table, required in REQUIRED_COLUMNS.items():
        missing = sorted(required - _columns(connection, table))
        missing_columns.extend(f"{table}.{column}" for column in missing)
    if missing_columns:
        raise ValueError(
            f"database is missing columns: {', '.join(missing_columns)}"
        )
    return tables


def _scalar(connection: sqlite3.Connection, sql: str) -> int:
    return int(connection.execute(sql).fetchone()[0])


def _group_counts(
    connection: sqlite3.Connection, sql: str
) -> dict[str, int]:
    return {
        str(row[0] if row[0] is not None else "(空)"): int(row[1])
        for row in connection.execute(sql)
    }


def _issue(
    issue: str,
    *,
    entity_type: str,
    entity_id: int,
    details: str,
    **values: Any,
) -> dict[str, Any]:
    row = {column: "" for column in ISSUE_COLUMNS}
    row.update(
        issue=issue,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details,
        **values,
    )
    return row


def _location_rows(
    connection: sqlite3.Connection, tables: set[str]
) -> list[dict[str, Any]]:
    layout_select = (
        "CASE WHEN f.location_id IS NULL THEN 0 ELSE 1 END"
        if "floor3_location_layouts" in tables
        else "0"
    )
    layout_join = (
        "LEFT JOIN floor3_location_layouts f ON f.location_id = l.id"
        if "floor3_location_layouts" in tables
        else ""
    )
    rows = connection.execute(
        f"""
        SELECT
            l.id AS location_id,
            l.location_code,
            l.location_name,
            l.warehouse_type,
            l.is_active,
            l.warehouse_floor,
            l.area_code,
            l.storage_type,
            l.is_temporary,
            l.placement_status,
            {layout_select} AS has_floor3_layout,
            p.id AS current_pallet_id,
            p.pallet_code AS current_pallet_code,
            COUNT(DISTINCT CASE WHEN lot.status='active' THEN lot.id END)
                AS active_lot_count
        FROM warehouse_locations l
        {layout_join}
        LEFT JOIN inventory_pallets p
          ON p.location_id=l.id AND p.is_current=1
        LEFT JOIN inventory_lots lot
          ON lot.warehouse_location_id=l.id
        GROUP BY
            l.id, l.location_code, l.location_name, l.warehouse_type,
            l.is_active, l.warehouse_floor, l.area_code, l.storage_type,
            l.is_temporary, l.placement_status, has_floor3_layout, p.id, p.pallet_code
        ORDER BY l.sort_order, l.location_code, l.id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _issues(
    connection: sqlite3.Connection, locations: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for row in locations:
        if not row["is_active"]:
            continue
        if row["placement_status"] == "unplaced":
            issues.append(
                _issue(
                    "location_unplaced",
                    entity_type="location",
                    entity_id=int(row["location_id"]),
                    location_id=row["location_id"],
                    location_code=row["location_code"],
                    details="库位已明确标记为未放置，不能入库或发起盘点",
                )
            )
            continue
        missing = [
            label
            for label, value in (
                ("楼层", row["warehouse_floor"]),
                ("区域", row["area_code"]),
                ("存储类型", row["storage_type"]),
            )
            if value in (None, "")
        ]
        if missing:
            issues.append(
                _issue(
                    "location_master_data_incomplete",
                    entity_type="location",
                    entity_id=int(row["location_id"]),
                    location_id=row["location_id"],
                    location_code=row["location_code"],
                    details=f"缺少：{'、'.join(missing)}",
                )
            )
        elif (
            int(row["warehouse_floor"] or 0) == 3
            and not bool(row["has_floor3_layout"])
        ):
            issues.append(
                _issue(
                    "location_master_data_incomplete",
                    entity_type="location",
                    entity_id=int(row["location_id"]),
                    location_id=row["location_id"],
                    location_code=row["location_code"],
                    details="三楼库位缺少平面图放置记录",
                )
            )

    for row in connection.execute(
        """
        SELECT
            lot.id AS lot_id,
            lot.lot_number,
            lot.inventory_type,
            lot.warehouse_location_id AS location_id,
            loc.location_code,
            lot.quantity_available AS quantity,
            lot.unit,
            pallet.id AS pallet_id,
            pallet.pallet_code,
            finished.inventory_lot_id AS finished_detail_id,
            semi.inventory_lot_id AS semi_detail_id,
            loc.is_active AS location_active
            , loc.placement_status AS location_placement_status
        FROM inventory_lots lot
        LEFT JOIN warehouse_locations loc ON loc.id=lot.warehouse_location_id
        LEFT JOIN inventory_pallet_items item ON item.inventory_lot_id=lot.id
        LEFT JOIN inventory_pallets pallet ON pallet.id=item.pallet_id
        LEFT JOIN finished_goods_inventory_details finished
          ON finished.inventory_lot_id=lot.id
        LEFT JOIN semi_finished_inventory_details semi
          ON semi.inventory_lot_id=lot.id
        WHERE lot.status='active'
        ORDER BY lot.id
        """
    ):
        common = {
            "location_id": row["location_id"],
            "location_code": row["location_code"] or "",
            "pallet_id": row["pallet_id"] or "",
            "pallet_code": row["pallet_code"] or "",
            "lot_id": row["lot_id"],
            "lot_number": row["lot_number"],
            "inventory_type": row["inventory_type"],
            "quantity": row["quantity"],
            "unit": row["unit"],
        }
        expected_detail = (
            row["finished_detail_id"]
            if row["inventory_type"] == "finished"
            else row["semi_detail_id"]
        )
        if expected_detail is None:
            issues.append(
                _issue(
                    "active_lot_missing_type_detail",
                    entity_type="lot",
                    entity_id=int(row["lot_id"]),
                    details="正式批次缺少对应成品/半成品明细",
                    **common,
                )
            )
        if row["location_placement_status"] != "placed":
            issues.append(
                _issue(
                    "active_lot_in_unplaced_location",
                    entity_type="lot",
                    entity_id=int(row["lot_id"]),
                    details="正式库存位于未放置库位，必须先停止并修正空间主数据",
                    **common,
                )
            )
        if row["pallet_id"] is None:
            issues.append(
                _issue(
                    "active_lot_without_pallet",
                    entity_type="lot",
                    entity_id=int(row["lot_id"]),
                    details="正式批次尚未绑定栈板",
                    **common,
                )
            )
        if row["location_active"] in (None, 0):
            issues.append(
                _issue(
                    "active_lot_on_missing_or_inactive_location",
                    entity_type="lot",
                    entity_id=int(row["lot_id"]),
                    details="正式批次所在库位不存在或已停用",
                    **common,
                )
            )

    for row in connection.execute(
        """
        SELECT
            item.id AS item_id,
            item.pallet_id,
            pallet.pallet_code,
            pallet.location_id,
            loc.location_code,
            item.inventory_lot_id AS lot_id,
            lot.lot_number,
            item.item_type,
            item.match_status,
            item.quantity,
            item.unit,
            item.customer_id,
            item.product_id
        FROM inventory_pallet_items item
        JOIN inventory_pallets pallet ON pallet.id=item.pallet_id
        LEFT JOIN warehouse_locations loc ON loc.id=pallet.location_id
        LEFT JOIN inventory_lots lot ON lot.id=item.inventory_lot_id
        WHERE item.inventory_lot_id IS NULL
           OR item.match_status <> 'matched'
           OR item.customer_id IS NULL
           OR (item.item_type='finished' AND item.product_id IS NULL)
        ORDER BY item.id
        """
    ):
        reasons: list[str] = []
        if row["lot_id"] is None:
            reasons.append("未转为正式库存")
        if row["match_status"] != "matched":
            reasons.append("匹配未完成")
        if row["customer_id"] is None:
            reasons.append("客户未匹配")
        if row["item_type"] == "finished" and row["product_id"] is None:
            reasons.append("产品未匹配")
        issues.append(
            _issue(
                "pallet_snapshot_requires_review",
                entity_type="pallet_item",
                entity_id=int(row["item_id"]),
                location_id=row["location_id"] or "",
                location_code=row["location_code"] or "",
                pallet_id=row["pallet_id"],
                pallet_code=row["pallet_code"],
                lot_id=row["lot_id"] or "",
                lot_number=row["lot_number"] or "",
                inventory_type="",
                item_type=row["item_type"],
                match_status=row["match_status"],
                quantity=row["quantity"],
                unit=row["unit"],
                details="；".join(reasons),
            )
        )

    for row in connection.execute(
        """
        SELECT id, pallet_code, location_id, needs_relocation
        FROM inventory_pallets
        WHERE is_current=1 AND (location_id IS NULL OR needs_relocation=1)
        ORDER BY id
        """
    ):
        issues.append(
            _issue(
                "current_pallet_location_requires_review",
                entity_type="pallet",
                entity_id=int(row["id"]),
                location_id=row["location_id"] or "",
                pallet_id=row["id"],
                pallet_code=row["pallet_code"],
                details=(
                    "当前栈板缺少库位"
                    if row["location_id"] is None
                    else "当前栈板标记为需要重新定位"
                ),
            )
        )

    issues.sort(
        key=lambda row: (
            str(row["issue"]),
            str(row["entity_type"]),
            int(row["entity_id"]),
        )
    )
    return issues


def audit_database(database: str | Path) -> dict[str, Any]:
    database_path = Path(database).resolve()
    before_hash = _sha256(database_path)
    before_size = database_path.stat().st_size
    started_at = datetime.now(timezone.utc).isoformat()
    connection = _connect_readonly(database_path)
    try:
        tables = _require_schema(connection)
        connection.execute("BEGIN")
        locations = _location_rows(connection, tables)
        issues = _issues(connection, locations)
        counts = {
            table: _scalar(connection, f'SELECT COUNT(*) FROM "{table}"')
            for table in REQUIRED_COLUMNS
        }
        summary = {
            "table_counts": dict(sorted(counts.items())),
            "location_active_counts": _group_counts(
                connection,
                "SELECT CASE WHEN is_active=1 THEN 'active' ELSE 'inactive' END, "
                "COUNT(*) FROM warehouse_locations GROUP BY is_active ORDER BY is_active DESC",
            ),
            "locations_by_floor": _group_counts(
                connection,
                "SELECT warehouse_floor, COUNT(*) FROM warehouse_locations "
                "GROUP BY warehouse_floor ORDER BY warehouse_floor",
            ),
            "locations_by_placement": _group_counts(
                connection,
                "SELECT placement_status, COUNT(*) FROM warehouse_locations "
                "GROUP BY placement_status ORDER BY placement_status",
            ),
            "lots_by_type": _group_counts(
                connection,
                "SELECT inventory_type, COUNT(*) FROM inventory_lots "
                "GROUP BY inventory_type ORDER BY inventory_type",
            ),
            "lots_by_status": _group_counts(
                connection,
                "SELECT status, COUNT(*) FROM inventory_lots "
                "GROUP BY status ORDER BY status",
            ),
            "lots_by_source": _group_counts(
                connection,
                "SELECT source_type, COUNT(*) FROM inventory_lots "
                "GROUP BY source_type ORDER BY source_type",
            ),
            "pallet_items_by_match_status": _group_counts(
                connection,
                "SELECT match_status, COUNT(*) FROM inventory_pallet_items "
                "GROUP BY match_status ORDER BY match_status",
            ),
            "issue_counts": dict(
                sorted(Counter(str(row["issue"]) for row in issues).items())
            ),
            "issue_rows": len(issues),
        }
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
        quick_check = str(
            connection.execute("PRAGMA quick_check").fetchone()[0]
        )
        foreign_key_errors = len(
            connection.execute("PRAGMA foreign_key_check").fetchall()
        )
        connection.rollback()
    finally:
        connection.close()

    after_hash = _sha256(database_path)
    after_size = database_path.stat().st_size
    return {
        "audit": {
            "started_at_utc": started_at,
            "database_path": str(database_path),
            "database_size_before": before_size,
            "database_size_after": after_size,
            "database_sha256_before": before_hash,
            "database_sha256_after": after_hash,
            "database_changed_during_scan": (
                before_hash != after_hash or before_size != after_size
            ),
            "connection_mode": "sqlite_mode_ro_query_only",
            "database_written": False,
            "alembic": alembic,
            "quick_check": quick_check,
            "foreign_key_errors": foreign_key_errors,
        },
        "summary": summary,
        "locations": locations,
        "issues": issues,
    }


def _safe_output_dir(value: str | Path) -> Path:
    output = Path(value).resolve()
    if _is_symlink_path(output):
        raise ValueError(f"symbolic-link output paths are refused: {output}")
    return output


def _write_csv(
    path: Path,
    rows: Iterable[dict[str, Any]],
    columns: tuple[str, ...],
) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})


def _render_markdown(report: dict[str, Any]) -> str:
    audit = report["audit"]
    summary = report["summary"]
    lines = [
        "# N081 库存盘点准备只读报告",
        "",
        f"- 数据库：`{audit['database_path']}`",
        f"- Alembic：`{', '.join(audit['alembic']) or '未记录'}`",
        f"- 只读连接：`{audit['connection_mode']}`",
        f"- 是否写入数据库：`{str(audit['database_written']).lower()}`",
        f"- 扫描期间文件是否变化：`{str(audit['database_changed_during_scan']).lower()}`",
        f"- quick_check：`{audit['quick_check']}`",
        f"- foreign_key_check：`{audit['foreign_key_errors']}`",
        "",
        "## 表数量",
        "",
        "| 表 | 行数 |",
        "| --- | ---: |",
    ]
    lines.extend(
        f"| `{table}` | {count} |"
        for table, count in summary["table_counts"].items()
    )
    lines.extend(
        [
            "",
            "## 盘点准备问题",
            "",
            "| 问题 | 行数 |",
            "| --- | ---: |",
        ]
    )
    if summary["issue_counts"]:
        lines.extend(
            f"| `{issue}` | {count} |"
            for issue, count in summary["issue_counts"].items()
        )
    else:
        lines.append("| 无 | 0 |")
    lines.extend(
        [
            "",
            "## 使用边界",
            "",
            "- 本报告只用于 N081-0 盘点准备，不会创建或调整正式库存。",
            "- `active_lot_without_pallet` 不等于库存错误，只表示需要现场定位。",
            "- `pallet_snapshot_requires_review` 仍不是正式库存，不得直接抵扣订单。",
            "- 正式盘点入账必须另行完成隔离演练、备份和批次授权。",
            "",
        ]
    )
    return "\n".join(lines)


def write_outputs(
    report: dict[str, Any], output_dir: str | Path
) -> dict[str, str]:
    output = _safe_output_dir(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": output / "n081_inventory_readiness.json",
        "locations_csv": output / "n081_locations.csv",
        "issues_csv": output / "n081_readiness_issues.csv",
        "markdown": output / "N081_INVENTORY_READINESS.md",
    }
    paths["json"].write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_csv(paths["locations_csv"], report["locations"], LOCATION_COLUMNS)
    _write_csv(paths["issues_csv"], report["issues"], ISSUE_COLUMNS)
    paths["markdown"].write_text(_render_markdown(report), encoding="utf-8")
    return {name: str(path) for name, path in paths.items()}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="N081 Phase 0 inventory readiness read-only export"
    )
    parser.add_argument(
        "--database",
        required=True,
        help="Explicit SQLite database path; opened mode=ro",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory for JSON/CSV/Markdown review files",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = audit_database(args.database)
        outputs = write_outputs(report, args.output_dir)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        print(f"read-only audit refused: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "summary": report["summary"],
                "audit": report["audit"],
                "outputs": outputs,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
