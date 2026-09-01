from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.models.warehouse_inventory import (
    Floor3LocationLayout,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseLocation,
)
from app.services.liner_finished_turnover import (
    MAP_REVISION,
    MIGRATION_KEY,
    TARGET_LOCATION_CODES,
    TARGETS,
    TURNOVER_REMARK,
)


ROOT = Path(__file__).resolve().parents[1]
PARENT = "iz61v8x9z50"
TARGET = "jg66v8x9z55"


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    return config


def _health(database: Path) -> tuple[str, list[tuple]]:
    with sqlite3.connect(database) as connection:
        return (
            connection.execute("PRAGMA integrity_check").fetchone()[0],
            connection.execute("PRAGMA foreign_key_check").fetchall(),
        )


def _seed_formal_profile(database: Path) -> None:
    engine = create_engine(f"sqlite:///{database}")
    with Session(engine) as session:
        floor = session.scalar(
            select(WarehouseFloor).where(WarehouseFloor.floor_number == 3)
        )
        assert floor is not None
        floor.construction_status = "enabled"

        reviewed_at = datetime(2026, 8, 27, 12, 0, 0)
        area_by_code: dict[str, WarehouseArea] = {}
        for area_code, area_name in (
            ("F34", "F3/F4之间临时周转区"),
            ("F12", "F1/F2之间临时周转区"),
        ):
            profile = TARGETS[area_code]
            area = session.scalar(
                select(WarehouseArea).where(
                    WarehouseArea.floor_id == floor.id,
                    WarehouseArea.area_code == area_code,
                )
            )
            assert area is not None
            area.area_name = area_name
            area.planned_location_count = int(profile["count"])
            area.planned_pallet_capacity = int(profile["count"])
            area.construction_status = "enabled"
            area.capacity_review_status = "confirmed"
            area.capacity_eligible = True
            area.confirmed_pallet_capacity = int(profile["count"])
            area.capacity_reviewed_by = "warehouse-map-migration"
            area.capacity_reviewed_at = reviewed_at
            area_by_code[area_code] = area
            session.add(
                WarehouseAreaStoragePolicy(
                    area_id=area.id,
                    map_feature_id=str(profile["feature_id"]),
                    allowed_inventory_types_json='["finished"]',
                    storage_layout="pallet_ground",
                    status="published",
                    published_map_revision=MAP_REVISION,
                    version=1,
                )
            )

        locations = list(
            session.scalars(
                select(WarehouseLocation).where(
                    WarehouseLocation.location_code.in_(TARGET_LOCATION_CODES)
                )
            )
        )
        assert {row.location_code for row in locations} == set(TARGET_LOCATION_CODES)
        for index, location in enumerate(
            sorted(locations, key=lambda row: row.location_code),
            start=1,
        ):
            location_code = location.location_code
            area_code = location_code.split("-", 1)[0]
            location.location_name = f"{area_code}临时周转{index:02d}"
            location.warehouse_type = "finished"
            location.is_active = False
            location.remarks = "当前地图已建档，尚未启用"
            location.warehouse_floor = 3
            location.area_code = area_code
            location.storage_type = "temporary_aisle"
            location.sort_order = index
            location.is_temporary = True
            location.source_version = "CURRENT_MAP"
            location.address_kind = "functional"
            location.address_area_id = area_by_code[area_code].id
            location.address_version = 1
            location.placement_status = "placed"
            layout = session.scalar(
                select(Floor3LocationLayout).where(
                    Floor3LocationLayout.location_id == location.id
                )
            )
            assert layout is not None
            layout.layout_kind = "logical_anchor"
        session.commit()
    engine.dispose()


def _location_profile(database: Path) -> list[tuple]:
    engine = create_engine(f"sqlite:///{database}")
    with engine.connect() as connection:
        rows = list(
            connection.execute(
                text(
                    "SELECT location_code,is_active,is_temporary,remarks,updated_at "
                    "FROM warehouse_locations "
                    "WHERE location_code LIKE 'F34-P%' OR location_code LIKE 'F12-P%' "
                    "ORDER BY location_code"
                )
            )
        )
    engine.dispose()
    return [tuple(row) for row in rows]


def test_empty_install_roundtrip_is_safe_noop(monkeypatch, tmp_path: Path) -> None:
    database = tmp_path / "p0-33-empty.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, TARGET)

    engine = create_engine(f"sqlite:///{database}")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == TARGET
        assert connection.scalar(
            text(
                "SELECT COUNT(*) FROM warehouse_current_map_migration_snapshots "
                "WHERE migration_key=:key"
            ),
            {"key": MIGRATION_KEY},
        ) == 0
    engine.dispose()

    command.downgrade(config, PARENT)
    command.upgrade(config, TARGET)
    assert _health(database) == ("ok", [])


def test_formal_profile_activates_exact_anchors_and_roundtrips(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p0-33-formal-profile.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, PARENT)
    _seed_formal_profile(database)
    before = _location_profile(database)

    command.upgrade(config, TARGET)
    after = _location_profile(database)
    assert len(after) == 11
    assert all(row[1:4] == (1, 1, TURNOVER_REMARK) for row in after)

    engine = create_engine(f"sqlite:///{database}")
    with engine.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT COUNT(*) FROM warehouse_current_map_migration_snapshots "
                "WHERE migration_key=:key"
            ),
            {"key": MIGRATION_KEY},
        ) == 1
        assert connection.scalar(
            text(
                "SELECT COUNT(*) FROM warehouse_ground_layout_slots slot "
                "JOIN warehouse_locations location ON location.id=slot.location_id "
                "WHERE location.location_code LIKE 'F34-P%' "
                "OR location.location_code LIKE 'F12-P%'"
            )
        ) == 0
    engine.dispose()

    command.downgrade(config, PARENT)
    assert _location_profile(database) == before
    engine = create_engine(f"sqlite:///{database}")
    with engine.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT COUNT(*) FROM warehouse_current_map_migration_snapshots "
                "WHERE migration_key=:key"
            ),
            {"key": MIGRATION_KEY},
        ) == 0
    engine.dispose()

    command.upgrade(config, TARGET)
    assert _health(database) == ("ok", [])


def test_downgrade_refuses_historical_business_reference(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p0-33-used-anchor.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, PARENT)
    _seed_formal_profile(database)
    command.upgrade(config, TARGET)

    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        location_id = connection.scalar(
            text("SELECT id FROM warehouse_locations WHERE location_code='F34-P01'")
        )
        pallet_id = connection.execute(
            text(
                "INSERT INTO inventory_pallets "
                "(pallet_code,location_id,location_occupancy_key,status,is_current,"
                "needs_relocation,version) "
                "VALUES ('P0-33-DOWNGRADE-GUARD',:location_id,'PRIMARY','active',1,1,1)"
            ),
            {"location_id": location_id},
        ).lastrowid
        connection.execute(
            text(
                "INSERT INTO inventory_location_movements "
                "(pallet_id,from_location_id,to_location_id,movement_type,remarks) "
                "VALUES (:pallet_id,NULL,:location_id,'create','P0-33回退保护')"
            ),
            {"pallet_id": pallet_id, "location_id": location_id},
        )
        connection.execute(
            text(
                "UPDATE inventory_pallets SET location_id=NULL,is_current=0,"
                "status='closed' WHERE id=:pallet_id"
            ),
            {"pallet_id": pallet_id},
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="Refusing P0-33 downgrade"):
        command.downgrade(config, PARENT)

    engine = create_engine(f"sqlite:///{database}")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == TARGET
    engine.dispose()
    assert _health(database) == ("ok", [])
