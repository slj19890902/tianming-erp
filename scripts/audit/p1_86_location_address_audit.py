from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys


REQUIRED_TABLES = {
    "warehouse_floors",
    "warehouse_areas",
    "warehouse_locations",
}
CANONICAL_RACK = re.compile(
    r"^(?P<floor>[1-9]\d?)F-(?P<area>[A-G]\d{2})-(?P<rack>[A-Z])-"
    r"(?P<level>\d{2})-(?P<slot>\d{2})$",
    re.IGNORECASE,
)
CANONICAL_GROUND = re.compile(
    r"^(?P<floor>[1-9]\d?)F-(?P<area>[A-G]\d{2})-P(?P<row>\d{2})-"
    r"(?P<slot>\d{2})$",
    re.IGNORECASE,
)
LEGACY_FLOOR_ZONE_SERIAL = re.compile(r"^\d+F-[A-Z0-9]+-\d+$", re.IGNORECASE)
LEGACY_RACK_GRID = re.compile(r"^\d+F-[A-Z0-9]+-R\d+-L\d+-[A-Z]\d+$", re.IGNORECASE)
LEGACY_SECTION_RACK = re.compile(r"^F\d+-S\d+-R\d+$", re.IGNORECASE)


@dataclass(frozen=True)
class FileState:
    path: str
    exists: bool
    size: int | None
    mtime_ns: int | None
    sha256: str | None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_state(path: Path) -> FileState:
    if not path.exists():
        return FileState(str(path), False, None, None, None)
    stat = path.stat()
    return FileState(str(path), True, stat.st_size, stat.st_mtime_ns, _sha256(path))


def _source_states(database: Path) -> list[FileState]:
    return [
        _file_state(database),
        _file_state(Path(f"{database}-wal")),
        _file_state(Path(f"{database}-shm")),
        _file_state(Path(f"{database}-journal")),
    ]


def _normalize(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).upper()


def _location_format(code: str) -> str:
    normalized = _normalize(code)
    if CANONICAL_RACK.fullmatch(normalized):
        return "canonical_rack"
    if CANONICAL_GROUND.fullmatch(normalized):
        return "canonical_ground"
    if LEGACY_RACK_GRID.fullmatch(normalized):
        return "legacy_floor_rack_grid"
    if LEGACY_FLOOR_ZONE_SERIAL.fullmatch(normalized):
        return "legacy_floor_zone_serial"
    if LEGACY_SECTION_RACK.fullmatch(normalized):
        return "legacy_section_rack"
    return "free_text_or_other"


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _count_reference(
    connection: sqlite3.Connection,
    *,
    table: str,
    condition: str,
) -> int | None:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if table not in tables:
        return None
    try:
        return int(
            connection.execute(
                f'SELECT count(*) FROM "{table}" WHERE {condition}'
            ).fetchone()[0]
            or 0
        )
    except sqlite3.OperationalError:
        return None


def _recommended_mapping(row: sqlite3.Row, columns: set[str]) -> dict:
    code = str(row["location_code"] or "").strip()
    canonical = CANONICAL_RACK.fullmatch(_normalize(code)) or CANONICAL_GROUND.fullmatch(
        _normalize(code)
    )
    if canonical:
        return {
            "status": "already_canonical",
            "recommended_address": _normalize(code),
            "old_alias": None,
            "decision": "none",
        }
    if {
        "address_kind",
        "address_zone_code",
        "address_subzone_no",
        "rack_code",
        "level_no",
        "slot_no",
        "ground_row_no",
    } <= columns:
        kind = str(row["address_kind"] or "legacy")
        floor = int(row["warehouse_floor"] or 0)
        zone = str(row["address_zone_code"] or "").upper()
        subzone = int(row["address_subzone_no"] or 0)
        slot = int(row["slot_no"] or 0)
        if kind == "rack_slot" and floor and zone and subzone and slot:
            rack = str(row["rack_code"] or "").upper()
            level = int(row["level_no"] or 0)
            if rack and level:
                return {
                    "status": "structured_candidate",
                    "recommended_address": (
                        f"{floor}F-{zone}{subzone:02d}-{rack}-{level:02d}-{slot:02d}"
                    ),
                    "old_alias": code,
                    "decision": "verify_map_and_physical_label",
                }
        if kind == "ground_slot" and floor and zone and subzone and slot:
            row_no = int(row["ground_row_no"] or 0)
            if row_no:
                return {
                    "status": "structured_candidate",
                    "recommended_address": (
                        f"{floor}F-{zone}{subzone:02d}-P{row_no:02d}-{slot:02d}"
                    ),
                    "old_alias": code,
                    "decision": "verify_map_and_physical_label",
                }
    return {
        "status": "manual_required",
        "recommended_address": None,
        "old_alias": code or None,
        "decision": "do_not_guess",
    }


def audit_database(database: Path) -> dict:
    resolved = database.resolve(strict=True)
    uri = f"file:{resolved.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        missing = sorted(REQUIRED_TABLES - tables)
        if missing:
            raise RuntimeError(f"缺少仓库台账表：{', '.join(missing)}")
        location_columns = _table_columns(connection, "warehouse_locations")
        area_columns = _table_columns(connection, "warehouse_areas")
        selected = [
            "location.id",
            "location.location_code",
            "location.location_name",
            "location.warehouse_floor",
            "location.area_code",
            "location.storage_type",
            "location.is_active",
            "location.placement_status",
        ]
        if "address_kind" in location_columns:
            selected.extend(
                [
                    "location.address_kind",
                    "location.rack_code",
                    "location.ground_row_no",
                    "location.level_no",
                    "location.slot_no",
                ]
            )
        else:
            selected.extend(
                [
                    "'legacy' AS address_kind",
                    "NULL AS rack_code",
                    "NULL AS ground_row_no",
                    "location.level_no",
                    "NULL AS slot_no",
                ]
            )
        if "address_area_id" in location_columns and {
            "address_zone_code",
            "address_subzone_no",
        } <= area_columns:
            selected.extend(
                [
                    "area.address_zone_code",
                    "area.address_subzone_no",
                ]
            )
            join = "LEFT JOIN warehouse_areas area ON area.id=location.address_area_id"
        else:
            selected.extend(
                ["NULL AS address_zone_code", "NULL AS address_subzone_no"]
            )
            join = ""
        rows = connection.execute(
            f"SELECT {', '.join(selected)} FROM warehouse_locations location {join} "
            "ORDER BY location.id"
        ).fetchall()
        normalized_owners: dict[str, list[int]] = {}
        formats: dict[str, int] = {}
        mappings: list[dict] = []
        for row in rows:
            code = str(row["location_code"] or "")
            normalized_owners.setdefault(_normalize(code), []).append(int(row["id"]))
            category = _location_format(code)
            formats[category] = formats.get(category, 0) + 1
            mapping = _recommended_mapping(
                row,
                set(row.keys()),
            )
            mappings.append(
                {
                    "stable_location_id": int(row["id"]),
                    "current_code": code,
                    "current_name": str(row["location_name"] or ""),
                    "warehouse_floor": row["warehouse_floor"],
                    "area_code": row["area_code"],
                    "format": category,
                    **mapping,
                }
            )
        duplicate_codes = [
            {"normalized_code": code, "stable_location_ids": ids}
            for code, ids in sorted(normalized_owners.items())
            if code and len(ids) > 1
        ]
        reference_counts = {
            "inventory_lots": _count_reference(
                connection,
                table="inventory_lots",
                condition="warehouse_location_id IS NOT NULL",
            ),
            "current_pallets": _count_reference(
                connection,
                table="inventory_pallets",
                condition="location_id IS NOT NULL AND is_current=1",
            ),
            "location_movements": _count_reference(
                connection,
                table="inventory_location_movements",
                condition="from_location_id IS NOT NULL OR to_location_id IS NOT NULL",
            ),
            "map_layouts": _count_reference(
                connection,
                table="floor3_location_layouts",
                condition="location_id IS NOT NULL",
            ),
            "production_completions": _count_reference(
                connection,
                table="production_completions",
                condition="warehouse_location_id IS NOT NULL",
            ),
            "molds_with_location_text": _count_reference(
                connection,
                table="mold_tools",
                condition="length(trim(rack_location)) > 0",
            ),
            "printing_plates_with_location_text": _count_reference(
                connection,
                table="printing_plates",
                condition="length(trim(rack_location)) > 0",
            ),
        }
        return {
            "schema_version": 1,
            "task_id": "P1-86A",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "database_file": resolved.name,
            "read_only": True,
            "summary": {
                "location_total": len(rows),
                "missing_floor": sum(row["warehouse_floor"] is None for row in rows),
                "missing_area": sum(not str(row["area_code"] or "").strip() for row in rows),
                "inactive": sum(not bool(row["is_active"]) for row in rows),
                "format_counts": formats,
                "normalized_duplicate_count": len(duplicate_codes),
                "manual_mapping_required": sum(
                    row["status"] == "manual_required" for row in mappings
                ),
            },
            "reference_counts": reference_counts,
            "normalized_duplicates": duplicate_codes,
            "mappings": mappings,
            "rule": "没有唯一结构化事实的旧码一律保持 manual_required，不从字符串猜楼层、区域、货架或层格。",
        }
    finally:
        connection.close()


def _assert_safe_outputs(database: Path, output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = [
        output_dir / "p1_86_location_address_audit.json",
        output_dir / "p1_86_location_address_mapping.csv",
        output_dir / "p1_86_location_address_audit.md",
    ]
    source_paths = [database, Path(f"{database}-wal"), Path(f"{database}-shm")]
    for output in outputs:
        if output.exists():
            raise RuntimeError(f"输出文件已存在，拒绝覆盖：{output}")
        if output.resolve() == database.resolve():
            raise RuntimeError("输出路径不能指向源数据库")
        for source in source_paths:
            if source.exists():
                try:
                    if output.exists() and output.samefile(source):
                        raise RuntimeError("输出文件不能是源数据库硬链接")
                except OSError:
                    pass
    return outputs


def write_reports(database: Path, output_dir: Path, report: dict) -> list[Path]:
    outputs = _assert_safe_outputs(database, output_dir)
    json_path, csv_path, markdown_path = outputs
    with json_path.open("x", encoding="utf-8", newline="") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    with csv_path.open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "stable_location_id",
                "current_code",
                "current_name",
                "warehouse_floor",
                "area_code",
                "format",
                "status",
                "recommended_address",
                "old_alias",
                "decision",
            ],
        )
        writer.writeheader()
        writer.writerows(report["mappings"])
    summary = report["summary"]
    lines = [
        "# P1-86A 正式位置只读盘点",
        "",
        f"- 只读：`{str(report['read_only']).lower()}`",
        f"- 位置总数：`{summary['location_total']}`",
        f"- 缺楼层：`{summary['missing_floor']}`",
        f"- 缺区域：`{summary['missing_area']}`",
        f"- 大小写归一后重码：`{summary['normalized_duplicate_count']}`",
        f"- 必须人工映射：`{summary['manual_mapping_required']}`",
        "",
        "## 格式统计",
        "",
    ]
    lines.extend(
        f"- `{name}`：`{count}`"
        for name, count in sorted(summary["format_counts"].items())
    )
    lines.extend(
        [
            "",
            "## 规则",
            "",
            report["rule"],
            "",
            "详细稳定位置映射见同目录 CSV；本报告未写入任何位置、库存或地图事实。",
        ]
    )
    with markdown_path.open("x", encoding="utf-8", newline="") as handle:
        handle.write("\n".join(lines) + "\n")
    return outputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P1-86A warehouse address read-only audit")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    database = args.database.resolve(strict=True)
    before = _source_states(database)
    outputs: list[Path] = []
    try:
        report = audit_database(database)
        outputs = write_reports(database, args.output_dir.resolve(), report)
        after = _source_states(database)
        if before != after:
            for path in outputs:
                path.unlink(missing_ok=True)
            raise RuntimeError("只读审计期间源数据库或 sidecar 发生变化，报告已删除。")
    except Exception as error:
        print(f"P1-86A audit failed: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "ok",
                "read_only": True,
                "database_sha256": before[0].sha256,
                "outputs": [str(path) for path in outputs],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
