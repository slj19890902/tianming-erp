from __future__ import annotations

from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect

from app.core.database import create_sqlite_engine


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "dm95v8x9z84"
TARGET_REVISION = "dn96v8x9z85"


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


def test_external_packaging_migration_is_linear_and_model_matches() -> None:
    source = (
        ROOT / "alembic/versions/dn96v8x9z85_external_packaging_catalog.py"
    ).read_text(encoding="utf-8")
    assert 'revision: str = "dn96v8x9z85"' in source
    assert 'down_revision: Union[str, Sequence[str], None] = "dm95v8x9z84"' in source
    assert "禁止破坏性降级" in source

    from app.models import Base

    categories = Base.metadata.tables["supplier_supply_categories"]
    products = Base.metadata.tables["external_packaging_products"]
    assert {"supplier_id", "category_code", "is_active"} <= set(categories.c.keys())
    assert {
        "supplier_id",
        "category_code",
        "supplier_product_code",
        "normalized_supplier_product_code",
        "purchase_unit",
        "specification_json",
        "version",
    } <= set(products.c.keys())


def test_external_packaging_migration_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "external-packaging-roundtrip.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET_REVISION)
    tables = set(inspect(create_sqlite_engine(path)).get_table_names())
    assert {"supplier_supply_categories", "external_packaging_products"} <= tables
    with sqlite3.connect(path) as connection:
        suppliers = connection.execute(
            "SELECT COUNT(*) FROM supplier_master_records"
        ).fetchone()[0]
        categories = connection.execute(
            "SELECT COUNT(*) FROM supplier_supply_categories WHERE category_code='corrugated_board' AND is_active=1"
        ).fetchone()[0]
        assert categories == suppliers
    assert _checks(path) == ("ok", 0)

    command.downgrade(config, PARENT_REVISION)
    tables = set(inspect(create_sqlite_engine(path)).get_table_names())
    assert "supplier_supply_categories" not in tables
    assert "external_packaging_products" not in tables
    assert _checks(path) == ("ok", 0)

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET_REVISION
    assert _checks(path) == ("ok", 0)


@pytest.mark.parametrize("fact_kind", ("product", "category"))
def test_external_packaging_migration_downgrade_fails_closed_after_use(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fact_kind: str
) -> None:
    path = tmp_path / f"external-packaging-fail-{fact_kind}.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        supplier_id = connection.execute(
            "SELECT id FROM supplier_master_records ORDER BY id LIMIT 1"
        ).fetchone()[0]
        if fact_kind == "category":
            connection.execute(
                "INSERT INTO supplier_supply_categories(supplier_id,category_code,is_active) VALUES (?,?,1)",
                (supplier_id, "paper_corner_guard"),
            )
        else:
            connection.execute(
                "INSERT INTO supplier_supply_categories(supplier_id,category_code,is_active) VALUES (?,?,1)",
                (supplier_id, "paper_corner_guard"),
            )
            connection.execute(
                """
                INSERT INTO external_packaging_products(
                    supplier_id,category_code,supplier_product_code,
                    normalized_supplier_product_code,product_name,purchase_unit,
                    specification_summary,specification_json,is_active,version
                ) VALUES (?,?,?,?,?,?,?,?,1,1)
                """,
                (
                    supplier_id,
                    "paper_corner_guard",
                    "HJ-001",
                    "HJ-001",
                    "纸护角",
                    "根",
                    "L型 50×50×5mm，长1200mm",
                    "{}",
                ),
            )
        connection.commit()

    with pytest.raises(RuntimeError, match="禁止破坏性降级"):
        command.downgrade(config, PARENT_REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET_REVISION
    assert _checks(path) == ("ok", 0)

