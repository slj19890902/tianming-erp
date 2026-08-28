from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.services.current_map_raw_staging import (
    MIGRATION_KEY,
    TARGET_LOCATION_CODES,
    TARGET_WAREHOUSE_TYPE,
)
from app.services import current_map_raw_staging
from app.services.warehouse_inventory import automatic_raw_material_staging_location


ROOT = Path(__file__).resolve().parents[1]


def _isolated_formal_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Config, Path]:
    source = os.environ.get("P0_25_ISOLATED_BASELINE")
    if not source:
        pytest.skip(
            "P0_25_ISOLATED_BASELINE is required; this contract never opens the formal database."
        )
    source_path = Path(source)
    assert source_path.is_file()
    database = tmp_path / "p0-25-current-map-isolated.sqlite3"
    shutil.copy2(source_path, database)
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config, database


def _engine(database: Path):
    return create_engine(f"sqlite:///{database}")


def test_missing_current_map_is_not_treated_as_blank_when_orders_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE warehouse_current_map_migration_snapshots "
                "(migration_key TEXT PRIMARY KEY)"
            )
        )
        monkeypatch.setattr(
            current_map_raw_staging,
            "_target_locations",
            lambda _connection: [],
        )

        def fake_scalar(_connection, statement: str, _params=None) -> int:
            if "warehouse_current_map_migration_snapshots" in statement:
                return 0
            assert "sales_orders" in statement
            return 1

        monkeypatch.setattr(current_map_raw_staging, "_scalar", fake_scalar)
        with pytest.raises(RuntimeError, match="current RAW-001 rack is missing"):
            current_map_raw_staging._preflight(connection)
    engine.dispose()


def test_normalizes_current_raw_rack_and_resolves_receipt_destination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, database = _isolated_formal_copy(monkeypatch, tmp_path)
    command.upgrade(config, "fj45v8x9z34")
    engine = _engine(database)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "fj45v8x9z34"
        rows = list(
            connection.execute(
                text(
                    """
                    SELECT location_code,warehouse_type,storage_type,is_active,
                           source_version,placement_status
                    FROM warehouse_locations
                    WHERE warehouse_floor=3 AND area_code='RAW-001'
                    ORDER BY location_code
                    """
                )
            )
        )
        assert tuple(row[0] for row in rows) == TARGET_LOCATION_CODES
        assert all(
            row[1:] == (
                TARGET_WAREHOUSE_TYPE,
                "rack",
                1,
                "CURRENT_MAP",
                "placed",
            )
            for row in rows
        )
        assert connection.scalar(
            text(
                "SELECT COUNT(*) FROM warehouse_current_map_migration_snapshots "
                "WHERE migration_key=:key"
            ),
            {"key": MIGRATION_KEY},
        ) == 1
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
    with Session(engine) as session:
        destination = automatic_raw_material_staging_location(session)
        assert destination.location_code == TARGET_LOCATION_CODES[0]
        assert destination.warehouse_type == TARGET_WAREHOUSE_TYPE
        assert destination.source_version == "CURRENT_MAP"
    engine.dispose()

    command.downgrade(config, "fi44v8x9z33")
    command.upgrade(config, "fj45v8x9z34")


def test_downgrade_refuses_current_raw_rack_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, database = _isolated_formal_copy(monkeypatch, tmp_path)
    command.upgrade(config, "fj45v8x9z34")
    engine = _engine(database)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE warehouse_locations SET location_name='post-migration drift' "
                "WHERE location_code=:code"
            ),
            {"code": TARGET_LOCATION_CODES[0]},
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="Refusing P0-25 downgrade"):
        command.downgrade(config, "fi44v8x9z33")
    engine = _engine(database)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "fj45v8x9z34"
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
    engine.dispose()
