from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "admin"
    / "p1_147_warehouse_coordinate_normalization.py"
)
SPEC = importlib.util.spec_from_file_location("p1_147_coordinate_normalization", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _create_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY);
        INSERT INTO alembic_version VALUES ('jk69v8x9z58');

        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT);
        INSERT INTO users VALUES (1,'admin');

        CREATE TABLE warehouse_floors (
          id INTEGER PRIMARY KEY,floor_code TEXT,floor_name TEXT,floor_number INTEGER
        );
        INSERT INTO warehouse_floors VALUES (1,'F1','一楼',1);
        INSERT INTO warehouse_floors VALUES (3,'3F','三楼',3);
        INSERT INTO warehouse_floors VALUES (4,'4F','四楼',4);

        CREATE TABLE warehouse_areas (
          id INTEGER PRIMARY KEY,floor_id INTEGER,area_code TEXT,area_name TEXT,
          address_version INTEGER,
          FOREIGN KEY(floor_id) REFERENCES warehouse_floors(id)
        );
        INSERT INTO warehouse_areas VALUES (10,3,'A1','A1 成品区',2);
        INSERT INTO warehouse_areas VALUES (11,1,'FIN-001','一楼成品区',1);
        INSERT INTO warehouse_areas VALUES (12,4,'EDIT-001','四楼编辑区',1);
        INSERT INTO warehouse_areas VALUES (13,3,'B1','B1 非矩形成品区',3);

        CREATE TABLE warehouse_area_storage_policies (
          id INTEGER PRIMARY KEY,area_id INTEGER,map_feature_id TEXT,status TEXT,
          storage_layout TEXT,published_map_revision TEXT,version INTEGER,
          FOREIGN KEY(area_id) REFERENCES warehouse_areas(id)
        );
        INSERT INTO warehouse_area_storage_policies
        VALUES (20,10,'zone-a1','published','pallet_ground','rev-1',4);
        INSERT INTO warehouse_area_storage_policies
        VALUES (21,11,'zone-f1','published','pallet_ground','rev-f1',1);
        INSERT INTO warehouse_area_storage_policies
        VALUES (22,12,'zone-4f','published','pallet_ground','rev-4-current',1);
        INSERT INTO warehouse_area_storage_policies
        VALUES (23,13,'zone-b1','published','pallet_ground','rev-1',5);

        CREATE TABLE warehouse_locations (
          id INTEGER PRIMARY KEY,location_code TEXT,location_name TEXT,
          address_version INTEGER,placement_status TEXT,storage_type TEXT,is_active INTEGER
        );
        INSERT INTO warehouse_locations VALUES (30,'3F-A1-P01-01','三楼 A1 第1排1号',7,'placed','ground',1);
        INSERT INTO warehouse_locations VALUES (31,'F1-FIN-01','一楼成品位',1,'placed','ground',1);
        INSERT INTO warehouse_locations VALUES (32,'4F-EDIT-01','四楼编辑位',1,'placed','ground',1);
        INSERT INTO warehouse_locations VALUES (33,'3F-A1-P01-02','三楼 A1 越界位',7,'placed','ground',1);
        INSERT INTO warehouse_locations VALUES (34,'3F-B1-P01-01','三楼 B1 非矩形位',8,'placed','ground',1);

        CREATE TABLE floor3_location_layouts (
          id INTEGER PRIMARY KEY,location_id INTEGER UNIQUE,left_pct NUMERIC,top_pct NUMERIC,
          width_pct NUMERIC,height_pct NUMERIC,z_index INTEGER,version INTEGER,
          source_type TEXT,layout_kind TEXT,updated_at TEXT,
          FOREIGN KEY(location_id) REFERENCES warehouse_locations(id)
        );
        INSERT INTO floor3_location_layouts
        VALUES (40,30,0,0,25,24,1,6,'manual','physical_pallet','2026-09-02 10:00:00');
        INSERT INTO floor3_location_layouts
        VALUES (41,31,0,0,25,24,1,1,'manual','physical_pallet','2026-09-02 10:00:00');
        INSERT INTO floor3_location_layouts
        VALUES (42,32,0,0,25,24,1,1,'manual','physical_pallet','2026-09-02 10:00:00');
        INSERT INTO floor3_location_layouts
        VALUES (43,33,90,0,25,24,2,6,'manual','physical_pallet','2026-09-02 10:00:00');
        INSERT INTO floor3_location_layouts
        VALUES (44,34,60,0,20,24,1,2,'manual','physical_pallet','2026-09-02 10:00:00');

        CREATE TABLE warehouse_ground_layout_plans (
          id INTEGER PRIMARY KEY,area_id INTEGER,status TEXT,target_slot_count INTEGER,
          numbering_origin TEXT,row_direction TEXT,slot_direction TEXT,row_start_no INTEGER,
          slot_start_no INTEGER,draft_map_revision TEXT,published_map_revision TEXT,
          preview_fingerprint TEXT,version INTEGER,updated_by INTEGER,updated_at TEXT,
          FOREIGN KEY(area_id) REFERENCES warehouse_areas(id),
          FOREIGN KEY(updated_by) REFERENCES users(id)
        );
        INSERT INTO warehouse_ground_layout_plans
        VALUES (50,10,'published',2,'south','from_aisle_inward','left_to_right',1,1,
                'rev-1','rev-1','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',1,1,
                '2026-09-02 10:00:00');
        INSERT INTO warehouse_ground_layout_plans
        VALUES (51,11,'published',1,'south','from_aisle_inward','left_to_right',1,1,
                'rev-f1','rev-f1','bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',1,1,
                '2026-09-02 10:00:00');
        INSERT INTO warehouse_ground_layout_plans
        VALUES (52,12,'published',1,'south','from_aisle_inward','left_to_right',1,1,
                'rev-4-old','rev-4-old','cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',1,1,
                '2026-09-02 10:00:00');
        INSERT INTO warehouse_ground_layout_plans
        VALUES (53,13,'published',1,'south','from_aisle_inward','left_to_right',1,1,
                'rev-1','rev-1','dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',2,1,
                '2026-09-02 10:00:00');

        CREATE TABLE warehouse_ground_layout_slots (
          id INTEGER PRIMARY KEY,plan_id INTEGER,location_id INTEGER,route_sequence INTEGER,
          row_no INTEGER,slot_no INTEGER,x_mm NUMERIC,y_mm NUMERIC,width_mm INTEGER,depth_mm INTEGER,
          FOREIGN KEY(plan_id) REFERENCES warehouse_ground_layout_plans(id),
          FOREIGN KEY(location_id) REFERENCES warehouse_locations(id)
        );
        INSERT INTO warehouse_ground_layout_slots VALUES (60,50,30,1,1,1,0,0,1000,1200);
        INSERT INTO warehouse_ground_layout_slots VALUES (61,51,31,1,1,1,0,0,1000,1200);
        INSERT INTO warehouse_ground_layout_slots VALUES (62,52,32,1,1,1,0,0,1000,1200);
        INSERT INTO warehouse_ground_layout_slots VALUES (63,50,33,2,1,2,3600,0,1000,1200);
        INSERT INTO warehouse_ground_layout_slots VALUES (64,53,34,1,1,1,3000,0,1000,1200);

        CREATE TABLE inventory_pallets (
          id INTEGER PRIMARY KEY,location_id INTEGER,pallet_code TEXT,is_current INTEGER,
          FOREIGN KEY(location_id) REFERENCES warehouse_locations(id)
        );
        INSERT INTO inventory_pallets VALUES (70,30,'PAL-TEST-001',1);
        INSERT INTO inventory_pallets VALUES (71,31,'PAL-F1-001',1);

        CREATE TABLE inventory_lots (
          id INTEGER PRIMARY KEY,warehouse_location_id INTEGER,lot_number TEXT,status TEXT,unit TEXT,
          quantity_available INTEGER,quantity_reserved INTEGER,quantity_damaged INTEGER,
          FOREIGN KEY(warehouse_location_id) REFERENCES warehouse_locations(id)
        );
        INSERT INTO inventory_lots VALUES (80,30,'LOT-TEST-001','active','boxes',8,2,1);
        INSERT INTO inventory_lots VALUES (81,31,'LOT-F1-001','active','boxes',5,0,0);

        CREATE TABLE inventory_pallet_items (
          id INTEGER PRIMARY KEY,pallet_id INTEGER,inventory_lot_id INTEGER,
          FOREIGN KEY(pallet_id) REFERENCES inventory_pallets(id),
          FOREIGN KEY(inventory_lot_id) REFERENCES inventory_lots(id)
        );
        INSERT INTO inventory_pallet_items VALUES (90,70,80);
        INSERT INTO inventory_pallet_items VALUES (91,71,81);

        CREATE TABLE warehouse_ground_occupancies (
          id INTEGER PRIMARY KEY,pallet_id INTEGER,status TEXT,
          FOREIGN KEY(pallet_id) REFERENCES inventory_pallets(id)
        );
        INSERT INTO warehouse_ground_occupancies VALUES (100,70,'active');
        INSERT INTO warehouse_ground_occupancies VALUES (101,71,'active');

        CREATE TABLE warehouse_ground_occupancy_slots (
          id INTEGER PRIMARY KEY,occupancy_id INTEGER,location_id INTEGER,status TEXT,
          FOREIGN KEY(occupancy_id) REFERENCES warehouse_ground_occupancies(id),
          FOREIGN KEY(location_id) REFERENCES warehouse_locations(id)
        );
        INSERT INTO warehouse_ground_occupancy_slots VALUES (110,100,30,'active');
        INSERT INTO warehouse_ground_occupancy_slots VALUES (111,101,31,'active');

        CREATE TABLE operation_logs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,action TEXT,resource TEXT,
          details TEXT,username TEXT,entity_type TEXT,entity_id INTEGER,description TEXT,
          extra_json TEXT,event_category TEXT,result TEXT,source TEXT,module_code TEXT,
          action_code TEXT,actor_user_id_snapshot INTEGER,operator_name_snapshot TEXT,
          object_ref TEXT,request_id TEXT,batch_id TEXT,schema_version INTEGER,
          FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TRIGGER trg_ground_plans_published_immutable
        BEFORE UPDATE ON warehouse_ground_layout_plans
        WHEN OLD.status = 'published'
        BEGIN
          SELECT RAISE(ABORT, 'published ground layout plan is immutable');
        END;
        CREATE TRIGGER trg_ground_layout_slots_immutable_update
        BEFORE UPDATE ON warehouse_ground_layout_slots
        BEGIN
          SELECT RAISE(ABORT, 'published ground layout slot is immutable');
        END;
        """
    )
    connection.commit()
    connection.close()


def _create_map(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "floors": {
                    "1F": {
                        "revision": "rev-f1",
                        "features": [
                            {
                                "id": "zone-f1",
                                "feature_kind": "zone",
                                "feature_code": "ZONE-1F-FIN",
                                "erp_area_code": "FIN-001",
                                "name": "一楼成品区",
                                "points": [[0, 0], [4000, 0], [4000, 5000], [0, 5000]],
                            }
                        ],
                    },
                    "3F": {
                        "revision": "rev-1",
                        "features": [
                            {
                                "id": "zone-a1",
                                "feature_kind": "zone",
                                "feature_code": "ZONE-3F-A1",
                                "erp_area_code": "A1",
                                "name": "A1 成品区",
                                "points": [[0, 0], [4000, 0], [4000, 5000], [0, 5000]],
                            },
                            {
                                "id": "zone-b1",
                                "feature_kind": "zone",
                                "feature_code": "ZONE-3F-B1",
                                "erp_area_code": "B1",
                                "name": "B1 非矩形成品区",
                                "points": [
                                    [0, 0],
                                    [5000, 0],
                                    [5000, 5000],
                                    [2500, 5000],
                                    [2500, 3000],
                                    [0, 3000],
                                ],
                            }
                        ],
                    },
                    "4F": {
                        "revision": "rev-4-current",
                        "features": [
                            {
                                "id": "zone-4f",
                                "feature_kind": "zone",
                                "feature_code": "ZONE-4F-EDIT",
                                "erp_area_code": "EDIT-001",
                                "name": "四楼编辑区",
                                "points": [[0, 0], [4000, 0], [4000, 5000], [0, 5000]],
                            }
                        ],
                    },
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, dict]:
    root = tmp_path / "p1-147-rehearsal"
    root.mkdir()
    database = root / "factory_snapshot.sqlite3"
    map_path = root / "published_map.json"
    output = root / "report"
    _create_database(database)
    _create_map(map_path)
    plan = MODULE.build_audit(database, map_path)
    MODULE.write_audit_outputs(plan, output)
    return database, map_path, output / "p1_147_coordinate_correction_plan.json", plan


def _slot_state(database: Path) -> tuple[float, float, int, str, int]:
    connection = sqlite3.connect(database)
    slot = connection.execute(
        "SELECT x_mm,y_mm FROM warehouse_ground_layout_slots WHERE id=60"
    ).fetchone()
    plan = connection.execute(
        "SELECT version,preview_fingerprint FROM warehouse_ground_layout_plans WHERE id=50"
    ).fetchone()
    logs = connection.execute("SELECT count(*) FROM operation_logs").fetchone()[0]
    connection.close()
    return float(slot[0]), float(slot[1]), int(plan[0]), str(plan[1]), int(logs)


def _protected_state(database: Path) -> dict:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    state = MODULE.protected_snapshot(connection)
    connection.close()
    return state


def _slot_coordinates(database: Path) -> dict[int, tuple[float, float]]:
    connection = sqlite3.connect(database)
    rows = connection.execute(
        "SELECT id,x_mm,y_mm FROM warehouse_ground_layout_slots ORDER BY id"
    ).fetchall()
    connection.close()
    return {int(row[0]): (float(row[1]), float(row[2])) for row in rows}


def _mutable_trigger_count(connection: sqlite3.Connection) -> int:
    placeholders = ",".join("?" for _ in MODULE.MUTABLE_TRIGGERS)
    return int(
        connection.execute(
            f"SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name IN ({placeholders})",
            tuple(MODULE.MUTABLE_TRIGGERS),
        ).fetchone()[0]
    )


def test_audit_reports_visible_coordinates_inventory_and_samples(tmp_path: Path) -> None:
    _database, _map_path, plan_path, plan = _fixture(tmp_path)

    summary = plan["summary"]
    assert summary["warehouse_published_slot_count"] == 5
    assert summary["warehouse_joined_source_row_count"] == 5
    assert summary["warehouse_published_plan_count"] == 4
    assert summary["published_slots_by_floor"] == {"F1": 1, "3F": 3, "4F": 1}
    assert summary["scope_floor_code"] == "3F"
    assert summary["scope_published_slot_count"] == 3
    assert summary["scope_audited_slot_count"] == 3
    assert summary["scope_changed_slot_count"] == 3
    assert summary["scope_unchanged_slot_count"] == 0
    assert summary["rehearsal_candidate_slot_count"] == 1
    assert summary["field_confirmation_slot_count"] == 1
    assert summary["geometry_blocked_slot_count"] == 1
    assert summary["execution_plan_count"] == 1
    assert summary["scope_published_plan_count"] == 2
    assert summary["out_of_scope_source_slot_count"] == 2
    assert summary["out_of_scope_issue_count"] == 2
    assert summary["out_of_scope_occupied_issue_count"] == 1
    assert summary["out_of_scope_issue_counts"] == {
        "floor_code_alias_mismatch": 1,
        "plan_revision_mismatch": 1,
    }
    assert summary["geometry_counts"] == {
        "axis_aligned_rectangle": 2,
        "rotated_rectangle": 0,
        "non_rectangular": 1,
    }
    assert plan["hard_apply_blockers"] == []
    assert plan["execution_gate"]["rehearsal_ready"] is True
    assert plan["execution_gate"]["formal_apply_ready"] is False
    assert {row["floor_code"] for row in plan["rows"]} == {"3F"}
    dispositions = {
        int(row["location_id"]): row["candidate_disposition"] for row in plan["rows"]
    }
    assert dispositions == {
        30: "rehearsal_candidate",
        33: "geometry_blocked",
        34: "field_confirmation_required",
    }
    row = plan["rows"][0]
    assert row["current_y_mm"] == "0.000"
    assert row["visible_center_y_mm"] == "4400.000"
    assert row["target_y_mm"] == "3800.000"
    assert row["deviation_mm"] == "3800.000"
    assert row["pallet_codes"] == ["PAL-TEST-001"]
    assert row["inventory_quantity"] == 11
    assert row["occupied_batch_lots"][0]["lot_number"] == "LOT-TEST-001"
    assert MODULE.load_plan(plan_path)["plan_sha256"] == plan["plan_sha256"]
    samples = (plan_path.parent / "p1_147_field_samples.csv").read_text(
        encoding="utf-8-sig"
    )
    assert "area_family_A" in samples
    assert "3F-A1-P01-01" in samples
    assert "non_rectangular_in_bounds_field_confirmation" in samples
    assert "geometry_blocked_full_review" in samples
    candidates = (plan_path.parent / "p1_147_rehearsal_candidates.csv").read_text(
        encoding="utf-8-sig"
    )
    assert "3F-A1-P01-01" in candidates
    assert "3F-A1-P01-02" not in candidates
    assert "3F-B1-P01-01" not in candidates
    out_of_scope = (
        plan_path.parent / "p1_147_out_of_scope_findings.csv"
    ).read_text(encoding="utf-8-sig")
    assert "floor_code_alias_mismatch" in out_of_scope
    assert "plan_revision_mismatch" in out_of_scope


def test_rehearsal_applies_full_cas_then_rolls_back_without_audit_rows(tmp_path: Path) -> None:
    database, map_path, plan_path, _plan = _fixture(tmp_path)
    before = _slot_state(database)
    coordinates_before = _slot_coordinates(database)

    result = MODULE.execute_plan(
        database=database,
        plan_path=plan_path,
        actor_user_id=1,
        direction="apply",
        confirm_isolated_copy=True,
        token=MODULE.APPLY_TOKEN,
        rehearse=True,
        published_map=map_path,
    )

    assert result["status"] == "rehearsed_and_rolled_back"
    assert result["changed"] == 1
    assert result["checks"]["ok"] is True
    assert _slot_state(database) == before
    assert _slot_coordinates(database) == coordinates_before
    connection = sqlite3.connect(database)
    trigger_count = _mutable_trigger_count(connection)
    connection.close()
    assert trigger_count == len(MODULE.MUTABLE_TRIGGERS)


def test_apply_is_idempotent_and_rollback_restores_coordinates(tmp_path: Path) -> None:
    database, map_path, plan_path, plan = _fixture(tmp_path)
    protected_before = _protected_state(database)
    approval_path = plan_path.parent / "approval.json"
    approval_path.write_text(
        json.dumps(
            {
                "task": "P1-147",
                "plan_sha256": plan["plan_sha256"],
                "authority": MODULE.AUTHORITY_CODE,
                "field_sampling_status": "confirmed",
                "scope_floor_code": "3F",
                "approved_candidate_count": plan["execution_candidate"]["row_count"],
                "candidate_location_ids_sha256": plan["execution_candidate"][
                    "location_ids_sha256"
                ],
                "confirmed_geometry_classes": ["axis_aligned_rectangle"],
                "approved_by": "test-owner",
                "approved_at": "2026-09-02T12:00:00+08:00",
            }
        ),
        encoding="utf-8",
    )

    applied = MODULE.execute_plan(
        database=database,
        plan_path=plan_path,
        actor_user_id=1,
        direction="apply",
        confirm_isolated_copy=True,
        token=MODULE.APPLY_TOKEN,
        approval_path=approval_path,
        backup_dir=plan_path.parent / "backups",
        published_map=map_path,
    )
    assert applied["status"] == "applied"
    assert _slot_state(database) == (
        0.0,
        3800.0,
        1,
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        1,
    )
    coordinates = _slot_coordinates(database)
    assert coordinates[63] == (3600.0, 0.0)
    assert coordinates[64] == (3000.0, 0.0)
    assert Path(applied["backup"]["path"]).is_file()
    assert _protected_state(database) == protected_before

    replay = MODULE.execute_plan(
        database=database,
        plan_path=plan_path,
        actor_user_id=1,
        direction="apply",
        confirm_isolated_copy=True,
        token=MODULE.APPLY_TOKEN,
        approval_path=approval_path,
        published_map=map_path,
    )
    assert replay["status"] == "idempotent_replay"
    assert replay["backup"] is None

    rolled_back = MODULE.execute_plan(
        database=database,
        plan_path=plan_path,
        actor_user_id=1,
        direction="rollback",
        confirm_isolated_copy=True,
        token=MODULE.ROLLBACK_TOKEN,
        backup_dir=plan_path.parent / "rollback-backups",
        published_map=map_path,
    )
    assert rolled_back["status"] == "rolled_back"
    state = _slot_state(database)
    assert state[:2] == (0.0, 0.0)
    assert state[2:] == (
        1,
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        2,
    )
    assert _protected_state(database) == protected_before
    rollback_replay = MODULE.execute_plan(
        database=database,
        plan_path=plan_path,
        actor_user_id=1,
        direction="rollback",
        confirm_isolated_copy=True,
        token=MODULE.ROLLBACK_TOKEN,
        published_map=map_path,
    )
    assert rollback_replay["status"] == "idempotent_replay"


@pytest.mark.parametrize("layout_id", [40, 44])
def test_stale_layout_version_fails_closed_and_rolls_back_everything(
    tmp_path: Path, layout_id: int
) -> None:
    database, _map_path, plan_path, _plan = _fixture(tmp_path)
    plan = MODULE.load_plan(plan_path)
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE floor3_location_layouts SET version=version+1 WHERE id=?", (layout_id,)
    )
    connection.commit()
    connection.row_factory = sqlite3.Row

    with pytest.raises(MODULE.CoordinateNormalizationError, match="CAS"):
        MODULE._mutate(
            connection,
            plan,
            actor_user_id=1,
            direction="apply",
            rollback_after_validation=False,
        )

    assert tuple(
        connection.execute(
            "SELECT x_mm,y_mm FROM warehouse_ground_layout_slots WHERE id=60"
        ).fetchone()
    ) == (0, 0)
    assert connection.execute("SELECT count(*) FROM operation_logs").fetchone()[0] == 0
    assert _mutable_trigger_count(connection) == len(MODULE.MUTABLE_TRIGGERS)
    connection.close()


def test_rejects_non_isolated_path_and_changed_published_map(tmp_path: Path) -> None:
    database, map_path, plan_path, _plan = _fixture(tmp_path)
    with pytest.raises(MODULE.CoordinateNormalizationError, match="confirm-isolated-copy"):
        MODULE.execute_plan(
            database=database,
            plan_path=plan_path,
            actor_user_id=1,
            direction="apply",
            confirm_isolated_copy=False,
            token=MODULE.APPLY_TOKEN,
            rehearse=True,
            published_map=map_path,
        )
    document = json.loads(map_path.read_text(encoding="utf-8"))
    document["floors"]["3F"]["revision"] = "rev-2"
    map_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(MODULE.CoordinateNormalizationError, match="3F 正式地图"):
        MODULE.execute_plan(
            database=database,
            plan_path=plan_path,
            actor_user_id=1,
            direction="apply",
            confirm_isolated_copy=True,
            token=MODULE.APPLY_TOKEN,
            rehearse=True,
            published_map=map_path,
        )


def test_unrelated_floor_map_change_does_not_block_three_floor_rehearsal(
    tmp_path: Path,
) -> None:
    database, map_path, plan_path, _plan = _fixture(tmp_path)
    document = json.loads(map_path.read_text(encoding="utf-8"))
    document["floors"]["1F"]["revision"] = "rev-f1-new"
    document["floors"]["1F"]["features"][0]["name"] = "一楼独立修复"
    map_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    result = MODULE.execute_plan(
        database=database,
        plan_path=plan_path,
        actor_user_id=1,
        direction="apply",
        confirm_isolated_copy=True,
        token=MODULE.APPLY_TOKEN,
        rehearse=True,
        published_map=map_path,
    )

    assert result["status"] == "rehearsed_and_rolled_back"
    assert result["changed"] == 1


def test_old_ambiguous_plan_schema_and_structurally_blocked_plan_are_rejected(
    tmp_path: Path,
) -> None:
    database, map_path, plan_path, _plan = _fixture(tmp_path)
    old_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    old_plan["schema_version"] = 1
    old_plan.pop("plan_sha256")
    old_plan["plan_sha256"] = MODULE.canonical_hash(old_plan)
    old_path = plan_path.parent / "old-plan.json"
    old_path.write_text(json.dumps(old_plan), encoding="utf-8")
    with pytest.raises(MODULE.CoordinateNormalizationError, match="P1-147"):
        MODULE.load_plan(old_path)

    blocked_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    blocked_plan["hard_apply_blockers"] = ["synthetic scoped blocker"]
    blocked_plan.pop("plan_sha256")
    blocked_plan["plan_sha256"] = MODULE.canonical_hash(blocked_plan)
    blocked_path = plan_path.parent / "blocked-plan.json"
    blocked_path.write_text(json.dumps(blocked_plan), encoding="utf-8")
    with pytest.raises(MODULE.CoordinateNormalizationError, match="结构性阻断"):
        MODULE.execute_plan(
            database=database,
            plan_path=blocked_path,
            actor_user_id=1,
            direction="apply",
            confirm_isolated_copy=True,
            token=MODULE.APPLY_TOKEN,
            rehearse=True,
            published_map=map_path,
        )


def test_audit_cli_returns_nonzero_when_three_floor_has_hard_blockers(
    tmp_path: Path,
) -> None:
    root = tmp_path / "p1-147-hard-blocker"
    root.mkdir()
    database = root / "factory_snapshot.sqlite3"
    map_path = root / "published_map.json"
    output = root / "audit"
    _create_database(database)
    _create_map(map_path)
    document = json.loads(map_path.read_text(encoding="utf-8"))
    document["floors"]["3F"]["revision"] = "stale-revision"
    map_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    exit_code = MODULE.main(
        [
            "audit",
            "--database",
            str(database),
            "--published-map",
            str(map_path),
            "--output-dir",
            str(output),
            "--scope-floor",
            "3F",
        ]
    )

    blocked = json.loads(
        (output / "p1_147_coordinate_correction_plan.json").read_text(
            encoding="utf-8"
        )
    )
    assert exit_code == 2
    assert blocked["execution_gate"]["rehearsal_ready"] is False
    assert blocked["hard_apply_blockers"]
