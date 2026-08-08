from __future__ import annotations

from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect

from app.core.database import create_sqlite_engine


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "do97v8x9z86"
TARGET_REVISION = "dp98v8x9z87"


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


def test_component_migration_is_linear_and_model_matches() -> None:
    source = (ROOT / "alembic/versions/dp98v8x9z87_product_external_components.py").read_text(encoding="utf-8")
    assert 'revision = "dp98v8x9z87"' in source
    assert 'down_revision = "do97v8x9z86"' in source
    assert "禁止降级" in source

    from app.models import Base

    assert "customer_scope_id" in Base.metadata.tables["external_packaging_products"].c
    assert {
        "product_external_component_sets",
        "product_external_components",
        "product_external_component_candidates",
    } <= set(Base.metadata.tables)


def test_component_migration_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "component-roundtrip.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET_REVISION)
    tables = set(inspect(create_sqlite_engine(path)).get_table_names())
    assert "product_external_component_sets" in tables
    assert _checks(path) == ("ok", 0)
    command.downgrade(config, PARENT_REVISION)
    tables = set(inspect(create_sqlite_engine(path)).get_table_names())
    assert "product_external_component_sets" not in tables
    assert "customer_scope_id" not in {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(external_packaging_products)")}
    assert _checks(path) == ("ok", 0)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == TARGET_REVISION
    assert _checks(path) == ("ok", 0)


def test_component_migration_downgrade_fails_closed_for_customer_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "component-fail-closed.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        customer_cursor = connection.execute(
            """
            INSERT INTO customers(
                name,payment_term_days,statement_cycle_start_day,credit_limit,
                delivery_method,default_tax_rate,status,is_active,version
            ) VALUES (?,0,20,0,'配送',0.13,'active',1,1)
            """,
            ("迁移匿名客户",),
        )
        customer_id = customer_cursor.lastrowid
        supplier_id = connection.execute("SELECT id FROM supplier_master_records ORDER BY id LIMIT 1").fetchone()[0]
        connection.execute(
            "INSERT OR IGNORE INTO supplier_supply_categories(supplier_id,category_code,is_active) VALUES (?,?,1)",
            (supplier_id, "paper_corner_guard"),
        )
        connection.execute(
            """
            INSERT INTO external_packaging_products(
                supplier_id,customer_scope_id,category_code,supplier_product_code,
                normalized_supplier_product_code,product_name,purchase_unit,
                specification_summary,specification_json,is_active,version
            ) VALUES (?,?,?,?,?,?,?,?,?,1,1)
            """,
            (supplier_id, customer_id, "paper_corner_guard", "SCOPE-UAT", "SCOPE-UAT", "客户专用护角", "根", "L型", "{}"),
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="禁止降级"):
        command.downgrade(config, PARENT_REVISION)
    assert _checks(path) == ("ok", 0)
