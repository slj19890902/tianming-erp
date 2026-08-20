from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from scripts.audit.phase0_d1_r1_candidate_matrix import audit_r1_database, render_markdown


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
CREATE TABLE alembic_version(version_num TEXT NOT NULL);
CREATE TABLE sales_orders(id INTEGER PRIMARY KEY, status TEXT NOT NULL);
CREATE TABLE sales_order_items(
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    delivered_quantity INTEGER NOT NULL,
    is_force_closed INTEGER NOT NULL,
    material_status TEXT NOT NULL,
    special_process TEXT,
    snapshot_splice_mode TEXT,
    snapshot_pieces_per_box INTEGER,
    supply_mode_snapshot TEXT,
    combination_role TEXT,
    is_virtual_composite_parent_snapshot INTEGER
);
CREATE TABLE incoming_receipts(id INTEGER PRIMARY KEY, status TEXT NOT NULL);
CREATE TABLE incoming_receipt_items(
    id INTEGER PRIMARY KEY,
    receipt_id INTEGER NOT NULL,
    order_item_id INTEGER,
    planned_quantity INTEGER NOT NULL,
    received_quantity INTEGER NOT NULL,
    resolution_action TEXT,
    status TEXT NOT NULL
);
CREATE TABLE production_tasks(id INTEGER PRIMARY KEY, order_item_id INTEGER NOT NULL);

INSERT INTO alembic_version VALUES ('vv30v8x9z19');
INSERT INTO sales_orders VALUES (1, 'pending_production');
INSERT INTO sales_order_items VALUES
    (101, 1, 10, 0, 0, 'pending', '一开二', 'single', 1, 'corrugated_production', 'standalone', 0),
    (102, 1, 10, 0, 0, 'pending', '一开一', 'double', 2, 'corrugated_production', 'standalone', 0),
    (103, 1, 10, 0, 0, 'received', '一开一', 'single', 1, 'corrugated_production', 'standalone', 0);
INSERT INTO incoming_receipts VALUES (1, 'posted');
INSERT INTO incoming_receipt_items VALUES
    (201, 1, 101, 5, 6, 'transfer_to_semi_inventory', 'posted'),
    (202, 1, 102, 20, 20, NULL, 'posted');
"""
        )


def test_r1_matrix_converts_units_and_fails_closed_without_writing(tmp_path: Path) -> None:
    database = tmp_path / "snapshot.sqlite3"
    _build_database(database)
    sha_before = _sha256(database)
    stat_before = database.stat()

    result = audit_r1_database(database, expected_sha256=sha_before)

    assert result["counts"] == {
        "normalized_posted_receipt_no_task": 2,
        "legacy_material_status_no_task": 1,
        "automatic_apply_allowed": 0,
    }
    first, second = result["normalized_candidates"]
    assert first["quantity_facts"] == {
        "receipt_sheet_quantity": 6,
        "latest_planned_sheet_quantity": 5,
        "allowed_production_input_sheet_quantity": 5,
        "frozen_cutting_output_factor": 2,
        "frozen_pieces_per_box": 1,
        "projected_finished_box_quantity": 10,
        "order_finished_box_quantity": 10,
        "posted_receipt_line_count": 1,
    }
    assert second["quantity_facts"]["projected_finished_box_quantity"] == 10
    assert all(
        candidate["automatic_apply_allowed"] is False
        for candidate in result["normalized_candidates"] + result["legacy_candidates"]
    )
    assert result["database"]["query_only"] == 1
    assert result["database"]["write_probe_denied"] is True
    assert result["database"]["total_changes_after"] == 0
    assert _sha256(database) == sha_before
    assert database.stat().st_mtime_ns == stat_before.st_mtime_ns

    rendered = json.dumps(result, ensure_ascii=False) + render_markdown(result)
    assert "101" not in rendered
    assert "102" not in rendered
    assert "103" not in rendered
    assert "ITEM-" in rendered

