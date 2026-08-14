from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from scripts.audit.p1_11d0_raw_board_readiness import audit_database, write_outputs


CURRENT_REVISION = "mm21v8x9z10"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _create_schema(path: Path, *, revision: str = CURRENT_REVISION) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            f"""
            PRAGMA foreign_keys=ON;
            CREATE TABLE alembic_version (version_num TEXT NOT NULL);
            INSERT INTO alembic_version VALUES ('{revision}');

            CREATE TABLE customers (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                is_active INTEGER NOT NULL
            );
            CREATE TABLE products (
                id INTEGER PRIMARY KEY,
                customer_id INTEGER NOT NULL,
                product_code TEXT NOT NULL,
                product_name TEXT NOT NULL,
                is_active INTEGER NOT NULL
            );
            CREATE TABLE warehouse_locations (
                id INTEGER PRIMARY KEY,
                location_code TEXT NOT NULL,
                is_active INTEGER NOT NULL
            );
            CREATE TABLE inventory_lots (
                id INTEGER PRIMARY KEY,
                lot_number TEXT,
                inventory_type TEXT NOT NULL,
                warehouse_location_id INTEGER,
                quantity_available INTEGER NOT NULL,
                quantity_reserved INTEGER NOT NULL,
                quantity_consumed INTEGER NOT NULL,
                quantity_damaged INTEGER NOT NULL,
                quantity_scrapped INTEGER NOT NULL,
                unit TEXT,
                status TEXT,
                stock_date TEXT,
                stock_date_accuracy TEXT,
                version INTEGER
            );
            CREATE TABLE semi_finished_inventory_details (
                inventory_lot_id INTEGER PRIMARY KEY,
                owner_customer_id INTEGER,
                material_code_snapshot TEXT,
                normalized_material_code TEXT,
                layer_count INTEGER,
                flute_type TEXT,
                board_length_mm INTEGER,
                board_width_mm INTEGER,
                component_type TEXT,
                pieces_per_box INTEGER,
                stock_yield_per_sheet INTEGER,
                sheet_type TEXT,
                crease_type TEXT,
                crease_left_mm INTEGER,
                crease_middle_mm INTEGER,
                crease_right_mm INTEGER
            );
            CREATE TABLE semi_finished_lot_allowed_products (
                id INTEGER PRIMARY KEY,
                inventory_lot_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL
            );
            CREATE TABLE inventory_pallet_items (
                id INTEGER PRIMARY KEY,
                inventory_lot_id INTEGER,
                item_type TEXT NOT NULL,
                quantity NUMERIC NOT NULL,
                unit TEXT NOT NULL,
                match_status TEXT NOT NULL
            );
            CREATE TABLE inventory_reservations (
                id INTEGER PRIMARY KEY,
                inventory_lot_id INTEGER NOT NULL,
                reserved_stock_quantity INTEGER NOT NULL,
                consumed_stock_quantity INTEGER NOT NULL,
                released_stock_quantity INTEGER NOT NULL,
                status TEXT NOT NULL
            );
            """
        )


def _insert_ready_lot(
    connection: sqlite3.Connection,
    *,
    lot_id: int,
    owner_customer_id: int | None,
    product_id: int | None,
    lot_number: str,
) -> None:
    connection.execute(
        """
        INSERT INTO inventory_lots VALUES (
            ?, ?, 'semi_finished', 1, 80, 0, 0, 0, 0,
            'sheets', 'active', '2026-08-14', 'exact', 1
        )
        """,
        (lot_id, lot_number),
    )
    connection.execute(
        """
        INSERT INTO semi_finished_inventory_details VALUES (
            ?, ?, 'K=A', 'K=A', 3, 'B', 1200, 800,
            'whole', 1, 1, 'raw_board', '毛片', NULL, NULL, NULL
        )
        """,
        (lot_id, owner_customer_id),
    )
    if product_id is not None:
        connection.execute(
            "INSERT INTO semi_finished_lot_allowed_products VALUES (?, ?, ?)",
            (lot_id, lot_id, product_id),
        )


def test_zero_raw_board_has_no_completeness_denominator_and_is_read_only(
    tmp_path: Path,
) -> None:
    database = tmp_path / "zero.sqlite3"
    _create_schema(database, revision="cr74v8x9z63")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO warehouse_locations VALUES (1, 'PRIVATE-A1', 1)"
        )
        connection.execute(
            """
            INSERT INTO inventory_lots VALUES (
                1, 'PRIVATE-FINISHED-LOT', 'finished', 1, 10, 0, 0, 0, 0,
                'boxes', 'active', '2026-07-27', 'exact', 1
            )
            """
        )
        connection.executemany(
            "INSERT INTO inventory_pallet_items VALUES (?, NULL, 'semi_finished', ?, 'sheets', 'matched')",
            [(1, 30), (2, 40)],
        )
    before = _sha256(database)

    report = audit_database(
        database,
        expected_sha256=before,
        expected_revision=CURRENT_REVISION,
    )

    assert _sha256(database) == before
    assert report["audit"]["database_written"] is False
    assert report["audit"]["connection_mode"] == "sqlite_mode_ro_query_only"
    assert report["audit"]["database_changed_during_scan"] is False
    assert report["summary"]["formal_raw_board_lots"] == 0
    assert report["summary"]["field_completeness"] == {
        "complete": 0,
        "denominator": 0,
        "percent": None,
        "semantics": "no_formal_raw_board_denominator",
    }
    assert report["summary"]["unlinked_historical_semi_items"] == 2
    assert report["summary"]["unlinked_historical_semi_quantity"] == 70
    assert report["gate"]["status"] == "blocked"
    assert report["gate"]["checks"]["revision_matches_current"] is False
    assert "PRIVATE-FINISHED-LOT" not in json.dumps(report, ensure_ascii=False)


def test_anonymous_customer_specific_and_general_fixture_can_pass(
    tmp_path: Path,
) -> None:
    database = tmp_path / "ready.sqlite3"
    _create_schema(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO warehouse_locations VALUES (1, 'SECRET-LOCATION', 1)"
        )
        connection.execute(
            "INSERT INTO customers VALUES (10, 'SECRET-CUSTOMER-NAME', 1)"
        )
        connection.execute(
            "INSERT INTO products VALUES (20, 10, 'SECRET-CODE', 'SECRET-PRODUCT', 1)"
        )
        _insert_ready_lot(
            connection,
            lot_id=1,
            owner_customer_id=10,
            product_id=20,
            lot_number="SECRET-DEDICATED-LOT",
        )
        _insert_ready_lot(
            connection,
            lot_id=2,
            owner_customer_id=None,
            product_id=None,
            lot_number="SECRET-GENERAL-LOT",
        )
    before = _sha256(database)

    report = audit_database(
        database,
        expected_sha256=before,
        expected_revision=CURRENT_REVISION,
    )
    serialized = json.dumps(report, ensure_ascii=False)

    assert report["gate"]["status"] == "passed"
    assert report["summary"]["ready_customer_specific_lots"] == 1
    assert report["summary"]["ready_general_lots"] == 1
    assert report["summary"]["field_completeness"]["percent"] == 100.0
    assert [row["anonymous_lot"] for row in report["anonymous_candidates"]] == [
        "raw-board-001",
        "raw-board-002",
    ]
    for secret in (
        "SECRET-CUSTOMER-NAME",
        "SECRET-CODE",
        "SECRET-PRODUCT",
        "SECRET-DEDICATED-LOT",
        "SECRET-GENERAL-LOT",
        "SECRET-LOCATION",
    ):
        assert secret not in serialized


def test_missing_binding_and_unavailable_facts_are_fail_closed_and_aggregated(
    tmp_path: Path,
) -> None:
    database = tmp_path / "blocked.sqlite3"
    _create_schema(database)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO warehouse_locations VALUES (1, 'A1', 1)")
        connection.execute("INSERT INTO customers VALUES (10, 'C', 1)")
        _insert_ready_lot(
            connection,
            lot_id=1,
            owner_customer_id=10,
            product_id=None,
            lot_number="BINDING-MISSING",
        )
        _insert_ready_lot(
            connection,
            lot_id=2,
            owner_customer_id=None,
            product_id=None,
            lot_number="FROZEN-DAMAGED",
        )
        connection.execute(
            "UPDATE inventory_lots SET status='frozen', quantity_damaged=2 WHERE id=2"
        )

    report = audit_database(
        database,
        expected_revision=CURRENT_REVISION,
    )

    assert report["gate"]["status"] == "blocked"
    assert report["summary"]["ready_customer_specific_lots"] == 0
    assert report["summary"]["ready_general_lots"] == 0
    assert report["reason_counts"] == {
        "dedicated_missing_valid_product_binding": 1,
        "has_damaged_quantity": 1,
        "lot_status_not_active": 1,
    }
    assert report["anonymous_candidates"][0]["missing_or_invalid"] == [
        "dedicated_missing_valid_product_binding"
    ]
    assert report["anonymous_candidates"][1]["business_blockers"] == [
        "has_damaged_quantity",
        "lot_status_not_active",
    ]


def test_reserved_balance_insufficient_is_distinct_from_empty_unreserved_lot(
    tmp_path: Path,
) -> None:
    database = tmp_path / "reserved.sqlite3"
    _create_schema(database)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO warehouse_locations VALUES (1, 'A1', 1)")
        _insert_ready_lot(
            connection,
            lot_id=1,
            owner_customer_id=None,
            product_id=None,
            lot_number="RESERVED",
        )
        connection.execute(
            "UPDATE inventory_lots SET quantity_available=0, quantity_reserved=80 WHERE id=1"
        )

    report = audit_database(database, expected_revision=CURRENT_REVISION)

    assert report["reason_counts"] == {"reserved_balance_insufficient": 1}
    assert report["anonymous_candidates"][0]["business_blockers"] == [
        "reserved_balance_insufficient"
    ]


def test_expected_database_hash_mismatch_is_rejected_before_audit(
    tmp_path: Path,
) -> None:
    database = tmp_path / "hash.sqlite3"
    _create_schema(database)

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        audit_database(database, expected_sha256="0" * 64)


def test_incomplete_schema_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "incomplete.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE inventory_lots (id INTEGER PRIMARY KEY)")

    with pytest.raises(ValueError, match="missing tables"):
        audit_database(database)


def test_outputs_are_aggregate_only_and_record_blocked_gate(tmp_path: Path) -> None:
    database = tmp_path / "output.sqlite3"
    output = tmp_path / "report"
    _create_schema(database)

    report = audit_database(database, expected_revision=CURRENT_REVISION)
    written = write_outputs(report, output)

    markdown = Path(written["markdown"]).read_text(encoding="utf-8")
    payload = json.loads(Path(written["json"]).read_text(encoding="utf-8"))
    assert payload["gate"]["status"] == "blocked"
    assert "0 条正式 raw_board" in markdown
    assert "完整率不是 0%" in markdown
    assert "数据库写入：`false`" in markdown
