from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "ix59v8x9z48"
TARGET_REVISION = "iy60v8x9z49"
TABLE = "finance_delivery_material_cost_facts"


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "p1-131-material-cost-migration-test")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return config


def _prepare_parent_schema(
    monkeypatch: pytest.MonkeyPatch, database: Path
) -> Config:
    from app.core.database import create_sqlite_engine
    from app.models import Base

    engine = create_sqlite_engine(database)
    Base.metadata.create_all(engine)
    engine.dispose()
    with sqlite3.connect(database) as connection:
        connection.execute(f"DROP TABLE {TABLE}")
        connection.commit()
    config = _config(monkeypatch, database)
    command.stamp(config, PREVIOUS_REVISION)
    return config


def _health(connection: sqlite3.Connection, revision: str) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    assert connection.execute(
        "SELECT version_num FROM alembic_version"
    ).fetchone() == (revision,)
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_delivery_material_cost_migration_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = tmp_path / "p1-131-material-cost-round-trip.sqlite3"
    config = _prepare_parent_schema(monkeypatch, database)
    script = ScriptDirectory.from_config(config)
    assert script.get_revision(TARGET_REVISION).down_revision == PREVIOUS_REVISION

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        _health(connection, TARGET_REVISION)
        columns = {
            row[1] for row in connection.execute(f"PRAGMA table_info({TABLE})")
        }
        assert {
            "source_kind",
            "snapshot_version",
            "incoming_receipt_purpose_allocation_id",
            "purchase_receipt_fact_id",
            "unit_material_cost",
            "total_material_cost",
            "source_fingerprint",
        } <= columns
        assert connection.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone() == (
            0,
        )

    command.downgrade(config, PREVIOUS_REVISION)
    with sqlite3.connect(database) as connection:
        _health(connection, PREVIOUS_REVISION)
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (TABLE,),
        ).fetchone() == (0,)

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        _health(connection, TARGET_REVISION)


def test_delivery_material_cost_facts_are_immutable_and_block_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = tmp_path / "p1-131-material-cost-guard.sqlite3"
    config = _prepare_parent_schema(monkeypatch, database)
    command.upgrade(config, TARGET_REVISION)

    with sqlite3.connect(database) as connection:
        connection.execute(
            f"""
            INSERT INTO {TABLE} (
                source_kind, snapshot_version, delivery_id, delivery_item_id,
                delivery_inventory_allocation_id, inventory_lot_id,
                incoming_receipt_purpose_allocation_id,
                purchase_receipt_fact_id, consumed_quantity,
                quantity_unit_snapshot, unit_material_cost,
                total_material_cost, currency_snapshot,
                tax_included_snapshot, tax_rate_snapshot,
                source_fingerprint
            ) VALUES (
                'inventory_allocation', 1, 1, 1, 1, 1, 1, 1, 10,
                'boxes', 1.250000, 12.500000, 'CNY', 1, 0.130000,
                ?
            )
            """,
            ("a" * 64,),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                f"UPDATE {TABLE} SET total_material_cost=99 WHERE id=1"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(f"DELETE FROM {TABLE} WHERE id=1")

    with pytest.raises(RuntimeError, match="frozen delivery material cost facts"):
        command.downgrade(config, PREVIOUS_REVISION)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (TARGET_REVISION,)
        assert connection.execute(
            f"SELECT total_material_cost FROM {TABLE} WHERE id=1"
        ).fetchone() == (12.5,)
