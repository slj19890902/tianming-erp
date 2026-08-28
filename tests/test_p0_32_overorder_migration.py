from __future__ import annotations

import os
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


ROOT = Path(__file__).resolve().parents[1]
PARENT = "gq52v8x9z41"
TARGET = "gr53v8x9z42"


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    return config


def _isolated_parent_copy(tmp_path: Path) -> Path:
    source = os.environ.get("P032_ISOLATED_BASELINE")
    if not source:
        pytest.skip(
            "P032_ISOLATED_BASELINE is required; P0-32 migration tests only use a copied formal baseline."
        )
    source_path = Path(source).resolve()
    assert source_path.is_file()
    database = tmp_path / "p0-32-isolated.sqlite3"
    with sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True) as source_db:
        source_db.execute("PRAGMA query_only=ON")
        assert source_db.execute("PRAGMA query_only").fetchone()[0] == 1
        version = source_db.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        assert version == PARENT
        with sqlite3.connect(database) as target_db:
            source_db.backup(target_db)
    return database


def _trigger_names(engine) -> set[str]:
    with engine.connect() as connection:
        return {
            str(row[0])
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='trigger'")
            )
        }


def test_p0_32_isolated_upgrade_downgrade_upgrade_preserves_integrity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = _isolated_parent_copy(tmp_path)
    config = _config(monkeypatch, database)
    engine = create_engine(f"sqlite:///{database}")
    allocation_guards_before = {
        name
        for name in _trigger_names(engine)
        if "incoming_receipt_purpose_allocations" in name
    }
    engine.dispose()

    command.upgrade(config, TARGET)
    engine = create_engine(f"sqlite:///{database}")
    inspector = inspect(engine)
    allocation_columns = {
        column["name"]
        for column in inspector.get_columns("incoming_receipt_purpose_allocations")
    }
    assert "surplus_disposition" in allocation_columns
    assert "production_completion_reserve_conversions" in inspector.get_table_names()
    assert (
        "production_completion_reserve_conversion_reversals"
        in inspector.get_table_names()
    )
    trigger_names = _trigger_names(engine)
    assert allocation_guards_before.issubset(trigger_names)
    assert "trg_production_completion_reserve_conversions_immutable_update" in trigger_names
    assert "trg_production_completion_reserve_conversions_immutable_delete" in trigger_names
    assert (
        "trg_production_completion_reserve_conversion_reversals_immutable_update"
        in trigger_names
    )
    assert (
        "trg_production_completion_reserve_conversion_reversals_immutable_delete"
        in trigger_names
    )
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
    engine.dispose()

    command.downgrade(config, PARENT)
    command.upgrade(config, TARGET)
    engine = create_engine(f"sqlite:///{database}")
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
    engine.dispose()


def test_p0_32_conversion_fact_is_immutable_and_blocks_destructive_downgrade(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = _isolated_parent_copy(tmp_path)
    config = _config(monkeypatch, database)
    command.upgrade(config, TARGET)
    engine = create_engine(f"sqlite:///{database}")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.execute(
            text(
                "INSERT INTO production_completion_reserve_conversions ("
                "production_completion_id,receipt_purpose_allocation_id,"
                "semi_finished_inventory_lot_id,finished_inventory_lot_id,"
                "semi_consume_movement_id,finished_adjust_movement_id,"
                "converted_sheet_quantity,finished_quantity_delta,"
                "supported_finished_quantity_before,"
                "supported_finished_quantity_after,idempotency_key,request_hash,created_by"
                ") VALUES (1,1,1,1,98765431,98765432,2,2,600,602,"
                "'p0-32-migration-fact',:request_hash,NULL)"
            ),
            {"request_hash": "a" * 64},
        )
        connection.commit()
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE production_completion_reserve_conversions "
                    "SET converted_sheet_quantity=3 "
                    "WHERE idempotency_key='p0-32-migration-fact'"
                )
            )
    engine.dispose()
    with pytest.raises(RuntimeError, match="拒绝破坏性降级"):
        command.downgrade(config, PARENT)
