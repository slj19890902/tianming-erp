"""Read-only N022 Phase C.1 audit for mold location pilot data.

This module deliberately has no apply mode. It reads only ``mold_tools`` from
an explicitly supplied SQLite database and emits deterministic review files.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote


FLAT_LOCATION = re.compile(
    r"^3F-M-R(?P<rack>\d+)-L(?P<level>[1-3])-D(?P<row>\d+)-P(?P<position>\d+)$",
    re.IGNORECASE,
)
VERTICAL_LOCATION = re.compile(
    r"^3F-M-R(?P<rack>\d+)-L(?P<level>[1-3])-V-P(?P<position>\d+)$",
    re.IGNORECASE,
)
REPORT_COLUMNS = (
    "issue", "mold_id", "mold_code", "mold_name", "rack_location",
    "normalized_location", "location_kind", "is_active", "related_mold_ids",
)
_FORMAL_LIVE_PATH_MARKERS = {"live", "prod", "production", "正式库"}


def _is_symlink_path(path: Path) -> bool:
    """Reject a symlink at the path itself or in an existing parent directory."""
    candidate = path.absolute()
    return any(parent.exists() and parent.is_symlink() for parent in (candidate, *candidate.parents))


def _location_kind(value: str) -> str:
    if not value:
        return "missing"
    if FLAT_LOCATION.fullmatch(value):
        return "flat"
    if VERTICAL_LOCATION.fullmatch(value):
        return "vertical"
    if value.upper().startswith("3F-M"):
        return "invalid_3f_m"
    return "legacy_free_text"


def _connect_readonly(database: Path) -> sqlite3.Connection:
    if not database.is_file():
        raise ValueError(f"database must be an existing regular file: {database}")
    if _is_symlink_path(database):
        raise ValueError(f"symbolic-link database paths are refused: {database}")
    uri_path = str(database.absolute()).replace("\\", "/")
    connection = sqlite3.connect(f"file:{quote(uri_path, safe='/:')}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
        connection.close()
        raise RuntimeError("could not enable SQLite query_only mode")
    return connection


def _require_mold_tools_schema(connection: sqlite3.Connection) -> None:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'mold_tools'"
    ).fetchone()
    if not exists:
        raise ValueError("database does not contain required mold_tools table")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(mold_tools)")}
    required = {"id", "mold_code", "mold_name", "rack_location", "is_active"}
    missing = sorted(required - columns)
    if missing:
        raise ValueError(f"mold_tools table is missing columns: {', '.join(missing)}")


def _issue(issue: str, record: dict[str, Any], related_ids: Iterable[int] = ()) -> dict[str, Any]:
    return {
        "issue": issue,
        **record,
        "related_mold_ids": [mold_id for mold_id in related_ids if mold_id != record["mold_id"]],
    }


def audit_database(database: str | Path) -> dict[str, Any]:
    """Return a deterministic, read-only audit report for one SQLite database."""
    database_path = Path(database)
    connection = _connect_readonly(database_path)
    try:
        _require_mold_tools_schema(connection)
        rows = connection.execute(
            "SELECT id, mold_code, mold_name, rack_location, is_active FROM mold_tools ORDER BY id"
        ).fetchall()
    finally:
        connection.close()

    records: list[dict[str, Any]] = []
    location_to_ids: dict[str, list[int]] = defaultdict(list)
    mold_code_to_ids: dict[str, list[int]] = defaultdict(list)
    for mold_id, mold_code, mold_name, rack_location, is_active in rows:
        raw_location = (rack_location or "").strip()
        normalized = raw_location.upper()
        record = {
            "mold_id": int(mold_id), "mold_code": str(mold_code or "").strip(),
            "mold_name": str(mold_name or "").strip(), "rack_location": raw_location,
            "normalized_location": normalized, "location_kind": _location_kind(normalized),
            "is_active": bool(is_active),
        }
        records.append(record)
        if record["location_kind"] in {"flat", "vertical"} and record["is_active"]:
            location_to_ids[normalized].append(record["mold_id"])
        if record["mold_code"]:
            mold_code_to_ids[record["mold_code"].upper()].append(record["mold_id"])

    records_by_id = {record["mold_id"]: record for record in records}
    issues: list[dict[str, Any]] = []
    for record in records:
        issue = {"missing": "missing_location", "legacy_free_text": "legacy_free_text",
                 "invalid_3f_m": "invalid_3f_m_code"}.get(record["location_kind"])
        if issue:
            issues.append(_issue(issue, record))
        if not record["is_active"]:
            issues.append(_issue("inactive_mold", record))
    for mold_ids in location_to_ids.values():
        if len(mold_ids) > 1:
            issues.extend(_issue("duplicate_location_occupancy", records_by_id[mold_id], mold_ids) for mold_id in mold_ids)
    for mold_ids in mold_code_to_ids.values():
        if len(mold_ids) > 1:
            issues.extend(_issue("duplicate_mold_code", records_by_id[mold_id], mold_ids) for mold_id in mold_ids)

    issues.sort(key=lambda row: (row["issue"], row["mold_id"], row["related_mold_ids"]))
    summary = {
        "total_molds": len(records), "active_molds": sum(record["is_active"] for record in records),
        "inactive_molds": sum(not record["is_active"] for record in records),
        "location_kinds": dict(sorted(Counter(record["location_kind"] for record in records).items())),
        "issue_counts": dict(sorted(Counter(issue["issue"] for issue in issues).items())),
        "issue_rows": len(issues),
    }
    return {"database": str(database_path.absolute()), "records": records, "issues": issues, "summary": summary}


def _safe_output_path(value: str, database: Path) -> Path:
    output = Path(value)
    if _is_symlink_path(output):
        raise ValueError(f"symbolic-link output paths are refused: {output}")
    if _is_formal_live_path(output):
        raise ValueError(f"formal/live database paths are refused for report output: {output}")
    if output.absolute() == database.absolute():
        raise ValueError("report output path must not be the database path")
    return output


def _is_formal_live_path(path: Path) -> bool:
    parts = {part.casefold() for part in path.absolute().parts}
    return bool(parts & _FORMAL_LIVE_PATH_MARKERS) or "纸箱厂erp软件搭建" in str(path.absolute())


def write_json(report: dict[str, Any], output: str | Path, database: str | Path) -> None:
    path = _safe_output_path(str(output), Path(database))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_csv(report: dict[str, Any], output: str | Path, database: str | Path) -> None:
    path = _safe_output_path(str(output), Path(database))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for issue in report["issues"]:
            row = dict(issue)
            row["related_mold_ids"] = ",".join(str(value) for value in row["related_mold_ids"])
            writer.writerow({column: row[column] for column in REPORT_COLUMNS})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="N022 mold location pilot read-only dry-run")
    parser.add_argument("--database", required=True, help="Explicit SQLite database path; opened mode=ro")
    parser.add_argument("--json", dest="json_output", help="Optional JSON report output path")
    parser.add_argument("--csv", dest="csv_output", help="Optional CSV difference output path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = audit_database(args.database)
        if args.json_output:
            write_json(report, args.json_output, args.database)
        if args.csv_output:
            write_csv(report, args.csv_output, args.database)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        print(f"dry-run refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report["summary"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
