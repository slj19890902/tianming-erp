from __future__ import annotations

from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect

from app.core.database import create_sqlite_engine


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "mm21v8x9z10"
TARGET_REVISION = "nn22v8x9z11"
TABLE = "supplier_minimum_order_rules"


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _checks(path: Path) -> tuple[str, int]:
    with sqlite3.connect(path) as connection:
        return (
            connection.execute("PRAGMA integrity_check").fetchone()[0],
            len(connection.execute("PRAGMA foreign_key_check").fetchall()),
        )


def _tables(path: Path) -> set[str]:
    engine = create_sqlite_engine(path)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_model_and_migration_contract_are_linear() -> None:
    migration = ROOT / "alembic/versions/nn22v8x9z11_supplier_minimum_order_rules.py"
    source = migration.read_text(encoding="utf-8")
    assert 'revision = "nn22v8x9z11"' in source
    assert 'down_revision = "mm21v8x9z10"' in source
    assert "拒绝破坏性降级" in source

    from app.models import Base

    table = Base.metadata.tables[TABLE]
    assert {
        "supplier_id", "supplier_name_snapshot", "scope_type", "scope_value",
        "scope_label_snapshot", "minimum_quantity", "unit", "merge_allowed",
        "merge_window_days", "effective_from", "effective_to", "status",
        "source", "evidence_reference", "confirmed_by_user_id",
        "confirmed_by_username_snapshot", "confirmed_at", "version",
    } <= set(table.c.keys())


def test_upgrade_downgrade_upgrade_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "p1-11d1-roundtrip.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET_REVISION)
    assert TABLE in _tables(path)
    assert _checks(path) == ("ok", 0)
    command.downgrade(config, PARENT_REVISION)
    assert TABLE not in _tables(path)
    assert _checks(path) == ("ok", 0)
    command.upgrade(config, TARGET_REVISION)
    assert TABLE in _tables(path)
    assert _checks(path) == ("ok", 0)

def test_downgrade_fails_closed_after_rule_fact(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "p1-11d1-fail-closed.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        supplier_id = connection.execute(
            "SELECT id FROM supplier_master_records ORDER BY id LIMIT 1"
        ).fetchone()[0]
        connection.execute(
            f"""
            INSERT INTO {TABLE}(
                supplier_id,supplier_name_snapshot,scope_type,scope_label_snapshot,
                minimum_quantity,unit,merge_allowed,merge_window_days,
                effective_from,status,source,evidence_reference,
                confirmed_by_username_snapshot,version
            ) VALUES (?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?)
            """,
            (
                supplier_id, "匿名供应商", "supplier", "全部", 100, "sheets",
                1, 7, "2026-08-14", "active", "manual_confirmation",
                "匿名确认单", "anonymous-admin", 1,
            ),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="拒绝破坏性降级"):
        command.downgrade(config, PARENT_REVISION)
    assert TABLE in _tables(path)
    assert _checks(path) == ("ok", 0)
