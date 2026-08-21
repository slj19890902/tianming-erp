from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


ROOT = Path(__file__).resolve().parents[1]
BASE_REVISION = "aaa35v8x9z24"
P1_86_REVISION = "bb36v8x9z25"
P1_86_TABLES = {
    "warehouse_location_aliases",
    "warehouse_location_address_mutations",
}


def _config(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    resolved = db_path.resolve()
    protected = (ROOT / "data" / "carton_erp.sqlite3").resolve()
    assert resolved != protected
    assert resolved.is_relative_to(db_path.parent.resolve())
    monkeypatch.setenv("ERP_DATABASE_PATH", str(resolved))
    monkeypatch.setenv("ERP_ENVIRONMENT", "development")
    monkeypatch.setenv("ERP_SECRET_KEY", "p1-86-migration-test-secret-key")
    monkeypatch.setenv("ERP_BACKUP_DIR", str((db_path.parent / "backups").resolve()))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{resolved.as_posix()}")
    return config


def _revision(engine) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(text("SELECT version_num FROM alembic_version"))
            .scalar_one()
        )


def _pragma(path: Path, statement: str):
    with sqlite3.connect(path) as connection:
        return connection.execute(statement).fetchall()


def _trigger_exists(path: Path, trigger: str) -> bool:
    with sqlite3.connect(path) as connection:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
                (trigger,),
            ).fetchone()
            is not None
        )


def _seed_legacy_location(engine) -> tuple[int, int, int]:
    with engine.begin() as connection:
        floor_id = connection.execute(
            text("SELECT id FROM warehouse_floors WHERE floor_number=3")
        ).scalar_one_or_none()
        if floor_id is None:
            floor_id = connection.execute(
                text(
                    "INSERT INTO warehouse_floors "
                    "(floor_code,floor_name,floor_number,construction_status) "
                    "VALUES ('3F','三楼',3,'enabled') RETURNING id"
                )
            ).scalar_one()
        area_id = connection.execute(
            text(
                "INSERT INTO warehouse_areas "
                "(floor_id,area_code,area_name,construction_status) "
                "VALUES (:floor,'D2-OLD','旧D2区','enabled') RETURNING id"
            ),
            {"floor": floor_id},
        ).scalar_one()
        location_id = connection.execute(
            text(
                "INSERT INTO warehouse_locations "
                "(location_code,location_name,warehouse_type,is_active,warehouse_floor,"
                "area_code,storage_type,sort_order,is_temporary,source_version,placement_status) "
                "VALUES ('P186-LEGACY-TEST-001','旧位置','finished',1,3,'D2-OLD','rack',0,0,"
                "'AREA-V1','placed') RETURNING id"
            )
        ).scalar_one()
    return floor_id, area_id, location_id


def test_revision_is_linear_from_latest_formal_head() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == [P1_86_REVISION]
    revision = script.get_revision(P1_86_REVISION)
    assert revision is not None
    assert revision.down_revision == BASE_REVISION


def test_legacy_sqlite_upgrade_downgrade_upgrade_preserves_old_rows_and_triggers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "p1_86_roundtrip.sqlite3"
    config = _config(db_path, monkeypatch)
    command.upgrade(config, BASE_REVISION)
    engine = create_engine(config.get_main_option("sqlalchemy.url"))
    _floor_id, _area_id, location_id = _seed_legacy_location(engine)
    old_row = engine.connect().execute(
        text(
            "SELECT location_code,location_name,warehouse_floor,area_code "
            "FROM warehouse_locations WHERE id=:id"
        ),
        {"id": location_id},
    ).one()
    protected_trigger = "trg_warehouse_locations_unplace_reference_guard"
    assert _trigger_exists(db_path, protected_trigger)

    command.upgrade(config, P1_86_REVISION)
    assert _revision(engine) == P1_86_REVISION
    assert P1_86_TABLES <= set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        upgraded = connection.execute(
            text(
                "SELECT location_code,location_name,warehouse_floor,area_code,"
                "address_kind,address_area_id,address_version "
                "FROM warehouse_locations WHERE id=:id"
            ),
            {"id": location_id},
        ).one()
    assert tuple(upgraded[:4]) == tuple(old_row)
    assert tuple(upgraded[4:]) == ("legacy", None, 1)
    assert _trigger_exists(db_path, protected_trigger)
    assert _pragma(db_path, "PRAGMA integrity_check") == [("ok",)]
    assert _pragma(db_path, "PRAGMA foreign_key_check") == []

    command.downgrade(config, BASE_REVISION)
    assert _revision(engine) == BASE_REVISION
    assert not (P1_86_TABLES & set(inspect(engine).get_table_names()))
    assert _trigger_exists(db_path, protected_trigger)
    with engine.connect() as connection:
        downgraded = connection.execute(
            text(
                "SELECT location_code,location_name,warehouse_floor,area_code "
                "FROM warehouse_locations WHERE id=:id"
            ),
            {"id": location_id},
        ).one()
    assert tuple(downgraded) == tuple(old_row)

    command.upgrade(config, P1_86_REVISION)
    assert _revision(engine) == P1_86_REVISION
    assert _trigger_exists(db_path, protected_trigger)
    assert _pragma(db_path, "PRAGMA integrity_check") == [("ok",)]
    assert _pragma(db_path, "PRAGMA foreign_key_check") == []


def test_structured_facts_are_guarded_immutable_and_block_downgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "p1_86_facts.sqlite3"
    config = _config(db_path, monkeypatch)
    command.upgrade(config, P1_86_REVISION)
    engine = create_engine(config.get_main_option("sqlalchemy.url"))
    _floor_id, area_id, location_id = _seed_legacy_location(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE warehouse_areas SET area_code='D02',address_zone_code='D',"
                "address_subzone_no=2,address_version=2 WHERE id=:id"
            ),
            {"id": area_id},
        )
        connection.execute(
            text(
                "UPDATE warehouse_locations SET location_code='3F-D02-A-02-03',"
                "location_name='三楼 D2区·A架·2层·3格',area_code='D02',"
                "address_kind='rack_slot',address_area_id=:area,rack_code='A',"
                "level_no=2,slot_no=3,address_version=2 WHERE id=:id"
            ),
            {"area": area_id, "id": location_id},
        )
        connection.execute(
            text(
                "INSERT INTO warehouse_location_aliases "
                "(location_id,alias_text,normalized_alias,alias_kind,created_by) "
                "VALUES (:location,'P186-LEGACY-TEST-001','P186-LEGACY-TEST-001','legacy_code',1)"
            ),
            {"location": location_id},
        )
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE warehouse_location_aliases SET alias_text='changed' "
                    "WHERE location_id=:location"
                ),
                {"location": location_id},
            )
    with pytest.raises(RuntimeError, match="P1-86"):
        command.downgrade(config, BASE_REVISION)
    assert _revision(engine) == P1_86_REVISION
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT alias_text FROM warehouse_location_aliases "
                "WHERE location_id=:location"
            ),
            {"location": location_id},
        ).scalar_one() == "P186-LEGACY-TEST-001"


def test_sqlite_structured_address_guards_reject_invalid_or_orphan_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "p1_86_guards.sqlite3"
    config = _config(db_path, monkeypatch)
    command.upgrade(config, P1_86_REVISION)
    engine = create_engine(config.get_main_option("sqlalchemy.url"))
    _floor_id, area_id, location_id = _seed_legacy_location(engine)
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE warehouse_locations SET address_kind='rack_slot',"
                    "address_area_id=999999,rack_code='A',level_no=1,slot_no=1 "
                    "WHERE id=:id"
                ),
                {"id": location_id},
            )
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE warehouse_locations SET address_kind='rack_slot',"
                "address_area_id=:area,rack_code='A',level_no=1,slot_no=1 "
                "WHERE id=:id"
            ),
            {"area": area_id, "id": location_id},
        )
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM warehouse_areas WHERE id=:area"),
                {"area": area_id},
            )
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE warehouse_areas SET address_zone_code='Z',"
                    "address_subzone_no=1 WHERE id=1"
                )
            )
    assert _pragma(db_path, "PRAGMA foreign_key_check") == []
