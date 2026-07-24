from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "ck67v8x9z56"
TARGET_REVISION = "cm69v8x9z58"
BATCH_ID = 9600
USER_ID = 9601


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "n081-posting-migration-test")
    return Config(str(PROJECT_ROOT / "alembic.ini"))


def _health(connection: sqlite3.Connection, revision: str) -> None:
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute(
        "SELECT version_num FROM alembic_version"
    ).fetchone() == (revision,)


def _insert_user(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO users (
            id, username, password_hash, role, real_name, is_active,
            auth_version, must_change_password, customer_access_mode, ui_mode
        ) VALUES (
            ?, 'n081_posting_test', 'not-used', 'admin',
            'N081 Posting Test', 1, 1, 0, 'all', 'standard'
        )
        """,
        (USER_ID,),
    )


def _insert_submitted_batch(
    connection: sqlite3.Connection,
) -> list[dict[str, int | str]]:
    connection.execute(
        """
        INSERT INTO inventory_onboarding_batches (
            id, batch_number, status, version, source_file_reference,
            source_file_sha256, source_original_filename, source_content_type,
            source_size, source_format, source_encoding, resolved_floor,
            resolved_area_code, created_by
        ) VALUES (
            ?, 'N081-POSTING-SOURCE', 'draft', 1,
            'private:inventory_onboarding/n081-posting.csv', ?,
            'n081-posting.csv', 'text/csv', 1024, 'csv', 'utf-8',
            3, 'E1', ?
        )
        """,
        (BATCH_ID, "b" * 64, USER_ID),
    )

    rows: list[dict[str, int | str]] = []
    for offset, (inventory_type, unit) in enumerate(
        (("finished", "boxes"), ("semi_finished", "sheets")),
        start=1,
    ):
        line_id = 9700 + offset
        location_id = 9800 + offset
        location_code = f"N081-POST-E1-{offset:02d}"
        pallet_code = f"PLT-N081-POST-{offset:02d}"
        connection.execute(
            """
            INSERT INTO warehouse_locations (
                id, location_code, location_name, warehouse_type, is_active,
                warehouse_floor, area_code, storage_type, placement_status
            ) VALUES (?, ?, ?, ?, 1, 3, 'E1', 'ground', 'placed')
            """,
            (
                location_id,
                location_code,
                f"N081 Posting {location_code}",
                inventory_type,
            ),
        )
        connection.execute(
            """
            INSERT INTO inventory_onboarding_lines (
                id, batch_id, source_sheet_name, source_row_number,
                source_row_hash, raw_row_text, original_values_json,
                inventory_type, ownership_type, location_id,
                location_code_snapshot, floor_snapshot, area_code_snapshot,
                pallet_code, quantity, unit, stock_date,
                stock_date_accuracy, action_decision, match_status
            ) VALUES (
                ?, ?, 'CSV', ?, ?, '原始盘点行', '{"数量":"10"}',
                ?, 'general', ?, ?, 3, 'E1', ?, 10, ?,
                '2026-07-24', 'exact', 'create_new', 'ready'
            )
            """,
            (
                line_id,
                BATCH_ID,
                offset + 1,
                f"{line_id:064x}",
                inventory_type,
                location_id,
                location_code,
                pallet_code,
                unit,
            ),
        )
        rows.append(
            {
                "line_id": line_id,
                "location_id": location_id,
                "inventory_type": inventory_type,
                "unit": unit,
            }
        )

    connection.execute(
        """
        UPDATE inventory_onboarding_batches
        SET dry_run_fingerprint = ?,
            dry_run_summary_json = '{"create_new_rows":2,"error_count":0}',
            dry_run_by = ?,
            dry_run_at = '2026-07-24 08:00:00',
            version = version + 1
        WHERE id = ?
        """,
        ("d" * 64, USER_ID, BATCH_ID),
    )
    connection.execute(
        """
        UPDATE inventory_onboarding_batches
        SET status = 'submitted',
            submit_idempotency_key = 'n081-posting-source-submit',
            submitted_by = ?,
            submitted_at = '2026-07-24 08:01:00',
            version = version + 1
        WHERE id = ?
        """,
        (USER_ID, BATCH_ID),
    )
    return rows


def _insert_source_lot(
    connection: sqlite3.Connection,
    source: dict[str, int | str],
    *,
    lot_id: int,
    quantity: int = 10,
) -> None:
    connection.execute(
        """
        INSERT INTO inventory_lots (
            id, lot_number, inventory_type, warehouse_location_id,
            quantity_available, quantity_reserved, quantity_consumed,
            quantity_damaged, quantity_scrapped, unit, status, source_type,
            source_ref_type, source_ref_id, stock_date,
            stock_date_accuracy, last_movement_at, version, created_by
        ) VALUES (
            ?, ?, ?, ?, ?, 0, 0, 0, 0, ?, 'active', 'stocktake',
            'inventory_onboarding_line', ?, '2026-07-24', 'exact',
            '2026-07-24 08:02:00', 1, ?
        )
        """,
        (
            lot_id,
            f"LOT-N081-POST-{lot_id}",
            source["inventory_type"],
            source["location_id"],
            quantity,
            source["unit"],
            source["line_id"],
            USER_ID,
        ),
    )


def _insert_posting(connection: sqlite3.Connection) -> int:
    return int(
        connection.execute(
            """
            INSERT INTO inventory_onboarding_postings (
                posting_number, onboarding_batch_id,
                onboarding_batch_version, onboarding_batch_fingerprint,
                posting_fingerprint, idempotency_key, floor_snapshot,
                area_code_snapshot, line_count, finished_line_count,
                semi_finished_line_count, evidence_json, posted_by, posted_at
            ) VALUES (
                'N081-POSTING-001', ?, 3, ?, ?,
                'n081-posting-migration-receipt', '3', 'E1',
                2, 1, 1, '{"lot_ids":[9901,9902]}',
                ?, '2026-07-24 08:03:00'
            )
            """,
            (BATCH_ID, "d" * 64, "f" * 64, USER_ID),
        ).lastrowid
    )


def test_cm69_is_the_only_head_and_linearly_descends_from_ck67(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = ScriptDirectory.from_config(
        _config(monkeypatch, tmp_path / "lineage.sqlite3")
    )

    assert script.get_heads() == [TARGET_REVISION]
    assert script.get_revision(TARGET_REVISION).down_revision == PREVIOUS_REVISION


def test_ck67_to_cm69_roundtrip_is_healthy_and_installs_expected_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "n081-posting-roundtrip.sqlite3"
    config = _config(monkeypatch, database)

    command.upgrade(config, PREVIOUS_REVISION)
    with sqlite3.connect(database) as connection:
        _health(connection, PREVIOUS_REVISION)
        assert connection.execute(
            """
            SELECT COUNT(*) FROM sqlite_master
            WHERE type = 'table' AND name = 'inventory_onboarding_postings'
            """
        ).fetchone() == (0,)

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        _health(connection, TARGET_REVISION)
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(inventory_onboarding_postings)"
            )
        }
        assert {
            "id",
            "posting_number",
            "onboarding_batch_id",
            "onboarding_batch_version",
            "onboarding_batch_fingerprint",
            "posting_fingerprint",
            "idempotency_key",
            "line_count",
            "finished_line_count",
            "semi_finished_line_count",
            "evidence_json",
            "posted_by",
            "posted_at",
        } <= columns

        index_sql = connection.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'index'
              AND name = 'uq_inventory_lots_onboarding_line_source'
            """
        ).fetchone()[0]
        assert "UNIQUE INDEX" in index_sql
        assert "source_ref_type" in index_sql
        assert "source_ref_id" in index_sql
        assert (
            "WHERE source_ref_type = 'inventory_onboarding_line'"
            in index_sql
        )
        assert "source_ref_id IS NOT NULL" in index_sql

        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        assert {
            "trg_inventory_lots_onboarding_source_insert_guard",
            "trg_inventory_lots_onboarding_source_update_guard",
            "trg_inventory_lots_onboarding_source_delete_guard",
            "trg_inventory_onboarding_postings_insert_guard",
            "trg_inventory_onboarding_postings_update_guard",
            "trg_inventory_onboarding_postings_delete_guard",
        } <= triggers

    command.downgrade(config, PREVIOUS_REVISION)
    with sqlite3.connect(database) as connection:
        _health(connection, PREVIOUS_REVISION)
        assert connection.execute(
            """
            SELECT COUNT(*) FROM sqlite_master
            WHERE type = 'table' AND name = 'inventory_onboarding_postings'
            """
        ).fetchone() == (0,)
        assert connection.execute(
            """
            SELECT COUNT(*) FROM sqlite_master
            WHERE type = 'index'
              AND name = 'uq_inventory_lots_onboarding_line_source'
            """
        ).fetchone() == (0,)

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        _health(connection, TARGET_REVISION)


def test_sqlite_guards_require_a_complete_batch_and_keep_receipts_immutable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "n081-posting-guards.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, TARGET_REVISION)

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _insert_user(connection)
        rows = _insert_submitted_batch(connection)

        with pytest.raises(sqlite3.IntegrityError, match="eligible source"):
            _insert_source_lot(connection, rows[0], lot_id=9900, quantity=9)

        with pytest.raises(sqlite3.IntegrityError, match="complete formal batch"):
            _insert_posting(connection)

        _insert_source_lot(connection, rows[0], lot_id=9901)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_source_lot(connection, rows[0], lot_id=9991)
        _insert_source_lot(connection, rows[1], lot_id=9902)

        posting_id = _insert_posting(connection)
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError, match="receipt is immutable"):
            connection.execute(
                """
                UPDATE inventory_onboarding_postings
                SET line_count = line_count
                WHERE id = ?
                """,
                (posting_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="receipt is immutable"):
            connection.execute(
                "DELETE FROM inventory_onboarding_postings WHERE id = ?",
                (posting_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="source is immutable"):
            connection.execute(
                """
                UPDATE inventory_lots
                SET source_ref_id = ?
                WHERE id = 9901
                """,
                (rows[1]["line_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            connection.execute("DELETE FROM inventory_lots WHERE id = 9901")

        assert connection.execute(
            "SELECT COUNT(*) FROM inventory_onboarding_postings"
        ).fetchone() == (1,)
        assert connection.execute(
            """
            SELECT COUNT(*) FROM inventory_lots
            WHERE source_ref_type = 'inventory_onboarding_line'
            """
        ).fetchone() == (2,)
        _health(connection, TARGET_REVISION)
