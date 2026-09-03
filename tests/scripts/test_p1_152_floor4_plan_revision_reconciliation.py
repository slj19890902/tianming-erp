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
    / "p1_152_floor4_plan_revision_reconciliation.py"
)
SPEC = importlib.util.spec_from_file_location("p1_152_floor4_revision", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


CURRENT_REVISION = "870f98d2353db75f"
AREA_FIXTURES = (
    {
        "area_id": 57,
        "area_code": "EDIT-001",
        "feature_id": "zone-edit-001",
        "plan_id": 22,
        "policy_id": 71,
        "policy_version": 5,
        "old_revision": "b1459bfb08cb363d",
        "first_location_id": 632,
        "first_slot_id": 319,
        "y_mm": 0,
        "plan_version": 4,
    },
    {
        "area_id": 58,
        "area_code": "EDIT-002",
        "feature_id": "zone-edit-002",
        "plan_id": 23,
        "policy_id": 72,
        "policy_version": 3,
        "old_revision": "d72b122a73dadd12",
        "first_location_id": 644,
        "first_slot_id": 331,
        "y_mm": 1500,
        "plan_version": 2,
    },
)


def _feature(area: dict) -> dict:
    y_mm = area["y_mm"]
    return {
        "id": area["feature_id"],
        "feature_kind": "zone",
        "status": "confirmed",
        "formal_floor_id": 4,
        "formal_area_id": area["area_id"],
        "erp_area_code": area["area_code"],
        "name": f"四楼 {area['area_code']} 区",
        "points": [
            [0, y_mm],
            [14400, y_mm],
            [14400, y_mm + 1000],
            [0, y_mm + 1000],
        ],
        "storage_mode": "floor",
        "no_stacking": True,
        "elevation_mm": 0,
        "storage_height_mm": 1800,
        "allowed_inventory_types": ["finished_goods"],
        "storage_layout": "pallet_ground",
    }


def _create_map(path: Path) -> dict:
    document = {
        "schema_version": 1,
        "floors": {
            "4F": {
                "revision": CURRENT_REVISION,
                "bounds_mm": {
                    "min_x": 0,
                    "min_y": 0,
                    "max_x": 14400,
                    "max_y": 3000,
                },
                "features": [_feature(area) for area in AREA_FIXTURES],
                "structures": [],
                "placements": [],
                "racks": [],
            }
        },
    }
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return document


def _create_database(path: Path, map_document: dict) -> None:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        """
        CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY);
        INSERT INTO alembic_version VALUES ('jm71v8x9z60');

        CREATE TABLE users (
          id INTEGER PRIMARY KEY,username TEXT,full_name TEXT
        );
        INSERT INTO users VALUES (1,'admin','测试管理员');

        CREATE TABLE warehouse_floors (
          id INTEGER PRIMARY KEY,floor_code TEXT,floor_name TEXT,floor_number INTEGER
        );
        INSERT INTO warehouse_floors VALUES (4,'4F','四楼',4);

        CREATE TABLE warehouse_areas (
          id INTEGER PRIMARY KEY,floor_id INTEGER,area_code TEXT,area_name TEXT,
          address_version INTEGER,planned_location_count INTEGER,
          planned_pallet_capacity INTEGER,
          FOREIGN KEY(floor_id) REFERENCES warehouse_floors(id)
        );

        CREATE TABLE warehouse_area_storage_policies (
          id INTEGER PRIMARY KEY,area_id INTEGER,map_feature_id TEXT,
          allowed_inventory_types_json TEXT,storage_layout TEXT,status TEXT,
          draft_map_revision TEXT,published_map_revision TEXT,version INTEGER,
          updated_by INTEGER,updated_at TEXT,
          FOREIGN KEY(area_id) REFERENCES warehouse_areas(id),
          FOREIGN KEY(updated_by) REFERENCES users(id)
        );

        CREATE TABLE warehouse_locations (
          id INTEGER PRIMARY KEY,location_code TEXT,location_name TEXT,
          warehouse_floor INTEGER,area_code TEXT,address_area_id INTEGER,
          address_version INTEGER,placement_status TEXT,storage_type TEXT,
          is_active INTEGER,sort_order INTEGER
        );

        CREATE TABLE floor3_location_layouts (
          id INTEGER PRIMARY KEY,location_id INTEGER UNIQUE,left_pct NUMERIC,
          top_pct NUMERIC,width_pct NUMERIC,height_pct NUMERIC,z_index INTEGER,
          version INTEGER,source_type TEXT,layout_kind TEXT,updated_at TEXT,
          FOREIGN KEY(location_id) REFERENCES warehouse_locations(id)
        );

        CREATE TABLE warehouse_ground_layout_plans (
          id INTEGER PRIMARY KEY,area_id INTEGER,status TEXT,target_slot_count INTEGER,
          numbering_origin TEXT,row_direction TEXT,slot_direction TEXT,
          row_start_no INTEGER,slot_start_no INTEGER,draft_map_revision TEXT,
          published_map_revision TEXT,preview_fingerprint TEXT,version INTEGER,
          publish_idempotency_key TEXT,publish_request_hash TEXT,
          updated_by INTEGER,updated_at TEXT,published_by INTEGER,published_at TEXT,
          FOREIGN KEY(area_id) REFERENCES warehouse_areas(id),
          FOREIGN KEY(updated_by) REFERENCES users(id),
          FOREIGN KEY(published_by) REFERENCES users(id)
        );

        CREATE TABLE warehouse_ground_layout_slots (
          id INTEGER PRIMARY KEY,plan_id INTEGER,location_id INTEGER,
          route_sequence INTEGER,row_no INTEGER,slot_no INTEGER,x_mm NUMERIC,
          y_mm NUMERIC,width_mm INTEGER,depth_mm INTEGER,
          FOREIGN KEY(plan_id) REFERENCES warehouse_ground_layout_plans(id),
          FOREIGN KEY(location_id) REFERENCES warehouse_locations(id)
        );

        CREATE TABLE inventory_lots (
          id INTEGER PRIMARY KEY,warehouse_location_id INTEGER,status TEXT,
          quantity_available INTEGER,quantity_reserved INTEGER,quantity_damaged INTEGER,
          FOREIGN KEY(warehouse_location_id) REFERENCES warehouse_locations(id)
        );
        CREATE TABLE inventory_pallets (
          id INTEGER PRIMARY KEY,location_id INTEGER,is_current INTEGER,
          FOREIGN KEY(location_id) REFERENCES warehouse_locations(id)
        );
        CREATE TABLE inventory_pallet_items (
          id INTEGER PRIMARY KEY,pallet_id INTEGER,inventory_lot_id INTEGER,
          FOREIGN KEY(pallet_id) REFERENCES inventory_pallets(id),
          FOREIGN KEY(inventory_lot_id) REFERENCES inventory_lots(id)
        );
        CREATE TABLE warehouse_ground_occupancies (
          id INTEGER PRIMARY KEY,pallet_id INTEGER,status TEXT,
          FOREIGN KEY(pallet_id) REFERENCES inventory_pallets(id)
        );
        CREATE TABLE warehouse_ground_occupancy_slots (
          id INTEGER PRIMARY KEY,occupancy_id INTEGER,location_id INTEGER,status TEXT,
          FOREIGN KEY(occupancy_id) REFERENCES warehouse_ground_occupancies(id),
          FOREIGN KEY(location_id) REFERENCES warehouse_locations(id)
        );

        CREATE TABLE operation_logs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,action TEXT,
          resource TEXT,details TEXT,username TEXT,entity_type TEXT,
          entity_id INTEGER,description TEXT,extra_json TEXT,event_category TEXT,
          result TEXT,source TEXT,module_code TEXT,action_code TEXT,
          actor_user_id_snapshot INTEGER,operator_name_snapshot TEXT,
          object_ref TEXT,request_id TEXT,batch_id TEXT,schema_version INTEGER,
          FOREIGN KEY(user_id) REFERENCES users(id)
        );

        """
    )

    floor = {"floor_code": "4F", **map_document["floors"]["4F"]}
    for area in AREA_FIXTURES:
        connection.execute(
            "INSERT INTO warehouse_areas VALUES (?,?,?,?,?,?,?)",
            (
                area["area_id"],
                4,
                area["area_code"],
                f"四楼 {area['area_code']} 区",
                1,
                12,
                12,
            ),
        )
        connection.execute(
            "INSERT INTO warehouse_area_storage_policies VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                area["policy_id"],
                area["area_id"],
                area["feature_id"],
                '["finished_goods"]',
                "pallet_ground",
                "published",
                CURRENT_REVISION,
                CURRENT_REVISION,
                area["policy_version"],
                1,
                "2026-09-03 08:00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO warehouse_ground_layout_plans
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                area["plan_id"],
                area["area_id"],
                "published",
                12,
                "south",
                "from_aisle_inward",
                "left_to_right",
                1,
                1,
                area["old_revision"],
                area["old_revision"],
                "pending-fingerprint",
                area["plan_version"],
                f"publish-{area['plan_id']}",
                f"request-{area['plan_id']}",
                1,
                "2026-09-03 08:10:00",
                1,
                "2026-09-03 08:10:00",
            ),
        )
        for index in range(12):
            location_id = area["first_location_id"] + index
            slot_id = area["first_slot_id"] + index
            x_mm = index * 1200
            location_code = f"4F-{area['area_code'].replace('-', '')}-P01-{index + 1:02d}"
            connection.execute(
                "INSERT INTO warehouse_locations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    location_id,
                    location_code,
                    f"四楼 {area['area_code']} 第1排{index + 1}号位",
                    4,
                    area["area_code"],
                    None,
                    1,
                    "placed",
                    "ground",
                    1,
                    index + 1,
                ),
            )
            connection.execute(
                "INSERT INTO floor3_location_layouts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    location_id,
                    location_id,
                    index * 100 / 12,
                    0,
                    100 / 12,
                    100,
                    index + 1,
                    1,
                    "ground_plan",
                    "physical_pallet",
                    "2026-09-03 08:10:00",
                ),
            )
            connection.execute(
                "INSERT INTO warehouse_ground_layout_slots VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    slot_id,
                    area["plan_id"],
                    location_id,
                    index + 1,
                    1,
                    index + 1,
                    x_mm,
                    area["y_mm"],
                    1200,
                    1000,
                ),
            )

    for area in AREA_FIXTURES:
        source = next(
            row
            for row in MODULE._plan_sources(connection)
            if int(row["plan_id"]) == area["plan_id"]
        )
        slots = MODULE._slot_sources(connection, area["plan_id"])
        derived = MODULE._derive_slots(floor, source, slots)
        old_fingerprint = MODULE.ground_preview_fingerprint(
            area_id=area["area_id"],
            policy_version=2,
            map_revision=area["old_revision"],
            configuration=MODULE._configuration(source),
            slots=derived,
        )
        connection.execute(
            "UPDATE warehouse_ground_layout_plans SET preview_fingerprint=? WHERE id=?",
            (old_fingerprint, area["plan_id"]),
        )
        feature = next(
            row for row in floor["features"] if row["id"] == area["feature_id"]
        )
        location_ids = list(
            range(area["first_location_id"], area["first_location_id"] + 12)
        )
        history = (
            (
                "TWIN_ZONE_POLICY_UPDATE",
                {
                    "floor_code": "4F",
                    "formal_area_code": area["area_code"],
                    "zone": feature,
                },
            ),
            (
                "TWIN_LAYOUT_PUBLISH",
                {
                    "floor_code": "4F",
                    "published_revision": area["old_revision"],
                    "published_sha256": f"old-map-{area['plan_id']}",
                    "formal_areas": [{"area_code": area["area_code"]}],
                },
            ),
            (
                "TWIN_ZONE_ONE_STEP_CONFIRM",
                {
                    "floor_code": "4F",
                    "area_id": area["area_id"],
                    "area_code": area["area_code"],
                    "ground_plan_id": area["plan_id"],
                    "published_revision": area["old_revision"],
                    "reflowed_location_ids": location_ids,
                },
            ),
        )
        for action, details in history:
            connection.execute(
                "INSERT INTO operation_logs (user_id,action,details) VALUES (?,?,?)",
                (1, action, json.dumps(details, ensure_ascii=False)),
            )
    connection.executescript(
        """
        CREATE TRIGGER trg_ground_plans_published_immutable
        BEFORE UPDATE ON warehouse_ground_layout_plans
        WHEN OLD.status = 'published'
        BEGIN
          SELECT RAISE(ABORT, 'published ground layout plan is immutable');
        END;
        """
    )
    connection.commit()
    connection.close()


def _base_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "p1-152-rehearsal"
    root.mkdir()
    database = root / "factory_snapshot.sqlite3"
    map_path = root / "published_map.json"
    output_dir = root / "audit"
    document = _create_map(map_path)
    _create_database(database, document)
    return database, map_path, output_dir


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, dict]:
    database, map_path, output_dir = _base_files(tmp_path)
    plan = MODULE.build_audit(database, map_path)
    paths = MODULE.write_audit_outputs(plan, output_dir)
    return database, map_path, paths["plan"], plan


def _execute(
    *,
    database: Path,
    map_path: Path,
    plan_path: Path,
    direction: str = "apply",
    token: str | None = None,
    approval_path: Path | None = None,
    backup_dir: Path | None = None,
    rehearse: bool = False,
    confirm_isolated_copy: bool = True,
) -> dict:
    return MODULE.execute_plan(
        database=database,
        plan_path=plan_path,
        published_map=map_path,
        actor_user_id=1,
        direction=direction,
        target_environment="isolated",
        confirm_isolated_copy=confirm_isolated_copy,
        confirm_formal_database=False,
        token=token
        or (MODULE.APPLY_TOKEN if direction == "apply" else MODULE.ROLLBACK_TOKEN),
        approval_path=approval_path,
        backup_dir=backup_dir,
        rehearse=rehearse,
    )


def _plan_rows(database: Path) -> list[tuple]:
    connection = sqlite3.connect(database)
    rows = connection.execute(
        """
        SELECT id,draft_map_revision,published_map_revision,preview_fingerprint,
               version,updated_by,updated_at
        FROM warehouse_ground_layout_plans ORDER BY id
        """
    ).fetchall()
    connection.close()
    return rows


def _log_count(database: Path, action_code: str | None = None) -> int:
    connection = sqlite3.connect(database)
    if action_code is None:
        count = connection.execute("SELECT count(*) FROM operation_logs").fetchone()[0]
    else:
        count = connection.execute(
            "SELECT count(*) FROM operation_logs WHERE action_code=?", (action_code,)
        ).fetchone()[0]
    connection.close()
    return int(count)


def _protected_state(database: Path) -> dict:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    snapshot = MODULE.protected_snapshot(connection)
    connection.close()
    return snapshot


def _trigger_sql(database: Path) -> str:
    connection = sqlite3.connect(database)
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
        (MODULE.MUTABLE_TRIGGER,),
    ).fetchone()
    connection.close()
    assert row is not None
    return str(row[0])


def test_audit_accepts_exact_two_plan_twenty_four_empty_slot_scope(
    tmp_path: Path,
) -> None:
    _database, _map_path, plan_path, plan = _fixture(tmp_path)

    assert plan_path.is_file()
    assert plan["hard_blockers"] == []
    assert plan["execution_gate"] == {
        "ready": True,
        "already_consistent": False,
        "candidate_plan_count": 2,
        "candidate_slot_count": 24,
        "formal_apply_ready": False,
        "reason": "候选只完成技术门禁；正式写入仍须停服、新备份、同批地图和正式确认文件。",
    }
    assert plan["scope"]["actual_plan_count"] == 2
    assert plan["scope"]["actual_slot_count"] == 24
    assert [row["area_code"] for row in plan["plans"]] == ["EDIT-001", "EDIT-002"]
    assert {row["disposition"] for row in plan["plans"]} == {
        "reference_refresh_candidate"
    }
    assert {tuple(row["legacy_preview_policy_versions"]) for row in plan["plans"]} == {
        (2,)
    }
    assert all(row["creation_evidence"] for row in plan["plans"])
    assert len(plan["slot_rows"]) == 24
    assert all(row["equivalent"] and not row["issues"] for row in plan["slot_rows"])


@pytest.mark.parametrize(
    ("drift", "expected_blocker"),
    [
        ("policy_revision", "policy_revision_not_current"),
        ("address_identity", "address_area_id_must_remain_null"),
        ("inventory", "location_not_empty"),
        ("zero_lot", "location_not_empty"),
        ("history", "one_step_creation_log_missing_or_ambiguous"),
        ("map_identity", "formal_area_id_mismatch"),
        ("geometry", "geometry_derivation_failed"),
    ],
)
def test_audit_fails_closed_for_policy_identity_inventory_history_or_geometry_drift(
    tmp_path: Path,
    drift: str,
    expected_blocker: str,
) -> None:
    database, map_path, _output_dir = _base_files(tmp_path)
    if drift in {"map_identity", "geometry"}:
        document = json.loads(map_path.read_text(encoding="utf-8"))
        feature = document["floors"]["4F"]["features"][0]
        if drift == "map_identity":
            feature["formal_area_id"] = 999
        else:
            feature["points"] = [[0, 0], [13000, 0], [13000, 1000], [0, 1000]]
        map_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    else:
        connection = sqlite3.connect(database)
        if drift == "policy_revision":
            connection.execute(
                "UPDATE warehouse_area_storage_policies SET published_map_revision='stale' WHERE area_id=57"
            )
        elif drift == "address_identity":
            connection.execute(
                "UPDATE warehouse_locations SET address_area_id=57 WHERE id=632"
            )
        elif drift == "inventory":
            connection.execute(
                "INSERT INTO inventory_lots VALUES (1,632,'active',1,0,0)"
            )
        elif drift == "zero_lot":
            connection.execute(
                "INSERT INTO inventory_lots VALUES (1,632,'depleted',0,0,0)"
            )
        else:
            connection.execute(
                "DELETE FROM operation_logs WHERE action='TWIN_ZONE_ONE_STEP_CONFIRM' AND details LIKE '%EDIT-001%'"
            )
        connection.commit()
        connection.close()

    plan = MODULE.build_audit(database, map_path)

    assert plan["execution_gate"]["ready"] is False
    assert any(expected_blocker in item for item in plan["hard_blockers"])


@pytest.mark.parametrize("head_mutation", ("zero", "multiple"))
def test_audit_fails_closed_when_database_has_zero_or_multiple_alembic_heads(
    tmp_path: Path, head_mutation: str
) -> None:
    database, map_path, _output_dir = _base_files(tmp_path)
    connection = sqlite3.connect(database)
    if head_mutation == "zero":
        connection.execute("DELETE FROM alembic_version")
    else:
        connection.execute("INSERT INTO alembic_version VALUES ('parallel_head')")
    connection.commit()
    connection.close()

    plan = MODULE.build_audit(database, map_path)

    assert plan["execution_gate"]["ready"] is False
    assert any("Alembic 必须且只能有一个" in item for item in plan["hard_blockers"])


def test_execute_rechecks_database_alembic_head_against_signed_plan(
    tmp_path: Path,
) -> None:
    database, map_path, plan_path, _plan = _fixture(tmp_path)
    connection = sqlite3.connect(database)
    connection.execute("UPDATE alembic_version SET version_num='newer_head'")
    connection.commit()
    connection.close()

    with pytest.raises(
        MODULE.Floor4RevisionReconciliationError,
        match="数据库 Alembic head 与 P1-152 只读计划不一致",
    ):
        _execute(
            database=database,
            map_path=map_path,
            plan_path=plan_path,
            rehearse=True,
        )


def test_rehearse_is_rejected_for_formal_target_before_any_database_write(
    tmp_path: Path,
) -> None:
    database, map_path, plan_path, _plan = _fixture(tmp_path)
    database_hash = MODULE.file_sha256(database)

    with pytest.raises(
        MODULE.Floor4RevisionReconciliationError,
        match="rehearse 只允许在隔离数据库副本执行",
    ):
        MODULE.execute_plan(
            database=database,
            plan_path=plan_path,
            published_map=map_path,
            actor_user_id=1,
            direction="apply",
            target_environment="formal",
            confirm_isolated_copy=False,
            confirm_formal_database=True,
            token=MODULE.APPLY_TOKEN,
            rehearse=True,
        )

    assert MODULE.file_sha256(database) == database_hash


def test_rehearse_rolls_back_every_byte_plan_row_log_and_trigger(
    tmp_path: Path,
) -> None:
    database, map_path, plan_path, _plan = _fixture(tmp_path)
    database_hash = MODULE.file_sha256(database)
    map_bytes = map_path.read_bytes()
    rows_before = _plan_rows(database)
    logs_before = _log_count(database)
    trigger_before = _trigger_sql(database)

    result = _execute(
        database=database,
        map_path=map_path,
        plan_path=plan_path,
        rehearse=True,
    )

    assert result["status"] == "rehearsed_and_rolled_back"
    assert result["changed_plans"] == 2
    assert result["affected_slots"] == 24
    assert result["checks"]["ok"] is True
    assert result["backup"] is None
    assert MODULE.file_sha256(database) == database_hash
    assert map_path.read_bytes() == map_bytes
    assert _plan_rows(database) == rows_before
    assert _log_count(database) == logs_before
    assert _trigger_sql(database) == trigger_before


def test_apply_and_rollback_are_audited_idempotent_and_leave_protected_facts_unchanged(
    tmp_path: Path,
) -> None:
    database, map_path, plan_path, plan = _fixture(tmp_path)
    source_by_id = {row["plan_id"]: row for row in plan["plans"]}
    protected_before = _protected_state(database)
    map_before = map_path.read_bytes()
    trigger_before = _trigger_sql(database)
    apply_approval = MODULE.write_approval_template(
        plan,
        plan_path.parent / "apply-approval.json",
        action="apply",
        target_environment="isolated",
    )

    applied = _execute(
        database=database,
        map_path=map_path,
        plan_path=plan_path,
        approval_path=apply_approval,
        backup_dir=plan_path.parent / "apply-backups",
    )

    assert applied["status"] == "applied"
    assert applied["changed_plans"] == 2
    assert applied["affected_slots"] == 24
    assert Path(applied["backup"]["path"]).is_file()
    assert applied["backup"]["checks"]["ok"] is True
    for row in _plan_rows(database):
        source = source_by_id[row[0]]
        assert row[1:5] == (
            CURRENT_REVISION,
            CURRENT_REVISION,
            source["target"]["preview_fingerprint"],
            source["source"]["version"] + 1,
        )
    assert _log_count(database, "P1_152_PLAN_REVISION_REFRESH") == 24
    assert _protected_state(database) == protected_before
    assert map_path.read_bytes() == map_before
    assert _trigger_sql(database) == trigger_before

    replay = _execute(
        database=database,
        map_path=map_path,
        plan_path=plan_path,
        approval_path=apply_approval,
        backup_dir=plan_path.parent / "apply-backups",
    )
    assert replay["status"] == "idempotent_replay"
    assert replay["backup"] is None
    assert _log_count(database, "P1_152_PLAN_REVISION_REFRESH") == 24

    rollback_approval = MODULE.write_approval_template(
        plan,
        plan_path.parent / "rollback-approval.json",
        action="rollback",
        target_environment="isolated",
    )
    rolled_back = _execute(
        database=database,
        map_path=map_path,
        plan_path=plan_path,
        direction="rollback",
        approval_path=rollback_approval,
        backup_dir=plan_path.parent / "rollback-backups",
    )

    assert rolled_back["status"] == "rolled_back"
    assert Path(rolled_back["backup"]["path"]).is_file()
    for row in _plan_rows(database):
        source = source_by_id[row[0]]
        assert row[1:5] == (
            source["source"]["draft_map_revision"],
            source["source"]["published_map_revision"],
            source["source"]["preview_fingerprint"],
            source["source"]["version"] + 2,
        )
    assert _log_count(database, "P1_152_PLAN_REVISION_ROLLBACK") == 24
    assert _protected_state(database) == protected_before
    assert map_path.read_bytes() == map_before
    assert _trigger_sql(database) == trigger_before

    rollback_replay = _execute(
        database=database,
        map_path=map_path,
        plan_path=plan_path,
        direction="rollback",
        approval_path=rollback_approval,
        backup_dir=plan_path.parent / "rollback-backups",
    )
    assert rollback_replay["status"] == "idempotent_replay"
    assert rollback_replay["backup"] is None
    assert _log_count(database, "P1_152_PLAN_REVISION_ROLLBACK") == 24


def test_mid_batch_plan_cas_drift_rolls_back_both_plans_and_all_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, map_path, plan_path, _plan = _fixture(tmp_path)
    plan = MODULE.load_plan(plan_path)
    rows_before = _plan_rows(database)
    logs_before = _log_count(database)
    trigger_before = _trigger_sql(database)
    original_update = MODULE._update_plan
    call_count = 0

    def racing_update(
        connection: sqlite3.Connection,
        *,
        plan_row: dict,
        actor_user_id: int,
        changed_at: str,
        direction: str,
    ) -> None:
        nonlocal call_count
        call_count += 1
        original_update(
            connection,
            plan_row=plan_row,
            actor_user_id=actor_user_id,
            changed_at=changed_at,
            direction=direction,
        )
        if call_count == 1:
            connection.execute(
                "UPDATE warehouse_ground_layout_plans SET version=version+1 WHERE id=23"
            )

    monkeypatch.setattr(MODULE, "_update_plan", racing_update)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")

    with pytest.raises(MODULE.Floor4RevisionReconciliationError, match="CAS"):
        MODULE._mutate(
            connection,
            plan,
            actor_user_id=1,
            direction="apply",
            rollback_after_validation=False,
        )
    connection.close()

    assert _plan_rows(database) == rows_before
    assert _log_count(database) == logs_before
    assert _trigger_sql(database) == trigger_before
    assert map_path.is_file()


def test_execute_rejects_isolation_token_approval_and_map_drift(
    tmp_path: Path,
) -> None:
    database, map_path, plan_path, plan = _fixture(tmp_path)
    rows_before = _plan_rows(database)
    with pytest.raises(MODULE.Floor4RevisionReconciliationError, match="confirm-isolated-copy"):
        _execute(
            database=database,
            map_path=map_path,
            plan_path=plan_path,
            rehearse=True,
            confirm_isolated_copy=False,
        )
    with pytest.raises(MODULE.Floor4RevisionReconciliationError, match="token"):
        _execute(
            database=database,
            map_path=map_path,
            plan_path=plan_path,
            rehearse=True,
            token="WRONG",
        )
    with pytest.raises(MODULE.Floor4RevisionReconciliationError, match="approval"):
        _execute(
            database=database,
            map_path=map_path,
            plan_path=plan_path,
            backup_dir=plan_path.parent / "backups",
        )

    wrong_approval = MODULE.write_approval_template(
        plan,
        plan_path.parent / "wrong-approval.json",
        action="rollback",
        target_environment="isolated",
    )
    with pytest.raises(MODULE.Floor4RevisionReconciliationError, match="approval"):
        _execute(
            database=database,
            map_path=map_path,
            plan_path=plan_path,
            approval_path=wrong_approval,
            backup_dir=plan_path.parent / "backups",
        )

    document = json.loads(map_path.read_text(encoding="utf-8"))
    document["floors"]["4F"]["revision"] = "newer-revision"
    map_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(MODULE.Floor4RevisionReconciliationError, match="4F 地图"):
        _execute(
            database=database,
            map_path=map_path,
            plan_path=plan_path,
            rehearse=True,
        )

    assert _plan_rows(database) == rows_before
    assert _log_count(database, "P1_152_PLAN_REVISION_REFRESH") == 0
