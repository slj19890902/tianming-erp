from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import Base
from app.models.audit import OperationLog
from app.models.user import User
from app.models.warehouse_inventory import WarehouseFloor, WarehouseArea, WarehouseAreaStoragePolicy
from app.services.warehouse_rack_map_application import carry_unchanged_rack_policies, unchanged_rack_area
from app.services.warehouse_location_address import published_measured_map_readiness


@pytest.fixture
def rack_case(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "rack.db"))
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(username="rack-admin", password_hash="unused", role="admin", real_name="测试")
        floor = WarehouseFloor(floor_code="3F", floor_name="三楼", floor_number=3, construction_status="enabled")
        area = WarehouseArea(floor=floor, area_code="EDIT-083", area_name="C货架", construction_status="enabled")
        area.storage_policy = WarehouseAreaStoragePolicy(map_feature_id="zone-c", storage_layout="rack",
            status="published", published_map_revision="old-map", allowed_inventory_types_json='["finished"]', version=4)
        db.add_all([user, area]); db.commit()
        source = dict(floor_code="3F", revision="old-map", features=[dict(id="zone-c", feature_kind="zone",
            erp_area_code="EDIT-083", storage_layout="rack", formal_area_name="C货架",
            allowed_inventory_types=["finished"], points=[[0,0],[100,0],[100,100]])],
            racks=[dict(id="rack-h1", area_feature_id="zone-c", area_code="EDIT-083", name="货H1", levels=3, level_cell_counts=[2,2,2])])
        target = deepcopy(source); target["revision"] = "new-map"
        yield db, user, area, source, target
    engine.dispose()


def carry(case, **kwargs):
    db, user, _, source, target = case
    return carry_unchanged_rack_policies(db, previous_floor_layout=source, floor_layout=target,
                                       actor=user, operation_key="one-area-publish", **kwargs)


def test_print_gate_recovers_only_after_verified_carry_and_replay_is_noop(rack_case):
    db, _, area, source, target = rack_case
    location = SimpleNamespace(is_active=True, placement_status="placed", warehouse_floor=3,
        area_code=area.area_code, source_version="CURRENT_MAP", address_kind="rack_slot", storage_type="rack")
    def readiness():
        return published_measured_map_readiness(location, floor=area.floor, area=area, policy=area.storage_policy,
            published_floor_identity=dict(revision=target["revision"], zones_by_id={"zone-c":"EDIT-083"}), has_geometry=True)
    assert readiness().issue == "该库位所属区域的发布版本不是当前运行地图版本"
    assert carry(rack_case, excluded_feature_id="different-selected-zone", audit_source="script") == 1
    assert readiness().position_status == "mapped"
    assert area.storage_policy.version == 5
    assert area.storage_policy.published_map_revision == "new-map"
    assert carry(rack_case) == 0
    logs = list(db.scalars(select(OperationLog)))
    assert len(logs) == 1 and logs[0].action_code == "warehouse.rack_area.map_application"
    assert logs[0].source == "script"
    assert source["revision"] == "old-map"
    db.rollback()
    assert area.storage_policy.published_map_revision == "old-map"
    assert area.storage_policy.version == 4
    assert list(db.scalars(select(OperationLog))) == []


@pytest.mark.parametrize("change", ["rack", "feature", "draft", "archived", "stale", "usage", "missing", "duplicate", "renamed", "floor"])
def test_changed_or_unproven_area_is_never_carried(rack_case, change):
    db, _, area, source, target = rack_case
    if change == "rack": target["racks"][0]["level_cell_counts"] = [2,3,2]
    elif change == "feature": target["features"][0]["points"][0] = [1,1]
    elif change == "draft": area.storage_policy.draft_map_revision = "unsaved"
    elif change == "archived":
        policy = area.storage_policy
        policy.archived_by = rack_case[1].id
        policy.archived_at = datetime(2026, 10, 9)
        policy.archive_operation_key = "test-archive"
        policy.archive_request_hash = "a" * 64
        policy.archive_feature_snapshot_json = "{}"
        policy.status = "archived"
    elif change == "stale": area.storage_policy.published_map_revision = "another-map"
    elif change == "usage": area.storage_policy.allowed_inventory_types_json = '["mold"]'
    elif change == "missing": target["racks"] = []
    elif change == "duplicate": target["features"].append(deepcopy(target["features"][0]))
    elif change == "renamed": area.area_name = "未发布的新名字"
    elif change == "floor": target["floor_code"] = "1F"
    assert not unchanged_rack_area(area, source, target)
    assert carry(rack_case) == 0
    assert area.storage_policy.version == 4
    assert list(db.scalars(select(OperationLog))) == []


def test_selected_area_and_repair_scope_are_excluded(rack_case):
    assert carry(rack_case, excluded_feature_id="zone-c") == 0
    assert carry(rack_case, area_ids=set()) == 0


def test_audit_failure_does_not_commit_policy_change(rack_case, monkeypatch):
    import app.services.warehouse_rack_map_application as service
    db, _, area, _, _ = rack_case
    def fail(*args, **kwargs): raise RuntimeError("audit failed")
    monkeypatch.setattr(service, "append_audit_event", fail)
    with pytest.raises(RuntimeError, match="audit failed"):
        carry(rack_case)
    db.rollback()
    assert area.storage_policy.version == 4
    assert area.storage_policy.published_map_revision == "old-map"
