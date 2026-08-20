from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from scripts.audit.phase0_d1_readonly_reproduction import (
    D1AuditError,
    audit_database,
    render_markdown,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
CREATE TABLE alembic_version(version_num TEXT NOT NULL);
CREATE TABLE sales_orders(
    id INTEGER PRIMARY KEY,
    order_number TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE sales_order_items(
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL,
    snapshot_product_code TEXT,
    material_status TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    delivered_quantity INTEGER NOT NULL,
    is_force_closed INTEGER NOT NULL
);
CREATE TABLE incoming_receipts(
    id INTEGER PRIMARY KEY,
    status TEXT NOT NULL
);
CREATE TABLE incoming_receipt_items(
    id INTEGER PRIMARY KEY,
    receipt_id INTEGER NOT NULL,
    order_item_id INTEGER,
    received_quantity INTEGER NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE production_tasks(
    id INTEGER PRIMARY KEY,
    order_item_id INTEGER NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE production_completions(
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL,
    order_item_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    inventory_lot_id INTEGER,
    quantity INTEGER NOT NULL,
    actual_output_quantity INTEGER NOT NULL,
    direct_delivery_quantity INTEGER NOT NULL,
    stock_quantity INTEGER NOT NULL
);
CREATE TABLE inventory_lots(
    id INTEGER PRIMARY KEY,
    source_ref_type TEXT,
    source_ref_id INTEGER
);
CREATE TABLE inventory_movements(
    id INTEGER PRIMARY KEY,
    inventory_lot_id INTEGER NOT NULL,
    movement_type TEXT NOT NULL,
    related_order_item_id INTEGER
);

INSERT INTO alembic_version VALUES ('vv30v8x9z19');
INSERT INTO sales_orders VALUES (1, 'ORDER-SECRET-A', 'pending_production');
INSERT INTO sales_orders VALUES (2, 'ORDER-SECRET-B', 'delivered');

INSERT INTO sales_order_items VALUES (101, 1, 'PRODUCT-SECRET-LEGACY', 'received', 10, 0, 0);
INSERT INTO sales_order_items VALUES (102, 1, 'PRODUCT-SECRET-NORMALIZED', 'pending', 20, 0, 0);
INSERT INTO sales_order_items VALUES (103, 1, 'PRODUCT-SECRET-COMPONENT', 'pending', 30, 0, 0);
INSERT INTO sales_order_items VALUES (104, 1, 'PRODUCT-SECRET-PENDING', 'received', 40, 0, 0);
INSERT INTO sales_order_items VALUES (105, 1, 'PRODUCT-SECRET-GAP', 'received', 50, 0, 0);
INSERT INTO sales_order_items VALUES (106, 2, 'PRODUCT-SECRET-HISTORY', 'received', 60, 60, 0);
INSERT INTO sales_order_items VALUES (107, 1, 'PRODUCT-SECRET-TRACED', 'received', 70, 0, 0);

INSERT INTO incoming_receipts VALUES (1, 'posted');
INSERT INTO incoming_receipt_items VALUES (1, 1, 102, 20, 'posted');
INSERT INTO incoming_receipt_items VALUES (2, 1, 103, 30, 'posted');
INSERT INTO incoming_receipt_items VALUES (3, 1, 104, 40, 'posted');
INSERT INTO incoming_receipt_items VALUES (4, 1, 107, 70, 'posted');

INSERT INTO production_tasks VALUES (201, 103, 'waiting_material');
INSERT INTO production_tasks VALUES (202, 103, 'waiting_material');
INSERT INTO production_tasks VALUES (203, 104, 'pending');
INSERT INTO production_tasks VALUES (204, 105, 'completed');
INSERT INTO production_tasks VALUES (205, 106, 'completed');
INSERT INTO production_tasks VALUES (206, 107, 'completed');

INSERT INTO production_completions VALUES (301, 204, 105, 'posted', NULL, 50, 50, 50, 0);
INSERT INTO production_completions VALUES (302, 205, 106, 'posted', NULL, 60, 60, 60, 0);
INSERT INTO production_completions VALUES (303, 206, 107, 'posted', 401, 70, 70, 0, 70);
INSERT INTO inventory_lots VALUES (401, 'production_completion', 303);
INSERT INTO inventory_movements VALUES (501, 401, 'manual_in', 107);
"""
        )


def test_audit_reproduces_categories_without_exposing_identifiers_or_writing(
    tmp_path: Path,
) -> None:
    database = tmp_path / "factory-snapshot.sqlite3"
    _build_database(database)
    sha_before = _sha256(database)
    stat_before = database.stat()

    result = audit_database(database, expected_sha256=sha_before)

    assert result["counts"] == {
        "unfinished_orders": 1,
        "unfinished_items": 6,
        "received_without_task": 2,
        "trace_interruptions": 3,
        "posted_completion_gaps_current_unfinished": 1,
        "posted_completion_gaps_all_history": 2,
    }
    assert result["classification"]["received_without_task_subtypes"] == {
        "legacy_status_no_task": 1,
        "normalized_receipt_no_task": 1,
    }
    assert result["classification"]["received_evidence_task_partition"] == {
        "all_waiting_material": {"item_count": 1, "order_count": 1},
        "completed_or_not_required": {"item_count": 2, "order_count": 1},
        "no_task": {"item_count": 2, "order_count": 1},
        "pending": {"item_count": 1, "order_count": 1},
    }
    assert result["classification"]["completion_gap_subtypes"] == {
        "direct_delivery_only_without_inventory_trace": 1
    }
    assert result["dry_run"]["apply_executed"] is False
    assert result["dry_run"]["before_counts"] == result["dry_run"]["after_counts"]
    assert result["database"]["query_only"] == 1
    assert result["database"]["write_probe_denied"] is True
    assert result["database"]["total_changes_before"] == 0
    assert result["database"]["total_changes_after"] == 0
    assert result["database"]["sha256_before"] == sha_before
    assert result["database"]["sha256_after"] == sha_before
    assert _sha256(database) == sha_before
    assert database.stat().st_mtime_ns == stat_before.st_mtime_ns

    serialized = json.dumps(result, ensure_ascii=False)
    markdown = render_markdown(result)
    for secret in (
        "ORDER-SECRET-A",
        "ORDER-SECRET-B",
        "PRODUCT-SECRET-LEGACY",
        "PRODUCT-SECRET-NORMALIZED",
        "PRODUCT-SECRET-COMPONENT",
        "PRODUCT-SECRET-GAP",
    ):
        assert secret not in serialized
        assert secret not in markdown
    assert "ITEM-" in serialized
    assert "automatic_apply_allowed" in serialized


def test_audit_refuses_sha_mismatch_and_missing_tables(tmp_path: Path) -> None:
    database = tmp_path / "incomplete.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE alembic_version(version_num TEXT NOT NULL)")
        connection.execute("INSERT INTO alembic_version VALUES ('vv30v8x9z19')")

    with pytest.raises(D1AuditError, match="SHA-256"):
        audit_database(database, expected_sha256="0" * 64)

    with pytest.raises(D1AuditError, match="required tables missing"):
        audit_database(database, expected_sha256=_sha256(database))
