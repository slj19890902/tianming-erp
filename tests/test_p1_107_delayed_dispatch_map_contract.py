from __future__ import annotations

import json
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.services.warehouse_delayed_dispatch_map import (
    MIGRATION_KEY,
    MAP_REVISION,
    PREVIOUS_MAP_REVISION,
    TARGET_ALLOWED_TYPES,
    TARGET_AREA_CODE,
    TARGET_AREA_NAME,
    TARGET_LOCATION_COUNT,
)
from factory_twin.scripts.migrate_floor3_current_map import _canonical_floor_revision


ROOT = Path(__file__).resolve().parents[1]
MAP_PATH = ROOT / "static" / "factory_maps" / "twin_layout_v1.json"


def test_current_map_keeps_stable_semi008_identity_for_left_turnover_area() -> None:
    document = json.loads(MAP_PATH.read_text(encoding="utf-8"))
    floor = document["floors"]["3F"]
    feature = next(
        row
        for row in floor["features"]
        if str(row.get("erp_area_code") or "").upper() == TARGET_AREA_CODE
    )

    assert floor["revision"] == MAP_REVISION == _canonical_floor_revision(floor)
    assert document["p1_107_delayed_dispatch_relocation"] == {
        "measurement_source": "owner_confirmed_2026-08-27",
        "previous_floor3_revision": PREVIOUS_MAP_REVISION,
        "floor3_revision": MAP_REVISION,
        "right_area_codes": [
            "A1", "A2", "AB1", "AB2", "B1", "B2", "C1", "C2",
            "CD1", "D1", "D2", "DE1", "E1", "E2", "E3", "F1",
            "F12", "F2", "F3", "F34", "F4",
        ],
        "left_delayed_dispatch_area_code": TARGET_AREA_CODE,
        "physical_move_required_before_inventory_write": True,
    }
    assert feature["id"] == "26849258-36f1-4620-815d-29d50e683578"
    assert feature["formal_area_id"] == 40
    assert feature["name"] == TARGET_AREA_NAME
    assert feature["formal_area_name"] == TARGET_AREA_NAME
    assert feature["allowed_inventory_types"] == TARGET_ALLOWED_TYPES
    assert feature["storage_layout"] == "pallet_ground"
    assert TARGET_LOCATION_COUNT == 20
    assert "现场搬运后才确认移货" in feature["usage_notice"]


def test_empty_install_upgrade_downgrade_upgrade_is_a_safe_noop(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p1-107-empty-roundtrip.sqlite3"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))

    command.upgrade(config, "fh43v8x9z32")
    engine = create_engine(f"sqlite:///{database}")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "fh43v8x9z32"
        assert connection.scalar(
            text(
                "SELECT COUNT(*) FROM warehouse_current_map_migration_snapshots "
                "WHERE migration_key=:key"
            ),
            {"key": MIGRATION_KEY},
        ) == 0
    engine.dispose()

    command.downgrade(config, "fg42v8x9z31")
    command.upgrade(config, "fh43v8x9z32")
    engine = create_engine(f"sqlite:///{database}")
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
    engine.dispose()
