from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from scripts.audit.phase0_d1_r3_completion_trace_matrix import audit_r3_database


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
CREATE TABLE sales_orders(id INTEGER PRIMARY KEY, status TEXT);
CREATE TABLE sales_order_items(
 id INTEGER PRIMARY KEY, order_id INTEGER, quantity INTEGER,
 delivered_quantity INTEGER, is_force_closed INTEGER
);
CREATE TABLE production_completions(
 id INTEGER PRIMARY KEY, task_id INTEGER, order_item_id INTEGER, status TEXT,
 inventory_lot_id INTEGER, initial_disposition TEXT, actual_output_quantity INTEGER,
 direct_delivery_quantity INTEGER, stock_quantity INTEGER, order_reserved_quantity INTEGER
);
CREATE TABLE inventory_lots(
 id INTEGER PRIMARY KEY, source_ref_type TEXT, source_ref_id INTEGER
);
CREATE TABLE inventory_movements(
 id INTEGER PRIMARY KEY, movement_type TEXT, related_order_item_id INTEGER
);
CREATE TABLE sales_deliveries(id INTEGER PRIMARY KEY, status TEXT);
CREATE TABLE sales_delivery_items(
 id INTEGER PRIMARY KEY, delivery_id INTEGER, order_item_id INTEGER,
 delivered_quantity INTEGER
);
CREATE TABLE delivery_inventory_allocations(
 id INTEGER PRIMARY KEY, delivery_item_id INTEGER, consumed_stock_quantity INTEGER,
 reversed_stock_quantity INTEGER, consume_movement_id INTEGER
);
CREATE TABLE bom_component_direct_delivery_allocations(
 id INTEGER PRIMARY KEY, production_completion_id INTEGER,
 consumed_quantity INTEGER, reversed_quantity INTEGER
);
INSERT INTO sales_orders VALUES
 (1, 'delivered'), (2, 'pending_delivery'), (3, 'delivered'), (4, 'delivered');
INSERT INTO sales_order_items VALUES
 (10, 1, 100, 100, 0),
 (20, 2, 50, 0, 0),
 (30, 3, 100, 100, 0),
 (40, 4, 100, 100, 0);
INSERT INTO production_completions VALUES
 (101, 1, 10, 'posted', NULL, 'direct', 100, 100, 0, 100),
 (102, 2, 20, 'posted', NULL, 'direct', 50, 50, 0, 50),
 (103, 3, 30, 'posted', NULL, 'direct', 80, 80, 0, 80),
 (104, 4, 40, 'posted', NULL, 'direct', 70, 70, 0, 70);
INSERT INTO sales_deliveries VALUES
 (201, 'dispatched'), (202, 'dispatched'), (203, 'dispatched');
INSERT INTO sales_delivery_items VALUES
 (301, 201, 10, 100),
 (302, 202, 30, 100),
 (303, 203, 40, 100);
INSERT INTO delivery_inventory_allocations VALUES (401, 302, 20, 0, 501);
INSERT INTO bom_component_direct_delivery_allocations VALUES (402, 104, 60, 0);
"""
        )


def test_r3_classifies_trace_stocktake_and_stop_without_writing(tmp_path: Path) -> None:
    database = tmp_path / "snapshot.sqlite3"
    _build_database(database)
    sha_before = _sha256(database)
    stat_before = database.stat()

    result = audit_r3_database(database, expected_sha256=sha_before)

    assert result["counts"] == {
        "completion_trace_gaps": 4,
        "trace_reconstruction_after_delivery_confirmation": 2,
        "physical_stocktake_required_before_trace": 1,
        "quantity_contradiction_stop": 1,
        "automatic_apply_allowed": 0,
    }
    assert [row["action_class"] for row in result["candidates"]] == [
        "trace_reconstruction_after_delivery_confirmation",
        "physical_stocktake_required_before_trace",
        "trace_reconstruction_after_delivery_confirmation",
        "quantity_contradiction_stop",
    ]
    assert all(row["automatic_apply_allowed"] is False for row in result["candidates"])
    assert result["database"]["query_only"] == 1
    assert result["database"]["write_probe_denied"] is True
    assert _sha256(database) == sha_before
    assert database.stat().st_mtime_ns == stat_before.st_mtime_ns
