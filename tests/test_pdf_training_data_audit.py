from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
from pathlib import Path

import pytest

_AUDIT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "audit_pdf_training_data.py"
_SPEC = importlib.util.spec_from_file_location("pdf_training_data_audit", _AUDIT_PATH)
assert _SPEC and _SPEC.loader
_AUDIT = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_AUDIT)
audit = _AUDIT.audit
classify_text_quality = _AUDIT.classify_text_quality
open_readonly = _AUDIT.open_readonly
write_report = _AUDIT.write_report


def _database(path: Path) -> Path:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE pdf_order_training_batches (id INTEGER PRIMARY KEY);
            CREATE TABLE pdf_order_training_samples (
                id INTEGER PRIMARY KEY, file_sha256 TEXT, parse_status TEXT,
                parse_method TEXT, ground_truth_json TEXT, score REAL
            );
            CREATE TABLE pdf_order_customer_templates (
                id INTEGER PRIMARY KEY, customer_id INTEGER, template_name TEXT,
                order_no_pattern TEXT, date_pattern TEXT, item_row_pattern TEXT,
                customer_name_pattern TEXT, column_map_json TEXT, is_active INTEGER
            );
            CREATE TABLE pdf_order_correction_logs (id INTEGER PRIMARY KEY);
            INSERT INTO pdf_order_training_samples VALUES
                (1, 'same', 'pending', 'text', NULL, NULL),
                (2, 'same', 'labeled', 'ocr', '{"items":[]}', 1.0),
                (3, 'unique', 'pending', 'ocr', '{"items":[]}', NULL);
            """
        )
    return path


def test_audit_is_readonly_and_returns_dry_run_candidates(tmp_path: Path) -> None:
    database = _database(tmp_path / "training.sqlite3")
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    report = audit(tmp_path, database)

    assert report["database_written"] is False
    assert report["database_fingerprint_unchanged"] is True
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert report["summary"]["duplicate_sample_rows"] == 1
    assert report["dry_run_cleanup_candidates"]["duplicate_sample_groups"][0]["keeper_id"] == 2


def test_readonly_connection_rejects_writes(tmp_path: Path) -> None:
    database = _database(tmp_path / "readonly.sqlite3")
    with open_readonly(database) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO pdf_order_training_batches VALUES (2)")


def test_report_and_custom_font_quality_are_explicit(tmp_path: Path) -> None:
    report = audit(tmp_path, _database(tmp_path / "report.sqlite3"))
    _, markdown_path = write_report(report, tmp_path / "report")

    assert "database_written=false" in markdown_path.read_text(encoding="utf-8")
    assert classify_text_quality("")[0] == "image_only"
    assert classify_text_quality("㐀" * 12)[0] == "garbled_text_layer"
