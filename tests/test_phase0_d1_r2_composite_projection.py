from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from scripts.audit.phase0_d1_r2_composite_projection import audit_r2_database


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
CREATE TABLE sales_orders(id INTEGER PRIMARY KEY, status TEXT NOT NULL);
CREATE TABLE sales_order_items(
 id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, material_status TEXT NOT NULL,
 is_virtual_composite_parent_snapshot INTEGER, combination_role TEXT,
 is_force_closed INTEGER, delivered_quantity INTEGER, quantity INTEGER
);
CREATE TABLE incoming_receipts(id INTEGER PRIMARY KEY, status TEXT NOT NULL);
CREATE TABLE incoming_receipt_items(
 id INTEGER PRIMARY KEY, receipt_id INTEGER, order_item_id INTEGER,
 requisition_item_id INTEGER, received_quantity INTEGER, status TEXT
);
CREATE TABLE production_tasks(id INTEGER PRIMARY KEY, order_item_id INTEGER, status TEXT);
CREATE TABLE sales_order_item_bom_components(
 id INTEGER PRIMARY KEY, sales_order_item_id INTEGER, is_required INTEGER,
 required_piece_quantity INTEGER, snapshot_component_default_cutting_mode TEXT,
 display_order INTEGER
);
CREATE TABLE requisition_item_bom_sources(
 id INTEGER PRIMARY KEY, requisition_item_id INTEGER,
 sales_order_item_bom_component_id INTEGER, active_guard INTEGER
);
INSERT INTO sales_orders VALUES (1, 'pending_production');
INSERT INTO sales_order_items VALUES (10, 1, 'pending', 1, 'set_parent', 0, 0, 5);
INSERT INTO incoming_receipts VALUES (20, 'posted');
INSERT INTO incoming_receipt_items VALUES
 (30, 20, 10, 40, 3, 'posted'),
 (31, 20, 10, 41, 5, 'posted');
INSERT INTO production_tasks VALUES
 (50, 10, 'waiting_material'),
 (51, 10, 'waiting_material');
INSERT INTO sales_order_item_bom_components VALUES
 (60, 10, 1, 10, '一开四', 1),
 (61, 10, 1, 20, '一开四', 2);
INSERT INTO requisition_item_bom_sources VALUES
 (70, 40, 60, 1),
 (71, 41, 61, 1);
"""
        )


def test_r2_diagnoses_virtual_parent_gate_without_writing(tmp_path: Path) -> None:
    database = tmp_path / "snapshot.sqlite3"
    _build_database(database)
    sha_before = _sha256(database)
    stat_before = database.stat()

    result = audit_r2_database(database, expected_sha256=sha_before)

    assert result["counts"] == {
        "all_waiting_with_posted_receipt": 1,
        "virtual_parent_false_parent_receipt_gate": 1,
        "automatic_apply_allowed": 0,
    }
    candidate = result["candidates"][0]
    assert candidate["task_facts"] == {
        "task_count": 2,
        "waiting_material_count": 2,
    }
    assert [
        row["projected_component_piece_quantity"]
        for row in candidate["component_facts"]
    ] == [12, 20]
    assert candidate["automatic_apply_allowed"] is False
    assert result["database"]["query_only"] == 1
    assert result["database"]["write_probe_denied"] is True
    assert _sha256(database) == sha_before
    assert database.stat().st_mtime_ns == stat_before.st_mtime_ns
