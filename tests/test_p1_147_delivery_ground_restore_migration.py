from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "jm71v8x9z60_delivery_ground_occupancy_restore.py"


def _migration_module():
    spec = importlib.util.spec_from_file_location("p1_147_ground_restore", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ground_occupancy_restore_guard_allows_only_the_exact_cancel_inverse() -> None:
    migration = _migration_module()
    db = sqlite3.connect(":memory:")
    db.executescript(
        """
        CREATE TABLE warehouse_ground_occupancies (
          id INTEGER PRIMARY KEY, pallet_id INTEGER NOT NULL,
          primary_location_id INTEGER NOT NULL, customer_id INTEGER NOT NULL,
          product_id INTEGER NOT NULL, footprint_kind TEXT NOT NULL,
          capacity_quantity INTEGER NOT NULL, status TEXT NOT NULL,
          version INTEGER NOT NULL, released_by INTEGER, released_at TEXT
        );
        CREATE TABLE warehouse_ground_occupancy_slots (
          id INTEGER PRIMARY KEY, occupancy_id INTEGER NOT NULL,
          location_id INTEGER NOT NULL, slot_sequence INTEGER NOT NULL,
          status TEXT NOT NULL, released_at TEXT
        );
        """
    )
    db.executescript(migration._occupancy_guard_sql(allow_restore=True))
    db.executescript(migration._slot_guard_sql(allow_restore=True))
    db.execute(
        "INSERT INTO warehouse_ground_occupancies VALUES (1, 10, 20, 30, 40, 'single', 50, 'released', 2, 9, '2026-09-02 13:39:22')"
    )
    db.execute(
        "INSERT INTO warehouse_ground_occupancy_slots VALUES (1, 1, 20, 1, 'released', '2026-09-02 13:39:22')"
    )

    db.execute(
        "UPDATE warehouse_ground_occupancies SET status='active', version=3, released_by=NULL, released_at=NULL WHERE id=1"
    )
    db.execute(
        "UPDATE warehouse_ground_occupancy_slots SET status='active', released_at=NULL WHERE id=1"
    )
    assert db.execute("SELECT status, version, released_by, released_at FROM warehouse_ground_occupancies").fetchone() == ("active", 3, None, None)
    assert db.execute("SELECT status, released_at FROM warehouse_ground_occupancy_slots").fetchone() == ("active", None)

    with pytest.raises(sqlite3.IntegrityError, match="invalid ground occupancy transition"):
        db.execute("UPDATE warehouse_ground_occupancies SET capacity_quantity=99 WHERE id=1")
    with pytest.raises(sqlite3.IntegrityError, match="invalid ground occupancy slot transition"):
        db.execute("UPDATE warehouse_ground_occupancy_slots SET location_id=21 WHERE id=1")
