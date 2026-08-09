from __future__ import annotations

from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect

from app.core.database import create_sqlite_engine


ROOT = Path(__file__).resolve().parents[1]
PARENT = "dv04v8x9z93"
TARGET = "dw05v8x9z94"
TABLE = "sales_order_item_estimated_cost_snapshots"


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


def test_migration_is_linear_registered_and_round_trips(monkeypatch, tmp_path) -> None:
    source = (ROOT / "alembic/versions/dw05v8x9z94_order_estimated_cost_snapshots.py").read_text(encoding="utf-8")
    assert 'revision = "dw05v8x9z94"' in source
    assert 'down_revision = "dv04v8x9z93"' in source
    from app.models import Base

    assert TABLE in Base.metadata.tables
    path = tmp_path / "estimated-cost-roundtrip.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET)
    assert TABLE in set(inspect(create_sqlite_engine(path)).get_table_names())
    assert _checks(path) == ("ok", 0)
    command.downgrade(config, PARENT)
    assert TABLE not in set(inspect(create_sqlite_engine(path)).get_table_names())
    command.upgrade(config, TARGET)
    assert _checks(path) == ("ok", 0)


def test_snapshot_is_immutable_and_downgrade_fails_closed(monkeypatch, tmp_path) -> None:
    path = tmp_path / "estimated-cost-fail-closed.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET)
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"""INSERT INTO {TABLE}(
            sales_order_item_id,order_item_reference_snapshot,snapshot_version,source_fingerprint,
            calculation_status,scope_code,rule_version,precision_version,order_quantity_snapshot,
            loss_rate,processing_batch_cost,processing_unit_cost,extra_color_unit_cost,
            loss_material_total_cost,processing_total_cost,one_time_fee_total,
            known_estimated_subtotal,tax_rate_reference,tax_basis_code,breakdown_json,missing_items_json
            ) VALUES (1,'TEST-001',1,?,'missing','estimated_total','p1-28c1-estimated-v1',
            'p1-28c1-decimal-v1',1,0.03,0,0,0,0,0,0,0,0.13,'material_source_as_stored','{{}}','[]')""",
            ("a" * 64,),
        )
        connection.commit()
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute(f"UPDATE {TABLE} SET snapshot_version=2")
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute(f"DELETE FROM {TABLE}")
    with pytest.raises(RuntimeError, match="禁止破坏性降级"):
        command.downgrade(config, PARENT)
    assert _checks(path) == ("ok", 0)
