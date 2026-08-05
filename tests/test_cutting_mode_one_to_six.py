from __future__ import annotations

from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest

from app.services.box_type_rules import normalize_box_configuration
from app.services.requisition_quantities import (
    cutting_factor,
    purchase_sheet_quantity,
)


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "dj92v8x9z81"
TARGET_REVISION = "dk93v8x9z82"


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _table_sql(path: Path, table: str) -> str:
    with sqlite3.connect(path) as connection:
        return str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()[0]
        )


def test_one_to_six_normalizes_and_calculates_six_outputs_per_sheet() -> None:
    configuration = normalize_box_configuration(
        box_style="平卡",
        splice_mode="single",
        pieces_per_box=1,
        flap_mm=None,
        default_cutting_mode="一开六",
    )

    assert configuration["default_cutting_mode"] == "一开六"
    assert cutting_factor("一开六") == 6
    assert purchase_sheet_quantity(13, 0, "一开六") == 3


def test_one_to_six_migration_round_trips_constraints(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "cutting-mode-one-to-six.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET_REVISION)

    assert "一开六" in _table_sql(path, "products")
    assert "一开六" in _table_sql(path, "sales_order_item_bom_components")

    command.downgrade(config, PARENT_REVISION)
    assert "一开六" not in _table_sql(path, "products")
    assert "一开六" not in _table_sql(path, "sales_order_item_bom_components")

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
