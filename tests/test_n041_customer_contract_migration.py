from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect

from app.core.database import create_sqlite_engine


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "co71v8x9z60"
TARGET_REVISION = "cp72v8x9z61"


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _checks(path: Path) -> tuple[str, int]:
    with sqlite3.connect(path) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    return integrity, foreign_keys


def test_n041_migration_is_linear_and_defines_contract_schema() -> None:
    path = (
        ROOT
        / "alembic"
        / "versions"
        / "cp72v8x9z61_n041_customer_contract_restore.py"
    )
    spec = importlib.util.spec_from_file_location("n041_migration", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == TARGET_REVISION
    assert module.down_revision == PARENT_REVISION
    source = path.read_text(encoding="utf-8")
    for table_name in (
        "contract_daily_sequences",
        "customer_contracts",
        "customer_contract_items",
    ):
        assert table_name in source
    assert "source_contract_id" in source
    assert "ck_customer_contracts_status" in source
    assert "uq_sales_orders_source_contract_id" in source
    assert "entity_type = 'customer_contract'" in source


def test_contract_metadata_has_unique_conversion_boundary() -> None:
    from app.models import Base

    contracts = Base.metadata.tables["customer_contracts"]
    orders = Base.metadata.tables["sales_orders"]
    assert "converted_order_id" in contracts.c
    assert "conversion_idempotency_key" in contracts.c
    assert "source_contract_id" in orders.c
    assert any(
        set(constraint.columns.keys()) == {"source_contract_id"}
        for constraint in orders.constraints
    )


def test_n041_migration_round_trips_current_head(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "n041-roundtrip.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET_REVISION)

    inspector = inspect(create_sqlite_engine(path))
    assert {
        "contract_daily_sequences",
        "customer_contracts",
        "customer_contract_items",
    } <= set(inspector.get_table_names())
    assert "source_contract_id" in {
        column["name"] for column in inspector.get_columns("sales_orders")
    }
    assert _checks(path) == ("ok", 0)

    command.downgrade(config, PARENT_REVISION)
    inspector = inspect(create_sqlite_engine(path))
    assert "customer_contracts" not in set(inspector.get_table_names())
    assert "source_contract_id" not in {
        column["name"] for column in inspector.get_columns("sales_orders")
    }
    assert _checks(path) == ("ok", 0)

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET_REVISION
    assert _checks(path) == ("ok", 0)


@pytest.mark.parametrize("fact_kind", ("sequence", "audit"))
def test_n041_downgrade_fails_closed_after_deleted_contract_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fact_kind: str,
) -> None:
    path = tmp_path / f"n041-fail-closed-{fact_kind}.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        if fact_kind == "sequence":
            connection.execute(
                """
                INSERT INTO contract_daily_sequences(sequence_date, last_value)
                VALUES ('2026-07-26', 1)
                """
            )
        else:
            connection.execute(
                """
                INSERT INTO operation_logs(action, resource, entity_type, entity_id)
                VALUES ('DELETE', 'customer_contract', 'customer_contract', 1)
                """
            )
        connection.commit()

    with pytest.raises(RuntimeError, match="合同事实已存在"):
        command.downgrade(config, PARENT_REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET_REVISION
    assert _checks(path) == ("ok", 0)
