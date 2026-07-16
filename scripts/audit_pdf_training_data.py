"""Read-only governance audit for the PDF training library.

The command has no apply or delete mode.  It opens SQLite with ``mode=ro``
and ``query_only``; it only writes reports to the requested output directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


TABLES = {
    "samples": "pdf_order_training_samples",
    "templates": "pdf_order_customer_templates",
    "batches": "pdf_order_training_batches",
    "corrections": "pdf_order_correction_logs",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def open_readonly(database: Path) -> sqlite3.Connection:
    database = database.expanduser().resolve()
    if not database.is_file():
        raise FileNotFoundError(f"database file does not exist: {database}")
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
        connection.close()
        raise RuntimeError("could not enable SQLite query_only protection")
    return connection


def _text_quality(text: str) -> tuple[str, dict[str, int | float]]:
    extension_a = sum("\u3400" <= char <= "\u4dbf" for char in text)
    basic_han = sum("\u4e00" <= char <= "\u9fff" for char in text)
    ratio = extension_a / max(extension_a + basic_han, 1)
    if not text.strip():
        status = "image_only"
    elif extension_a >= 8 and ratio >= 0.15:
        status = "garbled_text_layer"
    else:
        status = "readable_text"
    return status, {
        "text_chars": len(text),
        "basic_han_chars": basic_han,
        "extension_a_chars": extension_a,
        "extension_a_ratio": ratio,
    }


def classify_text_quality(text: str) -> tuple[str, dict[str, int | float]]:
    return _text_quality(text)


def _require_tables(connection: sqlite3.Connection) -> None:
    existing = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = sorted(set(TABLES.values()) - existing)
    if missing:
        raise RuntimeError(f"missing PDF training tables: {', '.join(missing)}")


def _has_json(value: Any) -> bool:
    return str(value or "").strip() not in {"", "{}", "null", "None"}


def _fingerprint(row: sqlite3.Row) -> tuple[Any, ...]:
    return tuple(row[field] for field in (
        "customer_id", "template_name", "order_no_pattern", "date_pattern",
        "item_row_pattern", "customer_name_pattern", "column_map_json", "is_active",
    ))


def audit_database(database: Path) -> dict[str, Any]:
    before = {"size": database.stat().st_size, "mtime_ns": database.stat().st_mtime_ns, "sha256": sha256_file(database)}
    with open_readonly(database) as connection:
        _require_tables(connection)
        samples = connection.execute(
            f"SELECT id, file_sha256, parse_status, parse_method, ground_truth_json, score FROM {TABLES['samples']} ORDER BY id"
        ).fetchall()
        templates = connection.execute(
            f"SELECT id, customer_id, template_name, order_no_pattern, date_pattern, item_row_pattern, customer_name_pattern, column_map_json, is_active FROM {TABLES['templates']} ORDER BY id"
        ).fetchall()
        batch_count = connection.execute(f"SELECT COUNT(*) FROM {TABLES['batches']}").fetchone()[0]
        correction_count = connection.execute(f"SELECT COUNT(*) FROM {TABLES['corrections']}").fetchone()[0]
    by_sha: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in samples:
        if row["file_sha256"]:
            by_sha[str(row["file_sha256"])].append(row)
    duplicate_samples = []
    for digest, rows in by_sha.items():
        if len(rows) > 1:
            keeper = max(rows, key=lambda row: (row["parse_status"] != "pending", _has_json(row["ground_truth_json"]), row["score"] is not None, -row["id"]))
            duplicate_samples.append({"file_sha256": digest, "keeper_id": keeper["id"], "candidate_duplicate_ids": [row["id"] for row in rows if row["id"] != keeper["id"]]})
    by_template: dict[tuple[Any, ...], list[sqlite3.Row]] = defaultdict(list)
    for row in templates:
        by_template[_fingerprint(row)].append(row)
    duplicate_templates = [
        {"keeper_id": min(row["id"] for row in rows), "candidate_duplicate_ids": [row["id"] for row in rows if row["id"] != min(r["id"] for r in rows)]}
        for rows in by_template.values() if len(rows) > 1
    ]
    ground_truth = [row for row in samples if _has_json(row["ground_truth_json"])]
    after = {"size": database.stat().st_size, "mtime_ns": database.stat().st_mtime_ns, "sha256": sha256_file(database)}
    return {
        "database_path": str(database.resolve()), "connection_mode": "sqlite_mode_ro_query_only", "database_written": False,
        "database_fingerprint_before": before, "database_fingerprint_after": after, "database_fingerprint_unchanged": before == after,
        "summary": {
            "batch_rows": batch_count, "sample_rows": len(samples), "unique_sample_sha256": len(by_sha),
            "duplicate_sample_rows": sum(len(rows) - 1 for rows in by_sha.values() if len(rows) > 1),
            "ground_truth_rows": len(ground_truth), "ground_truth_unique_sha256": len({row["file_sha256"] for row in ground_truth}),
            "pending_with_ground_truth_rows": sum(row["parse_status"] == "pending" for row in ground_truth),
            "correction_log_rows": correction_count, "template_rows": len(templates), "template_fingerprint_count": len(by_template),
            "parse_status": dict(Counter(row["parse_status"] for row in samples)), "parse_method": dict(Counter(row["parse_method"] for row in samples)),
        },
        "dry_run_cleanup_candidates": {"duplicate_sample_groups": duplicate_samples, "duplicate_template_groups": duplicate_templates},
    }


def audit(sample_dir: Path, database: Path) -> dict[str, Any]:
    report = audit_database(database)
    pdfs = list(sample_dir.glob("*.pdf"))
    report.update({"generated_at": datetime.now().isoformat(timespec="seconds"), "sample_dir": str(sample_dir.resolve()), "physical_summary": {"pdf_count": len(pdfs)}})
    return report


def write_report(report: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path, markdown_path = output_dir / "pdf_training_data_audit.json", output_dir / "pdf_training_data_audit.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text("# PDF training library read-only audit\n\n- No apply/delete mode.\n- database_written=false\n- Dry-run cleanup candidates only.\n", encoding="utf-8")
    return json_path, markdown_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only PDF training library audit")
    parser.add_argument("database", type=Path)
    parser.add_argument("sample_dir", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.sample_dir, args.database)
    write_report(report, args.output_dir)
    print("database_written=false")
    print(f"database_fingerprint_unchanged={report['database_fingerprint_unchanged']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
