from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "cj66v8x9z55"
TARGET_REVISION = "ck67v8x9z56"


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "n081-b1-migration-test")
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
            9001, 'n081_b1_test', 'not-used', 'admin', 'N081 B1 Test', 1,
            1, 0, 'all', 'standard'
        )
        """
    )


def _insert_batch(connection: sqlite3.Connection, *, batch_id: int = 1) -> None:
    connection.execute(
        """
        INSERT INTO inventory_onboarding_batches (
            id, batch_number, status, version, source_file_reference,
            source_file_sha256, source_original_filename, source_content_type,
            source_size, source_format, source_encoding, created_by
        ) VALUES (
            ?, ?, 'draft', 1, ?, ?, 'stocktake.csv', 'text/csv',
            128, 'csv', 'utf-8', 9001
        )
        """,
        (
            batch_id,
            f"N081-B1-{batch_id}",
            f"private:inventory_onboarding/{batch_id}.csv",
            f"{batch_id:064x}",
        ),
    )


def _insert_line(
    connection: sqlite3.Connection,
    *,
    line_id: int = 1,
    batch_id: int = 1,
    row_number: int = 2,
) -> None:
    connection.execute(
        """
        INSERT INTO inventory_onboarding_lines (
            id, batch_id, source_sheet_name, source_row_number,
            source_row_hash, raw_row_text, original_values_json
        ) VALUES (?, ?, 'CSV', ?, ?, '原始行', '{"数量":"10"}')
        """,
        (line_id, batch_id, row_number, f"{line_id:064x}"),
    )


def _record_dry_run(connection: sqlite3.Connection, batch_id: int = 1) -> None:
    connection.execute(
        """
        UPDATE inventory_onboarding_batches
        SET dry_run_fingerprint = ?,
            dry_run_summary_json = '{"ready":1}',
            dry_run_by = 9001,
            dry_run_at = '2026-07-23 12:00:00',
            version = version + 1
        WHERE id = ?
        """,
        ("d" * 64, batch_id),
    )


def _submit_batch(connection: sqlite3.Connection, batch_id: int = 1) -> None:
    connection.execute(
        """
        UPDATE inventory_onboarding_batches
        SET status = 'submitted',
            submit_idempotency_key = ?,
            submitted_by = 9001,
            submitted_at = '2026-07-23 12:01:00',
            version = version + 1
        WHERE id = ?
        """,
        (f"submit-{batch_id}", batch_id),
    )


def test_ck67_linearly_descends_from_cj66_on_the_unique_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = ScriptDirectory.from_config(
        _config(monkeypatch, tmp_path / "lineage.sqlite3")
    )

    heads = script.get_heads()
    assert len(heads) == 1
    assert TARGET_REVISION in {
        revision.revision
        for revision in script.iterate_revisions(heads[0], "base")
    }
    assert script.get_revision(TARGET_REVISION).down_revision == PREVIOUS_REVISION


def test_ck67_empty_upgrade_downgrade_upgrade_is_healthy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "n081-b1-roundtrip.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, TARGET_REVISION)

    with sqlite3.connect(database) as connection:
        _health(connection, TARGET_REVISION)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "inventory_onboarding_batches",
            "inventory_onboarding_lines",
        } <= tables
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        assert {
            "trg_inventory_onboarding_batches_insert_guard",
            "trg_inventory_onboarding_batches_source_immutable",
            "trg_inventory_onboarding_batches_update_guard",
            "trg_inventory_onboarding_batches_delete_guard",
            "trg_inventory_onboarding_lines_insert_guard",
            "trg_inventory_onboarding_lines_update_guard",
            "trg_inventory_onboarding_lines_delete_guard",
        } <= triggers

    command.downgrade(config, PREVIOUS_REVISION)
    with sqlite3.connect(database) as connection:
        _health(connection, PREVIOUS_REVISION)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "inventory_onboarding_batches" not in tables
        assert "inventory_onboarding_lines" not in tables

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        _health(connection, TARGET_REVISION)


def test_sqlite_guards_freeze_source_rows_and_submitted_batches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "n081-b1-guards.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, TARGET_REVISION)

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _insert_user(connection)
        _insert_batch(connection)
        _insert_line(connection)

        with pytest.raises(sqlite3.IntegrityError, match="source row"):
            connection.execute(
                """
                UPDATE inventory_onboarding_lines
                SET raw_row_text = '伪造原文', version = version + 1
                WHERE id = 1
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="source file facts"):
            connection.execute(
                """
                UPDATE inventory_onboarding_batches
                SET source_original_filename = 'changed.csv',
                    version = version + 1
                WHERE id = 1
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE inventory_onboarding_lines
                SET remarks = 'missing version increment'
                WHERE id = 1
                """
            )

        connection.execute(
            """
            UPDATE inventory_onboarding_lines
            SET remarks = '允许的草稿修正', version = version + 1
            WHERE id = 1
            """
        )
        _record_dry_run(connection)
        _submit_batch(connection)

        for statement in (
            """
            UPDATE inventory_onboarding_batches
            SET resolved_area_code = 'E', version = version + 1
            WHERE id = 1
            """,
            "DELETE FROM inventory_onboarding_batches WHERE id = 1",
            """
            UPDATE inventory_onboarding_lines
            SET remarks = 'submitted edit', version = version + 1
            WHERE id = 1
            """,
            "DELETE FROM inventory_onboarding_lines WHERE id = 1",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(statement)

        with pytest.raises(sqlite3.IntegrityError, match="draft batch"):
            _insert_line(connection, line_id=2, row_number=3)

        connection.commit()
        _health(connection, TARGET_REVISION)


def test_ck67_downgrade_fails_closed_when_any_draft_fact_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "n081-b1-downgrade.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, TARGET_REVISION)

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _insert_user(connection)
        _insert_batch(connection)
        _insert_line(connection)
        connection.commit()

    with pytest.raises(RuntimeError, match="fail-closed"):
        command.downgrade(config, PREVIOUS_REVISION)

    with sqlite3.connect(database) as connection:
        _health(connection, TARGET_REVISION)
        assert connection.execute(
            "SELECT COUNT(*) FROM inventory_onboarding_batches"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM inventory_onboarding_lines"
        ).fetchone() == (1,)
