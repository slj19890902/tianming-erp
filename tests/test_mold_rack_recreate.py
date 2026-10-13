from copy import deepcopy
import json

import pytest

from app.services import warehouse_twin_layout_editor as editor
from app.services.mold_cells import projected_cells
from test_warehouse_twin_layout_editor import _asset, _rack_values


def floor(path):
    return json.loads(path.read_text())["floors"]["3F"]


def write_floor(path, value):
    doc = json.loads(path.read_text())
    value["revision"] = editor._floor_revision(value)
    doc["floors"]["3F"] = value
    path.write_text(json.dumps(doc))


def seed(tmp_path, code="C"):
    path = _asset(tmp_path / "layout.json")
    value = floor(path)
    value["features"][0]["allowed_inventory_types"] = ["mold"]
    write_floor(path, value)
    values = _rack_values(name=f"模具{code}架", mold_rack_code=code,
                          levels=2, level_heights_mm=[1000], level_cell_counts=[2, 2])
    created = editor.create_warehouse_twin_rack("3F", expected_revision=floor(path)["revision"],
        operation_key="first-create-"+code, area_feature_id="zone-f1", values=values, path=path)
    deleted = editor.delete_warehouse_twin_rack("3F", created.value["id"],
        expected_revision=created.floor_revision, expected_version=created.value["version"],
        operation_key="first-delete-"+code, path=path)
    return path, values, created, deleted


def restore(path, values, key="restore-mold-001", revision=None):
    return editor.create_warehouse_twin_rack("3F", expected_revision=revision or floor(path)["revision"],
        operation_key=key, area_feature_id="zone-f1", values=values, path=path)


@pytest.mark.parametrize("code", ["C", "D", "E"])
def test_deleted_mold_letter_can_be_reused_without_changing_cell_or_rack_identity(tmp_path, code):
    path, values, original, _ = seed(tmp_path, code)
    before = floor(path)
    archived = deepcopy(before["retired_racks"][0])
    result = restore(path, {**values, "x_mm":6000, "name":f"模具{code}架重新启用"})
    assert result.applied
    assert result.value["id"] == original.value["id"]
    assert result.value["rack_code"] == original.value["rack_code"]
    assert projected_cells(result.value) == projected_cells(original.value)
    assert result.value["version"] > original.value["version"]
    assert result.value["restoration_history"] == [archived]
    assert result.value["x_mm"] == 6000
    assert "retired_at" not in result.value
    after = floor(path)
    assert after["retired_racks"] == []
    assert before["layout_edit_receipts"] == after["layout_edit_receipts"][:-1]


def test_restore_replay_and_repeated_delete_restore_keep_each_snapshot(tmp_path):
    path, values, original, _ = seed(tmp_path)
    first = restore(path, values)
    before = path.read_bytes()
    replay = restore(path, values, revision="old-revision")
    assert not replay.applied and replay.value == first.value and path.read_bytes() == before
    editor.delete_warehouse_twin_rack("3F", first.value["id"], expected_revision=first.floor_revision,
        expected_version=first.value["version"], operation_key="second-delete-C", path=path)
    second = restore(path, values, key="second-restore-C")
    assert len(second.value["restoration_history"]) == 2
    assert second.value["id"] == original.value["id"]
    assert second.value["mold_cells"] == original.value["mold_cells"]
    assert second.value["version"] == first.value["version"] + 1


def test_restore_same_key_with_changed_structure_is_rejected_without_writes(tmp_path):
    path, values, _, _ = seed(tmp_path)
    restore(path, values)
    before = path.read_bytes()
    with pytest.raises(editor.WarehouseTwinLayoutEditConflictError, match="操作键"):
        restore(path, {**values, "width_mm":3000})
    assert path.read_bytes() == before


@pytest.mark.parametrize("issue", ["active", "ambiguous", "id_collision", "locked", "old_revision", "supplied_cells", "not_mold", "capacity", "missing_code", "bad_version", "bad_history"])
def test_invalid_restore_keeps_layout_and_history_unchanged(tmp_path, issue):
    path, values, original, deleted = seed(tmp_path)
    value = floor(path)
    revision = None
    if issue == "active": value["racks"].append(deepcopy(original.value))
    elif issue == "ambiguous": value["retired_racks"].append({**deepcopy(value["retired_racks"][0]), "id":"another-identity"})
    elif issue == "id_collision": value["racks"].append({**deepcopy(original.value), "mold_rack_code":"B"})
    elif issue == "locked": value["retired_racks"][0]["is_locked"] = True
    elif issue == "old_revision": revision = original.floor_revision
    elif issue == "supplied_cells": values = {**values, "mold_cells":original.value["mold_cells"]}
    elif issue == "not_mold": value["features"][0]["allowed_inventory_types"] = ["finished"]
    elif issue == "capacity": value["features"][0]["max_rack_count"] = 0
    elif issue == "missing_code": value["retired_racks"][0].pop("rack_code")
    elif issue == "bad_version": value["retired_racks"][0]["version"] = "invalid"
    elif issue == "bad_history": value["retired_racks"][0]["restoration_history"] = "invalid"
    write_floor(path, value)
    before = path.read_bytes()
    with pytest.raises(editor.WarehouseTwinLayoutEditError): restore(path, values, revision=revision)
    assert path.read_bytes() == before


def test_changed_structure_preserves_old_cells_and_retired_short_aliases(tmp_path):
    path, values, original, _ = seed(tmp_path)
    result = restore(path, {**values, "level_cell_counts":[2,1]})
    old_ids = {c["id"] for c in original.value["mold_cells"]}
    active_ids = {c["id"] for c in result.value["mold_cells"]}
    assert len(active_ids) == 3 and active_ids.issubset(old_ids)
    assert {c["id"] for c in result.value["retired_mold_cells"]} == old_ids - active_ids
    assert result.value["restoration_history"][0]["mold_cells"] == original.value["mold_cells"]


def test_rack_code_allocator_reserves_retired_internal_codes(tmp_path):
    path, values, original, _ = seed(tmp_path)
    # A different new physical rack must not steal the removed rack's address.
    other = restore(path, {**values, "mold_rack_code":"B", "name":"模具B架"}, key="new-different-B")
    assert other.value["rack_code"] != original.value["rack_code"]
    restored = restore(path, values)
    assert restored.value["rack_code"] == original.value["rack_code"]


@pytest.fixture
def real_case(tmp_path, monkeypatch):
    from sqlalchemy import select
    from app.api import warehouse as api
    from app.models.user import User
    from app.services import mold_location
    from test_p1_47b_warehouse_area_planning import _isolate_layout_paths, _database, _request, _confirm_area_payload, _revision
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    runtime = editor.TWIN_LAYOUT_PATH
    def live(code):
        source = runtime if runtime.exists() else published
        floors = json.loads(source.read_text())["floors"]
        if code not in floors:
            from app.services.warehouse_twin_layout import WarehouseTwinLayoutNotFoundError
            raise WarehouseTwinLayoutNotFoundError(code)
        return floors[code]
    monkeypatch.setattr(api, "load_warehouse_twin_floor", live)
    monkeypatch.setattr(mold_location, "load_warehouse_twin_floor", live)
    monkeypatch.setattr(api, "list_production_projection_mappings", lambda *a,**k: [])
    engine, factory = _database(tmp_path)
    with factory() as db:
        admin = db.scalar(select(User))
        api.confirm_twin_zone_area("3F", "zone-f1", _confirm_area_payload(
            revision=_revision(published), operation_key="restore-area-create", usage="mold", storage_layout="rack",
            area_name="匿名三楼模具区", capacity=5), _request(), db, admin)
        values = _rack_values(name="模具C架", mold_rack_code="C", levels=2, level_heights_mm=[1000], level_cell_counts=[2,2])
        created = api.create_twin_layout_rack("3F", api.TwinRackLayoutCreatePayload(
            **values, expected_revision=live("3F")["revision"], operation_key="real-create-rack-C", area_feature_id="zone-f1"), _request(), db, admin)
        applied = api.apply_twin_rack_layout("3F", created["item"]["id"], api.TwinZoneGeometryApplyPayload(
            expected_revision=created["revision"], expected_published_revision=live("3F")["revision"],
            expected_version=created["item"]["version"], operation_key="real-first-apply-C"), _request(), db, admin)
        current = live("3F")
        old = deepcopy(current["racks"][0])
        deleted = api.delete_twin_layout_rack("3F", old["id"], expected_revision=current["revision"],
            expected_published_revision=current["revision"], expected_version=old["version"],
            operation_key="real-first-delete-C", request=_request(), db=db, user=admin)
    yield dict(factory=factory, live=live, runtime=runtime, draft=draft, values=values, old=old, deleted=deleted,
               request=_request, api=api, delete_revision=current["revision"])
    engine.dispose()


def recreate_real(case, db, admin, key="real-recreate-C"):
    api = case["api"]
    return api.create_twin_layout_rack("3F", api.TwinRackLayoutCreatePayload(
        **case["values"], expected_revision=editor.load_effective_warehouse_twin_floor_for_edit("3F")["revision"],
        operation_key=key, area_feature_id="zone-f1"), case["request"](), db, admin)


def test_real_delete_recreate_apply_preserves_ledger_and_only_consumes_target_archive(real_case):
    from sqlalchemy import select, func
    from app.models.user import User
    from app.models.audit import OperationLog
    from app.models.mold_tool import MoldTool, MoldLocationMovement
    case = real_case; api = case["api"]
    with case["factory"]() as db:
        admin = db.scalar(select(User))
        created = recreate_real(case, db, admin)
        assert created["item"]["id"] == case["old"]["id"]
        assert case["live"]("3F")["racks"] == []  # Saved planning is not a physical move.
        assert case["live"]("3F")["retired_racks"][0]["id"] == case["old"]["id"]
        replay = recreate_real(case, db, admin)
        assert not replay["applied"]
        assert db.scalar(select(func.count(OperationLog.id)).where(OperationLog.action == "TWIN_MOLD_RACK_RESTORE")) == 1
        # Keep a separate un-applied rack draft; applying C must not publish it.
        extra = editor.create_warehouse_twin_rack("3F", expected_revision=created["revision"],
            operation_key="unrelated-new-D", area_feature_id="zone-f1",
            values={**case["values"], "mold_rack_code":"D", "name":"模具D架", "x_mm":8500})
        applied = api.apply_twin_rack_layout("3F", created["item"]["id"], api.TwinZoneGeometryApplyPayload(
            expected_revision=extra.floor_revision, expected_published_revision=case["live"]("3F")["revision"],
            expected_version=created["item"]["version"], operation_key="real-restore-apply-C"), case["request"](), db, admin)
        assert applied["applied"]
        current = case["live"]("3F")
        assert [r["mold_rack_code"] for r in current["racks"]] == ["C"]
        assert not any(r["id"] == case["old"]["id"] for r in current["retired_racks"])
        physical = current["racks"][0]
        assert physical["mold_cells"] == case["old"]["mold_cells"]
        advanced = editor.load_effective_warehouse_twin_floor_for_edit("3F")
        assert {r["mold_rack_code"] for r in advanced["racks"]} == {"C","D"}
        assert db.scalar(select(func.count(MoldTool.id))) == 0
        assert db.scalar(select(func.count(MoldLocationMovement.id))) == 0
        # Old deletion confirmation never removes the revived physical rack again.
        before = case["runtime"].read_bytes()
        old_replay = api.delete_twin_layout_rack("3F", case["old"]["id"],
            expected_revision=case["delete_revision"], expected_published_revision=case["delete_revision"],
            expected_version=case["old"]["version"], operation_key="real-first-delete-C", request=case["request"](), db=db, user=admin)
        assert old_replay["idempotent_replay"] and case["runtime"].read_bytes() == before


@pytest.mark.parametrize("failure", ["audit", "commit"])
def test_recreate_failure_rolls_back_archive_and_all_draft_changes(real_case, monkeypatch, failure):
    from sqlalchemy import select, func
    from app.models.user import User
    from app.models.audit import OperationLog
    case = real_case
    def fail(*args, **kwargs): raise RuntimeError("injected restore transaction failure")
    before = editor.snapshot_warehouse_twin_layout_draft()
    published = case["runtime"].read_bytes()
    with case["factory"]() as db:
        admin = db.scalar(select(User))
        audits = db.scalar(select(func.count(OperationLog.id)))
        if failure == "audit": monkeypatch.setattr(case["api"], "_twin_layout_asset_log", fail)
        else: monkeypatch.setattr(db, "commit", fail)
        with pytest.raises(RuntimeError, match="injected"): recreate_real(case, db, admin)
        assert editor.snapshot_warehouse_twin_layout_draft() == before
        assert case["runtime"].read_bytes() == published
        assert db.scalar(select(func.count(OperationLog.id))) == audits


@pytest.mark.parametrize("issue", ["other_floor", "archived_area"])
def test_real_restore_keeps_other_floor_and_archived_area_protections(real_case, monkeypatch, issue):
    from sqlalchemy import select
    from fastapi import HTTPException
    from app.models.user import User
    from app.models.warehouse_inventory import WarehouseArea
    case = real_case
    with case["factory"]() as db:
        admin = db.scalar(select(User))
        if issue == "other_floor":
            original = case["api"].load_effective_warehouse_twin_floor_for_edit
            def other_floor(code):
                if code == "1F": return {"racks":[], "retired_racks":[deepcopy(case["old"])]}
                return original(code)
            monkeypatch.setattr(case["api"], "load_effective_warehouse_twin_floor_for_edit", other_floor)
        else:
            area = db.scalar(select(WarehouseArea))
            area.construction_status = "archived"; db.commit()
        before = editor.snapshot_warehouse_twin_layout_draft()
        with pytest.raises(HTTPException) as error: recreate_real(case, db, admin)
        assert error.value.status_code == 409
        assert editor.snapshot_warehouse_twin_layout_draft() == before


def test_restored_rack_apply_failure_rolls_back_both_maps_and_keeps_delete_history(real_case, monkeypatch):
    from sqlalchemy import select, func
    from app.models.user import User
    from app.models.audit import OperationLog
    case = real_case; api = case["api"]
    with case["factory"]() as db:
        admin = db.scalar(select(User))
        created = recreate_real(case, db, admin)
        before = editor.snapshot_warehouse_twin_publish_state()
        audits = db.scalar(select(func.count(OperationLog.id)))
        def fail(*args, **kwargs): raise RuntimeError("injected apply commit failure")
        monkeypatch.setattr(db, "commit", fail)
        with pytest.raises(RuntimeError, match="injected apply"):
            api.apply_twin_rack_layout("3F", created["item"]["id"], api.TwinZoneGeometryApplyPayload(
                expected_revision=created["revision"], expected_published_revision=case["live"]("3F")["revision"],
                expected_version=created["item"]["version"], operation_key="restore-apply-failure"), case["request"](), db, admin)
        assert editor.snapshot_warehouse_twin_publish_state() == before
        assert db.scalar(select(func.count(OperationLog.id))) == audits
        assert case["live"]("3F")["racks"] == []
        assert case["live"]("3F")["retired_racks"][0]["id"] == case["old"]["id"]
