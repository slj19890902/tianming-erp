from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "iv57v8x9z46"
TARGET_REVISION = "iw58v8x9z47"
COST_TABLES = {"finance_cost_centers", "finance_cost_pool_entries"}
DEFAULT_CENTERS = [
    ("PROD", "生产成本", "production", 1, 1),
    ("WH_DELIVERY", "仓储配送", "warehouse_delivery", 1, 1),
    ("SALES", "销售费用", "sales", 1, 1),
    ("ADMIN", "管理费用", "administration", 1, 1),
    ("FINANCE", "财务费用", "finance", 1, 1),
    ("UNALLOCATED", "待分类", "unallocated", 1, 1),
]


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "p1-131-cost-pool-migration-test")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return config


def _prepare_previous_schema(
    monkeypatch: pytest.MonkeyPatch,
    database: Path,
) -> Config:
    """Build the current baseline schema without opening a formal database.

    The historical migration chain contains deliberate data preflights that
    cannot be satisfied by an empty database.  Current ORM metadata matches the
    immediate parent schema apart from the two P1-131 tables, so create the
    isolated schema, remove those target tables, and stamp the exact parent
    before exercising the real hu56 upgrade/downgrade functions.
    """

    from app.core.database import create_sqlite_engine
    from app.models import Base

    engine = create_sqlite_engine(database)
    Base.metadata.create_all(engine)
    engine.dispose()
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE finance_cost_pool_entries")
        connection.execute("DROP TABLE finance_cost_centers")
        connection.commit()
    config = _config(monkeypatch, database)
    command.stamp(config, PREVIOUS_REVISION)
    return config


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _assert_health(connection: sqlite3.Connection, revision: str) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    assert connection.execute(
        "SELECT version_num FROM alembic_version"
    ).fetchone() == (revision,)
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _center_rows(connection: sqlite3.Connection) -> list[tuple]:
    return connection.execute(
        """
        SELECT code, name, center_type, is_active, version
        FROM finance_cost_centers
        ORDER BY id
        """
    ).fetchall()


def test_p1_131_cost_pool_migration_upgrade_downgrade_upgrade_round_trip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p1-131-cost-pool-round-trip.sqlite3"
    config = _prepare_previous_schema(monkeypatch, database)
    script = ScriptDirectory.from_config(config)
    assert script.get_revision(TARGET_REVISION).down_revision == PREVIOUS_REVISION

    with sqlite3.connect(database) as connection:
        _assert_health(connection, PREVIOUS_REVISION)
        assert COST_TABLES.isdisjoint(_tables(connection))

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        _assert_health(connection, TARGET_REVISION)
        assert COST_TABLES <= _tables(connection)
        assert _center_rows(connection) == DEFAULT_CENTERS
        assert connection.execute(
            "SELECT COUNT(*) FROM finance_cost_pool_entries"
        ).fetchone() == (0,)

    command.downgrade(config, PREVIOUS_REVISION)
    with sqlite3.connect(database) as connection:
        _assert_health(connection, PREVIOUS_REVISION)
        assert COST_TABLES.isdisjoint(_tables(connection))

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        _assert_health(connection, TARGET_REVISION)
        assert COST_TABLES <= _tables(connection)
        assert _center_rows(connection) == DEFAULT_CENTERS
        assert connection.execute(
            "SELECT COUNT(*) FROM finance_cost_pool_entries"
        ).fetchone() == (0,)


def test_p1_131_cost_pool_facts_block_destructive_downgrade(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p1-131-cost-pool-downgrade-guard.sqlite3"
    config = _prepare_previous_schema(monkeypatch, database)
    command.upgrade(config, TARGET_REVISION)

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        center_id = connection.execute(
            "SELECT id FROM finance_cost_centers WHERE code = 'PROD'"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO finance_cost_pool_entries (
                cost_month, document_date, cost_center_id,
                cost_center_code_snapshot, cost_center_name_snapshot,
                cost_center_type_snapshot, cost_category, accounting_class,
                allocation_basis, description, amount, tax_amount,
                source_type, source_reference, status, version
            ) VALUES (
                '2026-08', '2026-08-31', ?,
                'PROD', '生产成本', 'production',
                'production_wages', 'manufacturing', 'unallocated',
                'P1-131 降级保护事实', 100.00, 0.00,
                'manual', 'P1-131-DOWNGRADE-GUARD', 'draft', 1
            )
            """,
            (center_id,),
        )
        connection.commit()
        _assert_health(connection, TARGET_REVISION)

    with pytest.raises(
        RuntimeError,
        match="finance cost pool facts would be lost",
    ):
        command.downgrade(config, PREVIOUS_REVISION)

    with sqlite3.connect(database) as connection:
        _assert_health(connection, TARGET_REVISION)
        assert COST_TABLES <= _tables(connection)
        assert connection.execute(
            "SELECT source_reference, amount, status "
            "FROM finance_cost_pool_entries"
        ).fetchone() == (
            "P1-131-DOWNGRADE-GUARD",
            100,
            "draft",
        )
