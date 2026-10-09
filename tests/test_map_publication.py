from copy import deepcopy
from datetime import datetime
import json

import pytest
from sqlalchemy import select

from tests.test_rack_map_application import rack_case, carry
from app.models.audit import OperationLog
from app.models.warehouse_inventory import WarehouseGroundLayoutPlan
from app.services.warehouse_area_activation import WarehouseAreaActivationError
from app.services.warehouse_map_publication import (
    unchanged_area, carry_unchanged_area_policies, assert_current_floor_bindings,
)
from app.services.warehouse_ground_map_application import _published_floor_plans


def apply(case, **kwargs):
    db, actor, _, old, new = case
    return carry_unchanged_area_policies(db, previous_floor_layout=old,
        floor_layout=new, actor=actor, operation_key="map-unification", **kwargs)


def first_floor(case):
    db, _, area, old, new = case
    area.floor.floor_code = "F1"
    area.floor.floor_number = 1
    old["floor_code"] = new["floor_code"] = "1F"
    db.commit()


def test_first_floor_rack_alias_is_carried(rack_case):
    first_floor(rack_case)
    assert carry(rack_case) == 1
    assert_current_floor_bindings(rack_case[0], rack_case[4])


@pytest.mark.parametrize("layout", ["rack", "pallet_ground", "mixed", "functional"])
def test_all_unchanged_layouts_carry_with_identity_and_replay_protection(rack_case, layout):
    db, _, area, old, new = rack_case
    first_floor(rack_case)
    area.storage_policy.storage_layout = layout
    old["features"][0]["storage_layout"] = new["features"][0]["storage_layout"] = layout
    db.commit()
    with pytest.raises(WarehouseAreaActivationError, match="尚有区域未同步"):
        assert_current_floor_bindings(db, new)
    assert apply(rack_case) == 1
    assert_current_floor_bindings(db, new)
    assert apply(rack_case) == 0
    assert area.storage_policy.version == 5
    logs = list(db.scalars(select(OperationLog)))
    assert len(logs) == 1
    assert json.loads(logs[0].details)["previous_map_revision"] == "old-map"
    db.rollback()
    assert area.storage_policy.published_map_revision == "old-map"
    assert area.storage_policy.version == 4
    assert list(db.scalars(select(OperationLog))) == []


@pytest.mark.parametrize("change", ["bounds", "rack", "pallet", "duplicate", "draft", "disabled", "usage", "old_source"])
def test_unproved_changes_never_become_current(rack_case, change):
    db, _, area, old, new = rack_case
    if change == "bounds": new["bounds_mm"] = dict(min_x=0, max_x=999)
    elif change == "rack": new["racks"][0]["levels"] = 4
    elif change == "pallet": new["pallets"] = [dict(id="p", area_feature_id="zone-c")]
    elif change == "duplicate": new["racks"].append(deepcopy(new["racks"][0]))
    elif change == "draft": area.storage_policy.draft_map_revision = "pending"
    elif change == "disabled": area.construction_status = "archived"
    elif change == "usage": area.storage_policy.allowed_inventory_types_json = '["mold"]'
    elif change == "old_source": area.storage_policy.published_map_revision = "older-than-provided-map"
    assert not unchanged_area(area, old, new)
    assert apply(rack_case) == 0
    if change != "disabled":
        with pytest.raises(WarehouseAreaActivationError): assert_current_floor_bindings(db, new)
    assert area.storage_policy.version == 4
    assert list(db.scalars(select(OperationLog))) == []


def test_removed_feature_cannot_commit_even_with_current_revision(rack_case):
    db, _, area, _, new = rack_case
    area.storage_policy.published_map_revision = new["revision"]
    new["features"] = []
    with pytest.raises(WarehouseAreaActivationError): assert_current_floor_bindings(db, new)


def test_empty_area_without_physical_racks_is_still_synchronized(rack_case):
    _, _, _, old, new = rack_case
    old["racks"] = []; new["racks"] = []
    assert apply(rack_case) == 1


def test_scoped_historical_repair_and_audit_failure_roll_back(rack_case, monkeypatch):
    import app.services.warehouse_map_publication as service
    db, _, area, _, _ = rack_case
    assert apply(rack_case, area_ids=set()) == 0
    assert apply(rack_case, excluded_feature_id="zone-c") == 0
    def fail(*args, **kwargs): raise RuntimeError("audit failure")
    monkeypatch.setattr(service, "append_audit_event", fail)
    with pytest.raises(RuntimeError): apply(rack_case, area_ids={area.id})
    db.rollback()
    assert area.storage_policy.published_map_revision == "old-map"


def test_ground_plans_also_use_stable_first_floor_identity(rack_case):
    db, actor, area, old, _ = rack_case
    first_floor(rack_case)
    plan = WarehouseGroundLayoutPlan(area_id=area.id, status="published", target_slot_count=1,
        numbering_origin="south", row_direction="from_aisle_inward", slot_direction="left_to_right",
        row_start_no=1, slot_start_no=1, draft_map_revision="old-map", published_map_revision="old-map",
        preview_fingerprint="a"*64, publish_idempotency_key="test-first-floor",
        publish_request_hash="b"*64, updated_by=actor.id, published_by=actor.id,
        published_at=datetime(2026,10,9))
    db.add(plan); db.commit()
    assert [p.id for p in _published_floor_plans(db, old)] == [plan.id]
    area.construction_status = "archived"
    db.flush()
    assert _published_floor_plans(db, old) == []


def test_production_read_and_edit_never_revive_seed_map(tmp_path, monkeypatch):
    from app.services import warehouse_twin_layout as reader, warehouse_twin_layout_editor as editor
    baseline = tmp_path / "old-map.json"
    baseline.write_text('{"schema_version":1,"floors":{}}', encoding="utf-8")
    runtime = tmp_path / "missing-runtime.json"
    monkeypatch.setenv("ERP_ENVIRONMENT", "production")
    monkeypatch.delenv("ERP_UAT_ROOT", raising=False)
    monkeypatch.setattr(reader, "TWIN_LAYOUT_RUNTIME_PATH", runtime)
    monkeypatch.setattr(reader, "TWIN_LAYOUT_PATH", baseline)
    monkeypatch.setattr(editor, "TWIN_LAYOUT_PATH", runtime)
    monkeypatch.setattr(editor, "TWIN_LAYOUT_BASELINE_PATH", baseline)
    with pytest.raises(reader.WarehouseTwinLayoutNotFoundError): reader.load_warehouse_twin_floor("1F")
    with pytest.raises(editor.WarehouseTwinLayoutEditNotFoundError): editor.load_published_warehouse_twin_floor_for_edit("1F")
    assert not runtime.exists()
