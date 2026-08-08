from __future__ import annotations

from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect

from app.core.database import create_sqlite_engine


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "dn96v8x9z85"
TARGET_REVISION = "do97v8x9z86"


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


def test_price_migration_is_linear_and_model_matches() -> None:
    source = (
        ROOT / "alembic/versions/do97v8x9z86_external_packaging_prices.py"
    ).read_text(encoding="utf-8")
    assert 'revision: str = "do97v8x9z86"' in source
    assert 'down_revision: Union[str, Sequence[str], None] = "dn96v8x9z85"' in source
    assert "禁止破坏性降级" in source

    from app.models import Base

    table = Base.metadata.tables["external_packaging_price_versions"]
    assert {
        "external_product_id",
        "version_number",
        "product_version",
        "unit_price",
        "quote_unit",
        "currency",
        "tax_mode",
        "tax_rate",
        "moq_quantity",
        "tier_prices_json",
        "shipping_fee",
        "sample_fee",
        "plate_fee",
        "die_fee",
        "quote_fingerprint",
    } <= set(table.c.keys())


def test_price_migration_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "external-price-roundtrip.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET_REVISION)
    assert "external_packaging_price_versions" in set(
        inspect(create_sqlite_engine(path)).get_table_names()
    )
    assert _checks(path) == ("ok", 0)
    command.downgrade(config, PARENT_REVISION)
    assert "external_packaging_price_versions" not in set(
        inspect(create_sqlite_engine(path)).get_table_names()
    )
    assert _checks(path) == ("ok", 0)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET_REVISION
    assert _checks(path) == ("ok", 0)


def test_price_migration_downgrade_fails_closed_after_quote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "external-price-fail-closed.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        supplier_id = connection.execute(
            "SELECT id FROM supplier_master_records ORDER BY id LIMIT 1"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO supplier_supply_categories(supplier_id,category_code,is_active) VALUES (?,?,1)",
            (supplier_id, "paper_corner_guard"),
        )
        cursor = connection.execute(
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
                "HJ-PRICE",
                "HJ-PRICE",
                "纸护角",
                "根",
                "L型 50×50×5mm，长1200mm",
                "{}",
            ),
        )
        connection.execute(
            """
            INSERT INTO external_packaging_price_versions(
                external_product_id,version_number,product_version,
                specification_snapshot_json,quote_unit,currency,tax_mode,tax_rate,
                unit_price,effective_from,tier_prices_json,shipping_fee_mode,
                evidence_reference,quote_fingerprint
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                cursor.lastrowid,
                1,
                1,
                "{}",
                "根",
                "CNY",
                "tax_inclusive",
                0.13,
                5.2,
                "2026-08-09",
                "[]",
                "not_provided",
                "UAT",
                "a" * 64,
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

