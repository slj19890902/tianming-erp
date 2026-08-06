from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import sqlite3

import pytest

from app.services.warehouse_twin_production import (
    WarehouseTwinProductionError,
    build_production_projection,
    delete_production_projection_mapping,
    save_production_projection_mapping,
)


def _mapping_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE twin_layouts (id TEXT PRIMARY KEY);
            INSERT INTO twin_layouts VALUES ('layout-1f');
            CREATE TABLE twin_production_projection_mappings (
                id TEXT PRIMARY KEY,
                layout_id TEXT NOT NULL,
                source_task_id INTEGER NOT NULL,
                source_type TEXT NOT NULL,
                target_kind TEXT NOT NULL,
                target_id TEXT NOT NULL,
                confirmed_at TEXT NOT NULL,
                version INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(layout_id, source_task_id),
                FOREIGN KEY(layout_id) REFERENCES twin_layouts(id)
            );
            """
        )


def _floor() -> dict:
    return {
        "layout_id": "layout-1f",
        "floor_code": "1F",
        "pallets": [
            {"id": "pallet-a", "pallet_code": "PAL-1F-001", "name": "印刷周转栈板"}
        ],
        "features": [
            {
                "id": "zone-a",
                "feature_code": "ZONE-1F-TMP-001",
                "name": "临时周转区",
                "feature_kind": "zone",
            }
        ],
    }


def _task() -> dict:
    return {
        "id": 17,
        "version": 4,
        "order_id": 81,
        "order_number": "TM20260806-0081",
        "customer_name": "昆山华诚电子有限公司",
        "product_code": "HC-520",
        "product_name": "五层加强纸箱",
        "specification": "520×350×300mm",
        "planned_quantity": 1200,
        "production_quantity_unit": "sets",
    }


def test_mapping_write_is_isolated_versioned_and_stale_source_is_not_current(
    tmp_path: Path,
) -> None:
    mapping_db = tmp_path / "factory-twin.sqlite3"
    erp_snapshot = tmp_path / "erp-replica.sqlite3"
    _mapping_database(mapping_db)
    erp_snapshot.write_bytes(b"authoritative ERP facts stay unchanged")
    erp_before = sha256(erp_snapshot.read_bytes()).hexdigest()

    initial = build_production_projection(
        floor=_floor(), tasks=[_task()], path=mapping_db
    )
    assert initial["source_read_only"] is True
    assert initial["items"][0]["mapping"] is None

    mapping = save_production_projection_mapping(
        floor=_floor(),
        source_task_id=17,
        target_kind="pallet",
        target_id="pallet-a",
        expected_version=None,
        path=mapping_db,
    )
    assert mapping["target_code"] == "PAL-1F-001"
    assert mapping["version"] == 1
    with pytest.raises(WarehouseTwinProductionError, match="已被更新"):
        save_production_projection_mapping(
            floor=_floor(),
            source_task_id=17,
            target_kind="zone",
            target_id="zone-a",
            expected_version=9,
            path=mapping_db,
        )

    restarted = build_production_projection(floor=_floor(), tasks=[], path=mapping_db)
    assert restarted["items"] == []
    assert restarted["stale_mappings"][0]["source_task_id"] == 17
    assert restarted["stale_mappings"][0]["source_state"] == "not_pending_or_missing"
    assert sha256(erp_snapshot.read_bytes()).hexdigest() == erp_before

    delete_production_projection_mapping(
        layout_id="layout-1f",
        source_task_id=17,
        expected_version=1,
        path=mapping_db,
    )
    cleared = build_production_projection(floor=_floor(), tasks=[], path=mapping_db)
    assert cleared["stale_mappings"] == []
