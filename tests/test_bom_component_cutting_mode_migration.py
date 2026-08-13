from __future__ import annotations

from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import CheckConstraint, Column, Integer, MetaData, String, Table, inspect
from app.core.database import create_sqlite_engine


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "ci65v8x9z54"
TARGET_REVISION = "co71v8x9z60"


def _create_parent_schema(path: Path) -> None:
    """Build only the direct-parent contract used by this migration.

    Replaying the entire historical chain is unrelated to this revision and
    can crash SQLAlchemy's native SQLite reflection path on Windows.
    """
    engine = create_sqlite_engine(path)
    metadata = MetaData()
    Table("alembic_version", metadata, Column("version_num", String, primary_key=True))
    Table(
        "sales_order_item_bom_components",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("sales_order_item_id", Integer, nullable=False),
        Column("component_product_id", Integer, nullable=False),
        Column("product_bom_component_id", Integer),
        Column("snapshot_schema_version", Integer, nullable=False),
        Column("order_set_quantity", Integer, nullable=False),
        Column("quantity_per_set", Integer, nullable=False),
        Column("required_piece_quantity", Integer, nullable=False),
        Column("display_order", Integer, nullable=False),
        Column("internal_component_code", String),
        Column("is_die_cut", Integer, nullable=False),
        Column("spare_sheet_quantity", Integer, nullable=False),
        Column("display_mode", String, nullable=False),
        Column("is_required", Integer, nullable=False),
        Column("snapshot_component_product_code", String, nullable=False),
        Column("snapshot_component_product_name", String, nullable=False),
        Column("snapshot_component_box_category", String, nullable=False),
        Column("snapshot_component_box_style", String),
    )
    Table(
        "requisition_item_bom_sources",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("calculation_rule_version", String, nullable=False),
        Column("order_set_quantity", Integer, nullable=False),
        Column("quantity_per_set", Integer, nullable=False),
        Column("required_piece_quantity", Integer, nullable=False),
        CheckConstraint(
            "required_piece_quantity = order_set_quantity * quantity_per_set",
            name="ck_requisition_item_bom_sources_required_piece_formula",
        ),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"INSERT INTO alembic_version(version_num) VALUES ('{PARENT_REVISION}')"
        )
    engine.dispose()


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


def test_cutting_snapshot_migration_is_linear_and_round_trips_empty_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "bom-cutting-roundtrip.sqlite3"
    _create_parent_schema(path)
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET_REVISION)

    engine = create_sqlite_engine(path)
    inspector = inspect(engine)
    assert {
        "snapshot_component_default_cutting_mode"
    } <= {column["name"] for column in inspector.get_columns(
        "sales_order_item_bom_components"
    )}
    assert {"demand_basis"} <= {
        column["name"]
        for column in inspector.get_columns("requisition_item_bom_sources")
    }
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET_REVISION
    assert _checks(path) == ("ok", 0)

    command.downgrade(config, PARENT_REVISION)
    inspector = inspect(create_sqlite_engine(path))
    assert "snapshot_component_default_cutting_mode" not in {
        column["name"]
        for column in inspector.get_columns("sales_order_item_bom_components")
    }
    assert "demand_basis" not in {
        column["name"]
        for column in inspector.get_columns("requisition_item_bom_sources")
    }
    assert _checks(path) == ("ok", 0)


def test_cutting_snapshot_migration_downgrade_fails_closed_after_new_order_fact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "bom-cutting-fail-closed.sqlite3"
    _create_parent_schema(path)
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """
            INSERT INTO sales_order_item_bom_components (
                sales_order_item_id,
                component_product_id,
                snapshot_schema_version,
                order_set_quantity,
                quantity_per_set,
                required_piece_quantity,
                display_order,
                internal_component_code,
                is_die_cut,
                spare_sheet_quantity,
                display_mode,
                is_required,
                snapshot_component_product_code,
                snapshot_component_product_name,
                snapshot_component_box_category,
                snapshot_component_box_style,
                snapshot_component_default_cutting_mode
            ) VALUES (
                1, 1, 3, 3000, 1, 3000, 1, 'UAT-PARENT-S01',
                0, 0, 'internal_only', 1, 'UAT-KNIFE', '匿名刀卡',
                'normal', '刀卡', '一开二'
            )
            """
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="禁止破坏性降级"):
        command.downgrade(config, PARENT_REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET_REVISION
        connection.execute("DELETE FROM sales_order_item_bom_components")
        connection.commit()
    assert _checks(path) == ("ok", 0)
