from __future__ import annotations

from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "sp28v8x9z90"
TARGET_REVISION = "sq29v8x9z91"
MIGRATION = (
    ROOT
    / "alembic"
    / "versions"
    / "sq29v8x9z91_ai_inventory_run_ledger.py"
)


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option(
        "sqlalchemy.url",
        f"sqlite:///{database.as_posix()}",
    )
    return config


def _health(database: Path, revision: str) -> None:
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (revision,)


def test_ai_ledger_migration_is_linear_constrained_and_business_write_free() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    compile(source, str(MIGRATION), "exec")
    assert 'revision = "sq29v8x9z91"' in source
    assert 'down_revision = "sp28v8x9z90"' in source
    for table in (
        "ai_analysis_runs",
        "ai_analysis_feedback",
        "ai_usage_ledger",
    ):
        assert table in source
    for constraint in (
        "ck_ai_analysis_runs_status",
        "uq_ai_analysis_runs_user_idempotency",
        "uq_ai_analysis_feedback_run_user",
        "uq_ai_usage_ledger_run",
    ):
        assert constraint in source
    for forbidden in (
        "UPDATE sales_",
        "UPDATE inventory_",
        "INSERT INTO sales_",
        "INSERT INTO inventory_",
        "DELETE FROM sales_",
        "DELETE FROM inventory_",
    ):
        assert forbidden not in source


def test_ai_ledger_empty_database_round_trip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "ai-ledger-round-trip.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, PARENT_REVISION)
    command.upgrade(config, TARGET_REVISION)
    _health(database, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "ai_analysis_runs",
            "ai_analysis_feedback",
            "ai_usage_ledger",
        } <= tables

    command.downgrade(config, PARENT_REVISION)
    _health(database, PARENT_REVISION)
    command.upgrade(config, TARGET_REVISION)
    _health(database, TARGET_REVISION)


def test_ai_ledger_downgrade_is_blocked_after_first_run_fact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "ai-ledger-fail-closed.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO users (
                username, password_hash, role, real_name, is_active,
                auth_version, must_change_password, customer_access_mode,
                ui_mode
            ) VALUES (
                'ai-migration-user', 'not-used', 'admin', '迁移测试',
                1, 1, 0, 'all', 'standard'
            )
            """
        )
        user_id = connection.execute(
            "SELECT id FROM users WHERE username = 'ai-migration-user'"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO ai_analysis_runs (
                public_id, analysis_type, status, requested_by, as_of_date,
                scope_json, idempotency_key, prompt_version, provider_code,
                model_code, input_hash, sanitized_snapshot_json,
                input_tokens, output_tokens, provider_cost_estimate
            ) VALUES (
                '00000000-0000-0000-0000-000000000001',
                'inventory_insight', 'degraded', ?, '2026-07-26',
                '{"customer_access_mode":"all"}', 'migration-fact',
                'ai.inventory.insight.zh-cn.v1', 'disabled', 'disabled',
                ?, '{}', 0, 0, 0
            )
            """,
            (user_id, "0" * 64),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="refusing to remove"):
        command.downgrade(config, PARENT_REVISION)
    _health(database, TARGET_REVISION)
