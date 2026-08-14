"""P1-11D0 read-only raw-board inventory data-gate audit.

This command deliberately has no apply mode. It opens one explicitly named
SQLite database with ``mode=ro`` and ``PRAGMA query_only=ON`` and emits only
aggregate/anonymised review artifacts outside the database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import quote


REQUIRED_COLUMNS: dict[str, set[str]] = {
    "alembic_version": {"version_num"},
    "customers": {"id", "is_active"},
    "products": {"id", "customer_id", "is_active"},
    "warehouse_locations": {"id", "is_active"},
    "inventory_lots": {
        "id", "lot_number", "inventory_type", "warehouse_location_id",
        "quantity_available", "quantity_reserved", "quantity_consumed",
        "quantity_damaged", "quantity_scrapped", "unit", "status",
        "stock_date", "stock_date_accuracy", "version",
    },
    "semi_finished_inventory_details": {
        "inventory_lot_id", "owner_customer_id", "material_code_snapshot",
        "normalized_material_code", "layer_count", "flute_type",
        "board_length_mm", "board_width_mm", "component_type",
        "pieces_per_box", "stock_yield_per_sheet", "sheet_type",
        "crease_type", "crease_left_mm", "crease_middle_mm", "crease_right_mm",
    },
    "semi_finished_lot_allowed_products": {"inventory_lot_id", "product_id"},
    "inventory_pallet_items": {
        "inventory_lot_id", "item_type", "quantity", "unit", "match_status",
    },
    "inventory_reservations": {
        "inventory_lot_id", "reserved_stock_quantity",
        "consumed_stock_quantity", "released_stock_quantity", "status",
    },
}

VALID_STOCK_DATE_ACCURACY = {"exact", "estimated"}
VALID_COMPONENT_TYPES = {"whole", "cover", "base"}
VALID_CREASE_TYPES = {"毛片", "净料", "压线", "其他"}


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
    connection.execute("PRAGMA query_only=ON")
    if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
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


def _require_schema(connection: sqlite3.Connection) -> None:
    tables = _table_names(connection)
    missing_tables = sorted(set(REQUIRED_COLUMNS) - tables)
    if missing_tables:
        raise ValueError(f"database is missing tables: {', '.join(missing_tables)}")
    missing_columns: list[str] = []
    for table, required in REQUIRED_COLUMNS.items():
        missing_columns.extend(
            f"{table}.{column}"
            for column in sorted(required - _columns(connection, table))
        )
    if missing_columns:
        raise ValueError(f"database is missing columns: {', '.join(missing_columns)}")


def _positive(value: Any) -> bool:
    try:
        return value is not None and float(value) > 0
    except (TypeError, ValueError):
        return False


def _nonnegative(value: Any) -> bool:
    try:
        return value is not None and float(value) >= 0
    except (TypeError, ValueError):
        return False


def _required_text(value: Any) -> bool:
    return bool(str(value or "").strip())


def _lot_reasons(row: sqlite3.Row) -> tuple[list[str], list[str]]:
    missing: list[str] = []
    blockers: list[str] = []
    field_checks = {
        "inventory_type_not_semi_finished": str(
            row["inventory_type"] or ""
        ).strip() == "semi_finished",
        "missing_lot_number": _required_text(row["lot_number"]),
        "missing_or_invalid_version": _positive(row["version"]),
        "missing_or_inactive_location": bool(row["location_is_active"]),
        "missing_stock_date": _required_text(row["stock_date"]),
        "missing_or_invalid_stock_date_accuracy": str(
            row["stock_date_accuracy"] or ""
        ).strip() in VALID_STOCK_DATE_ACCURACY,
        "unit_not_sheets": str(row["unit"] or "").strip() == "sheets",
        "invalid_quantity_facts": all(
            _nonnegative(row[column])
            for column in (
                "quantity_available", "quantity_reserved", "quantity_consumed",
                "quantity_damaged", "quantity_scrapped",
            )
        ),
        "missing_material_code_snapshot": _required_text(row["material_code_snapshot"]),
        "missing_normalized_material_code": _required_text(row["normalized_material_code"]),
        "missing_or_invalid_layer_count": _positive(row["layer_count"]),
        "missing_flute_type": _required_text(row["flute_type"]),
        "missing_or_invalid_board_length_mm": _positive(row["board_length_mm"]),
        "missing_or_invalid_board_width_mm": _positive(row["board_width_mm"]),
        "missing_or_invalid_component_type": str(row["component_type"] or "").strip()
        in VALID_COMPONENT_TYPES,
        "missing_or_invalid_pieces_per_box": _positive(row["pieces_per_box"]),
        "missing_or_invalid_stock_yield_per_sheet": _positive(row["stock_yield_per_sheet"]),
        "sheet_type_not_raw_board": str(row["sheet_type"] or "").strip() == "raw_board",
        "missing_or_invalid_crease_type": str(row["crease_type"] or "").strip()
        in VALID_CREASE_TYPES,
    }
    missing.extend(reason for reason, valid in field_checks.items() if not valid)
    if str(row["crease_type"] or "").strip() == "压线" and not all(
        _positive(row[column])
        for column in ("crease_left_mm", "crease_middle_mm", "crease_right_mm")
    ):
        missing.append("missing_or_invalid_crease_dimensions")
    if row["owner_customer_id"] is not None:
        if not bool(row["owner_customer_active"]):
            missing.append("dedicated_missing_active_owner_customer")
        if int(row["valid_product_bindings"] or 0) < 1:
            missing.append("dedicated_missing_valid_product_binding")

    if str(row["status"] or "").strip() != "active":
        blockers.append("lot_status_not_active")
    if not _positive(row["quantity_available"]):
        blockers.append(
            "reserved_balance_insufficient"
            if _positive(row["quantity_reserved"])
            else "no_available_quantity"
        )
    if float(row["quantity_damaged"] or 0) > 0:
        blockers.append("has_damaged_quantity")
    if float(row["quantity_scrapped"] or 0) > 0:
        blockers.append("has_scrapped_quantity")
    return sorted(set(missing)), sorted(set(blockers))


def audit_database(
    database: str | Path,
    *,
    expected_sha256: str | None = None,
    expected_revision: str | None = None,
) -> dict[str, Any]:
    """Audit one SQLite replica without modifying it."""
    database_path = Path(database)
    if not database_path.is_file():
        raise ValueError(f"database must be an existing regular file: {database_path}")
    before_hash = _sha256(database_path)
    if expected_sha256 and before_hash != expected_sha256.strip().upper():
        raise ValueError(
            "database SHA-256 mismatch: "
            f"expected {expected_sha256.strip().upper()}, got {before_hash}"
        )

    connection = _connect_readonly(database_path)
    try:
        _require_schema(connection)
        total_changes_before = int(connection.total_changes)
        revision_row = connection.execute(
            "SELECT version_num FROM alembic_version LIMIT 1"
        ).fetchone()
        revision = str(revision_row[0]) if revision_row else None
        integrity_rows = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        integrity_ok = integrity_rows == ["ok"]
        foreign_key_errors = len(list(connection.execute("PRAGMA foreign_key_check")))
        raw_rows = list(
            connection.execute(
                """
                SELECT
                    lot.id, lot.lot_number, lot.inventory_type,
                    lot.quantity_available, lot.quantity_reserved,
                    lot.quantity_consumed, lot.quantity_damaged,
                    lot.quantity_scrapped, lot.unit, lot.status, lot.stock_date,
                    lot.stock_date_accuracy, lot.version,
                    detail.owner_customer_id, detail.material_code_snapshot,
                    detail.normalized_material_code, detail.layer_count,
                    detail.flute_type, detail.board_length_mm, detail.board_width_mm,
                    detail.component_type, detail.pieces_per_box,
                    detail.stock_yield_per_sheet, detail.sheet_type,
                    detail.crease_type, detail.crease_left_mm,
                    detail.crease_middle_mm, detail.crease_right_mm,
                    CASE WHEN location.id IS NOT NULL AND location.is_active = 1
                         THEN 1 ELSE 0 END AS location_is_active,
                    CASE WHEN customer.id IS NOT NULL AND customer.is_active = 1
                         THEN 1 ELSE 0 END AS owner_customer_active,
                    (
                        SELECT COUNT(*)
                        FROM semi_finished_lot_allowed_products binding
                        JOIN products product ON product.id = binding.product_id
                        WHERE binding.inventory_lot_id = lot.id
                          AND product.is_active = 1
                          AND detail.owner_customer_id IS NOT NULL
                          AND product.customer_id = detail.owner_customer_id
                    ) AS valid_product_bindings
                FROM inventory_lots lot
                JOIN semi_finished_inventory_details detail
                  ON detail.inventory_lot_id = lot.id
                LEFT JOIN warehouse_locations location
                  ON location.id = lot.warehouse_location_id
                LEFT JOIN customers customer
                  ON customer.id = detail.owner_customer_id
                WHERE detail.sheet_type = 'raw_board'
                ORDER BY lot.id
                """
            )
        )

        anonymous_candidates: list[dict[str, Any]] = []
        reason_counts: Counter[str] = Counter()
        complete_count = 0
        ready_customer_specific = 0
        ready_general = 0
        field_incomplete_lots = 0
        business_blocked_lots = 0
        for index, row in enumerate(raw_rows, start=1):
            missing, blockers = _lot_reasons(row)
            ready = not missing and not blockers
            scope = "customer_specific" if row["owner_customer_id"] is not None else "general"
            if not missing:
                complete_count += 1
            else:
                field_incomplete_lots += 1
            if blockers:
                business_blocked_lots += 1
            if ready and scope == "customer_specific":
                ready_customer_specific += 1
            if ready and scope == "general":
                ready_general += 1
            reason_counts.update(missing)
            reason_counts.update(blockers)
            anonymous_candidates.append(
                {
                    "anonymous_lot": f"raw-board-{index:03d}",
                    "scope": scope,
                    "field_complete": not missing,
                    "ready": ready,
                    "missing_or_invalid": missing,
                    "business_blockers": blockers,
                }
            )

        historical = connection.execute(
            """
            SELECT COUNT(*) AS item_count,
                   COALESCE(SUM(CAST(quantity AS NUMERIC)), 0) AS item_quantity
            FROM inventory_pallet_items
            WHERE inventory_lot_id IS NULL
              AND item_type = 'semi_finished'
              AND unit = 'sheets'
              AND match_status = 'matched'
            """
        ).fetchone()
        reservation = connection.execute(
            """
            SELECT COUNT(*) AS reservation_count,
                   COALESCE(SUM(reserved_stock_quantity), 0) AS reserved_quantity
            FROM inventory_reservations reservation
            JOIN semi_finished_inventory_details detail
              ON detail.inventory_lot_id = reservation.inventory_lot_id
            WHERE detail.sheet_type = 'raw_board'
              AND reservation.status IN ('active', 'partial')
            """
        ).fetchone()
        total_changes_after = int(connection.total_changes)
    finally:
        connection.close()

    after_hash = _sha256(database_path)
    database_unchanged = before_hash == after_hash
    denominator = len(raw_rows)
    completeness = {
        "complete": complete_count,
        "denominator": denominator,
        "percent": round(complete_count * 100.0 / denominator, 2)
        if denominator else None,
        "semantics": "measured_over_formal_raw_board_lots"
        if denominator else "no_formal_raw_board_denominator",
    }
    revision_matches = expected_revision is None or revision == expected_revision
    checks = {
        "database_hash_unchanged": database_unchanged,
        "query_only_no_changes": total_changes_before == total_changes_after == 0,
        "integrity_check_ok": integrity_ok,
        "foreign_key_check_ok": foreign_key_errors == 0,
        "revision_matches_current": revision_matches,
        "has_formal_raw_board_denominator": denominator > 0,
        "has_ready_customer_specific_lot": ready_customer_specific > 0,
        "has_ready_general_lot": ready_general > 0,
        "historical_unlinked_semi_items_resolved": int(historical[0]) == 0,
    }
    return {
        "task": "P1-11D0",
        "scope": {
            "stage": "real_data_gate_only",
            "business_compatibility": (
                "not_evaluated_without_frozen_demand_D1_D3_not_authorized"
            ),
        },
        "audit": {
            "database_written": False,
            "connection_mode": "sqlite_mode_ro_query_only",
            "database_changed_during_scan": not database_unchanged,
            "source_sha256_before": before_hash,
            "source_sha256_after": after_hash,
            "connection_total_changes_before": total_changes_before,
            "connection_total_changes_after": total_changes_after,
        },
        "source": {
            "database_revision": revision,
            "expected_revision": expected_revision,
            "integrity_check": integrity_rows,
            "foreign_key_errors": foreign_key_errors,
        },
        "summary": {
            "formal_raw_board_lots": denominator,
            "field_completeness": completeness,
            "ready_customer_specific_lots": ready_customer_specific,
            "ready_general_lots": ready_general,
            "field_incomplete_lots": field_incomplete_lots,
            "business_blocked_lots": business_blocked_lots,
            "unlinked_historical_semi_items": int(historical[0]),
            "unlinked_historical_semi_quantity": float(historical[1]),
            "active_raw_board_reservations": int(reservation[0]),
            "active_raw_board_reserved_quantity": float(reservation[1]),
        },
        "reason_counts": dict(sorted(reason_counts.items())),
        "anonymous_candidates": anonymous_candidates,
        "gate": {"status": "passed" if all(checks.values()) else "blocked", "checks": checks},
    }


def _markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    completeness = summary["field_completeness"]
    lines = [
        "# P1-11D0 原料片料数据门槛只读审计",
        "",
        f"- 门槛状态：`{report['gate']['status']}`",
        f"- 数据库写入：`{str(report['audit']['database_written']).lower()}`",
        f"- 连接方式：`{report['audit']['connection_mode']}`",
        f"- 数据库 revision：`{report['source']['database_revision']}`",
        f"- 期望 revision：`{report['source']['expected_revision']}`",
        "",
        "## 数据结论",
        "",
        f"- 正式 raw_board：{summary['formal_raw_board_lots']} 条正式 raw_board。",
    ]
    if completeness["denominator"] == 0:
        lines.append("- 没有正式 raw_board 分母，因此完整率不是 0%，而是不可计算。")
    else:
        lines.append(
            f"- 字段完整率：{completeness['complete']}/{completeness['denominator']}"
            f"（{completeness['percent']}%）。"
        )
    lines.extend(
        [
            f"- 可用客户专用片料：{summary['ready_customer_specific_lots']} 条。",
            f"- 可用通用片料：{summary['ready_general_lots']} 条。",
            "- 未关联正式 lot 的历史半成品栈板行："
            f"{summary['unlinked_historical_semi_items']} 条，"
            f"数量 {summary['unlinked_historical_semi_quantity']:g} 张。",
            "",
            "## 门槛检查",
            "",
        ]
    )
    for name, passed in report["gate"]["checks"].items():
        lines.append(f"- {'PASS' if passed else 'BLOCKED'}：`{name}`")
    if report["reason_counts"]:
        lines.extend(["", "## 匿名原因汇总", ""])
        for reason, count in report["reason_counts"].items():
            lines.append(f"- `{reason}`：{count}")
    lines.extend(
        [
            "", "## 边界", "",
            "本报告只含聚合数字、匿名编号和原因代码；不包含批次号、客户、产品、货位或原始业务 ID。",
            "匿名测试夹具只验证审计器，不构成真实数据门槛通过证据。",
            "D0 不接订单冻结需求，因此不计算尺寸/楞型等订单兼容性；D1～D3 未获授权。",
            "",
        ]
    )
    return "\n".join(lines)


def write_outputs(report: dict[str, Any], output: str | Path) -> dict[str, str]:
    output_path = Path(output)
    output_path.mkdir(parents=True, exist_ok=True)
    json_path = output_path / "p1_11d0_raw_board_data_gate.json"
    markdown_path = output_path / "p1_11d0_raw_board_data_gate.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(_markdown(report), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(markdown_path)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--expected-revision")
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    report = audit_database(
        args.database,
        expected_sha256=args.expected_sha256,
        expected_revision=args.expected_revision,
    )
    written = write_outputs(report, args.output)
    print(json.dumps({"gate": report["gate"], "outputs": written}, ensure_ascii=False))
    return 0 if report["gate"]["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
