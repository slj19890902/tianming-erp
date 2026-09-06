from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from app.services import warehouse_twin_layout_editor as editor
from app.services.warehouse_twin_layout_editor import (
    WarehouseTwinLayoutEditConflictError,
    WarehouseTwinLayoutEditError,
    WarehouseTwinLayoutEditNotFoundError,
    _floor_revision,
    begin_warehouse_twin_one_step_rack_publish,
    create_warehouse_twin_rack,
    delete_warehouse_twin_rack,
    discard_warehouse_twin_layout_draft,
    load_warehouse_twin_layout_draft,
    publish_warehouse_twin_layout_draft,
    rebuild_stale_warehouse_twin_layout_draft,
    rebase_warehouse_twin_advanced_rack_after_one_step,
    update_warehouse_twin_rack,
    update_warehouse_twin_zone_policy,
    validate_warehouse_twin_layout_draft,
)
from factory_twin.scripts.export_erp_twin_floor_maps import preserve_operator_layout_edits


ROOT = Path(__file__).resolve().parents[1]


def _asset(path: Path) -> Path:
    floor = {
        "layout_id": "layout-3f",
        "floor_code": "3F",
        "features": [
            {
                "id": "zone-f1",
                "feature_code": "ZONE-3F-ERP-F1",
                "name": "F1",
                "feature_kind": "zone",
                "subtype": "rack_storage",
                "points": [[0, 0], [10000, 0], [10000, 10000], [0, 10000]],
                "version": 1,
                "erp_area_code": "F1",
            }
        ],
        "racks": [],
        "pallets": [],
    }
    floor["revision"] = _floor_revision(floor)
    path.write_text(
        json.dumps({"schema_version": 1, "generated_at": "old", "floors": {"3F": floor}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _rack_values(**changes) -> dict:
    values = {
        "name": "F1测试货架",
        "x_mm": 5000,
        "y_mm": 5000,
        "width_mm": 2800,
        "depth_mm": 1100,
        "height_mm": 2200,
        "levels": 3,
        "level_heights_mm": [700, 1450],
        "cargo_rows": 4,
        "level_cell_counts": [0, 0, 0],
        "bays": 1,
        "access_side": "south",
        "min_aisle_width_mm": 1500,
        "rotation_deg": 0,
        "color": "#38bdf8",
    }
    values.update(changes)
    return values


def test_area_rack_numbers_preserve_identity_geometry_and_are_explicit(tmp_path: Path) -> None:
    path = _asset(tmp_path / "numbering.json")
    document = json.loads(path.read_text(encoding="utf8"))
    document["floors"]["3F"]["features"][0]["formal_area_name"] = "左区成品C2"
    document["floors"]["3F"]["revision"] = _floor_revision(document["floors"]["3F"])
    path.write_text(json.dumps(document), encoding="utf8")
    rev = json.loads(path.read_text(encoding="utf8"))["floors"]["3F"]["revision"]
    for index, (x, y, levels) in enumerate([(6000, 5000, 3), (2000, 4000, 4), (2000, 6000, 3)]):
        created = create_warehouse_twin_rack("3F", expected_revision=rev,
            operation_key=f"number-create-{index}", area_feature_id="zone-f1", path=path,
            values=_rack_values(x_mm=x, y_mm=y, levels=levels,
                level_heights_mm=[500 * i for i in range(1, levels)], level_cell_counts=[2] * levels))
        rev = created.floor_revision
    before = json.loads(path.read_text(encoding="utf8"))["floors"]["3F"]
    numbered = editor.number_warehouse_twin_area_racks("3F", expected_revision=rev,
        operation_key="number-area-first", area_feature_id="zone-f1", path=path)
    assert [r["x_mm"] for r in numbered.value["racks"]] == [2000, 2000, 6000]
    assert [r["y_mm"] for r in numbered.value["racks"]] == [6000, 4000, 5000]
    assert [r["name"] for r in numbered.value["racks"]] == ["左区成品C2 01号架", "左区成品C2 02号架", "左区成品C2 03号架"]
    originals = {r["id"]: r for r in before["racks"]}
    for r in numbered.value["racks"]:
        assert {k: v for k, v in r.items() if k not in {"name", "version", "status"}} == {
            k: v for k, v in originals[r["id"]].items() if k not in {"name", "version", "status"}}
    assert json.loads(path.read_text(encoding="utf8"))["floors"]["3F"]["features"] == before["features"]
    bytes_after = path.read_bytes()
    replay = editor.number_warehouse_twin_area_racks("3F", expected_revision=rev,
        operation_key="number-area-first", area_feature_id="zone-f1", path=path)
    assert not replay.applied and path.read_bytes() == bytes_after
    with pytest.raises(WarehouseTwinLayoutEditConflictError):
        editor.number_warehouse_twin_area_racks("3F", expected_revision=rev,
            operation_key="number-area-stale", area_feature_id="zone-f1", path=path)
    assert path.read_bytes() == bytes_after


@pytest.mark.parametrize("guard", ["is_locked", "mold_rack_code"])
def test_area_rack_numbering_rejects_protected_racks_without_partial_write(tmp_path: Path, guard: str) -> None:
    path = _asset(tmp_path / "protected.json")
    rev = json.loads(path.read_text(encoding="utf8"))["floors"]["3F"]["revision"]
    created = create_warehouse_twin_rack("3F", expected_revision=rev, operation_key="protected-create",
        area_feature_id="zone-f1", values=_rack_values(), path=path)
    document = json.loads(path.read_text(encoding="utf8"));floor = document["floors"]["3F"]
    floor["racks"][0][guard] = True if guard == "is_locked" else "R01"
    floor["revision"] = _floor_revision(floor)
    path.write_text(json.dumps(document), encoding="utf8");before = path.read_bytes()
    with pytest.raises(WarehouseTwinLayoutEditConflictError):
        editor.number_warehouse_twin_area_racks("3F", expected_revision=floor["revision"],
            operation_key="protected-number", area_feature_id="zone-f1", path=path)
    assert path.read_bytes() == before


def test_rack_crud_is_versioned_idempotent_and_deletion_is_recoverable(tmp_path: Path) -> None:
    path = _asset(tmp_path / "layout.json")
    revision = json.loads(path.read_text(encoding="utf-8"))["floors"]["3F"]["revision"]
    created = create_warehouse_twin_rack(
        "3f",
        expected_revision=revision,
        operation_key="create-rack-0001",
        area_feature_id="zone-f1",
        values=_rack_values(),
        path=path,
    )
    assert created.applied is True
    assert created.value["area_code"] == "F1"
    assert created.value["rack_code"] == "RACK-3F-F1-EDIT-001"
    assert created.value["level_cell_counts"] == [0, 0, 0]
    assert created.value["cell_plan_status"] == "pending_admin_configuration"

    retried = create_warehouse_twin_rack(
        "3F",
        expected_revision=revision,
        operation_key="create-rack-0001",
        area_feature_id="zone-f1",
        values=_rack_values(),
        path=path,
    )
    assert retried.applied is False
    assert retried.value["id"] == created.value["id"]

    updated = update_warehouse_twin_rack(
        "3F",
        created.value["id"],
        expected_revision=created.floor_revision,
        expected_version=1,
        operation_key="update-rack-0001",
        values=_rack_values(
            name="F1现场货架",
            width_mm=5600,
            rotation_deg=90,
            level_cell_counts=[1, 4, 7],
        ),
        path=path,
    )
    assert updated.value["width_mm"] == 5600
    assert updated.value["rotation_deg"] == 90
    assert updated.value["level_cell_counts"] == [1, 4, 7]
    assert updated.value["cell_plan_status"] == "configured"
    assert updated.value["version"] == 2

    with pytest.raises(WarehouseTwinLayoutEditConflictError):
        update_warehouse_twin_rack(
            "3F",
            created.value["id"],
            expected_revision=updated.floor_revision,
            expected_version=1,
            operation_key="update-rack-stale",
            values=_rack_values(),
            path=path,
        )

    deleted = delete_warehouse_twin_rack(
        "3F",
        created.value["id"],
        expected_revision=updated.floor_revision,
        expected_version=2,
        operation_key="delete-rack-0001",
        path=path,
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    floor = document["floors"]["3F"]
    assert deleted.value["inventory_changed"] is False
    assert floor["racks"] == []
    assert floor["retired_racks"][0]["id"] == created.value["id"]
    assert len(floor["layout_edit_receipts"]) == 3


@pytest.mark.parametrize(
    "level_cell_counts",
    ([0, 1], [-1, 1, 1], [1, 1, 51]),
)
def test_rack_level_cell_counts_must_match_levels_and_stay_within_range(
    tmp_path: Path,
    level_cell_counts: list[int],
) -> None:
    path = _asset(tmp_path / "layout.json")
    revision = json.loads(path.read_text(encoding="utf-8"))["floors"]["3F"]["revision"]
    with pytest.raises(WarehouseTwinLayoutEditError):
        create_warehouse_twin_rack(
            "3F",
            expected_revision=revision,
            operation_key="create-rack-invalid-cells",
            area_feature_id="zone-f1",
            values=_rack_values(level_cell_counts=level_cell_counts),
            path=path,
        )


def test_zone_policy_keeps_business_usage_separate_from_storage_layout(tmp_path: Path) -> None:
    path = _asset(tmp_path / "layout.json")
    revision = json.loads(path.read_text(encoding="utf-8"))["floors"]["3F"]["revision"]
    result = update_warehouse_twin_zone_policy(
        "3F",
        "zone-f1",
        expected_revision=revision,
        expected_version=1,
        operation_key="zone-policy-0001",
        allowed_inventory_types=["finished", "semi_finished", "raw_material"],
        storage_layout="mixed",
        path=path,
    )
    assert result.value["allowed_inventory_types"] == ["finished", "semi_finished", "raw_material"]
    assert result.value["storage_layout"] == "mixed"
    assert result.value["subtype"] == "rack_storage"


def test_draft_edit_validate_and_publish_are_separate_versioned_steps(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published = _asset(tmp_path / "published.json")
    draft = tmp_path / "runtime" / "layout.draft.json"
    backups = tmp_path / "backups"
    monkeypatch.setattr(editor, "TWIN_LAYOUT_PATH", published)
    monkeypatch.setattr(editor, "TWIN_LAYOUT_DRAFT_PATH", draft)
    monkeypatch.setattr(editor, "TWIN_LAYOUT_BACKUP_DIR", backups)
    before = sha256(published.read_bytes()).hexdigest()
    revision = json.loads(published.read_text(encoding="utf-8"))["floors"]["3F"]["revision"]

    created = create_warehouse_twin_rack(
        "3F",
        expected_revision=revision,
        operation_key="draft-create-rack-0001",
        area_feature_id="zone-f1",
        values=_rack_values(level_cell_counts=[1, 2, 3]),
    )
    assert created.applied is True
    assert draft.is_file()
    assert sha256(published.read_bytes()).hexdigest() == before
    assert json.loads(published.read_text(encoding="utf-8"))["floors"]["3F"]["racks"] == []

    preview = load_warehouse_twin_layout_draft("3F")
    assert preview["draft_control"]["status"] == "draft"
    assert preview["draft_control"]["published_revision"] == revision
    assert preview["racks"][0]["id"] == created.value["id"]

    validated = validate_warehouse_twin_layout_draft(
        "3F",
        expected_revision=created.floor_revision,
    )
    assert validated.value["status"] == "validated"
    assert validated.value["blockers"] == []

    published_result = publish_warehouse_twin_layout_draft(
        "3F",
        expected_published_revision=revision,
        expected_draft_revision=created.floor_revision,
        operation_key="publish-layout-0001",
    )
    assert published_result.applied is True
    assert published_result.value["inventory_changed"] is False
    assert published_result.value["published_revision"] == created.floor_revision
    final_document = json.loads(published.read_text(encoding="utf-8"))
    assert final_document["floors"]["3F"]["racks"][0]["id"] == created.value["id"]
    assert "draft_meta" not in final_document
    backup = backups / published_result.value["backup_name"]
    assert backup.is_file()
    assert sha256(backup.read_bytes()).hexdigest() == before

    repeated = publish_warehouse_twin_layout_draft(
        "3F",
        expected_published_revision=revision,
        expected_draft_revision=created.floor_revision,
        operation_key="publish-layout-0001",
    )
    assert repeated.applied is False
    assert len(list(backups.glob("*.json"))) == 1


def test_one_step_rack_publish_consumes_only_selected_rack(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published = _asset(tmp_path / "published.json")
    draft = tmp_path / "runtime" / "layout.draft.json"
    backups = tmp_path / "backups"
    monkeypatch.setattr(editor, "TWIN_LAYOUT_PATH", published)
    monkeypatch.setattr(editor, "TWIN_LAYOUT_DRAFT_PATH", draft)
    monkeypatch.setattr(editor, "TWIN_LAYOUT_BACKUP_DIR", backups)
    published_revision = json.loads(published.read_text(encoding="utf8"))["floors"]["3F"]["revision"]
    first = create_warehouse_twin_rack(
        "3F", expected_revision=published_revision, operation_key="rack-one-step-first",
        area_feature_id="zone-f1", values=_rack_values(name="一号架", level_cell_counts=[4, 4, 4]),
    )
    second = create_warehouse_twin_rack(
        "3F", expected_revision=first.floor_revision, operation_key="rack-one-step-second",
        area_feature_id="zone-f1", values=_rack_values(name="二号架", x_mm=8000, level_cell_counts=[3, 3, 3]),
    )
    context = begin_warehouse_twin_one_step_rack_publish(
        "3F", first.value["id"], expected_effective_revision=second.floor_revision,
        expected_published_revision=published_revision,
    )
    validation = validate_warehouse_twin_layout_draft(
        "3F", expected_revision=context.published_floor_revision,
    )
    assert validation.value["blockers"] == []
    applied = publish_warehouse_twin_layout_draft(
        "3F", expected_published_revision=published_revision,
        expected_draft_revision=context.published_floor_revision,
        operation_key="rack-one-step-publish",
    )
    assert applied.applied is True
    assert rebase_warehouse_twin_advanced_rack_after_one_step(
        context, "3F", first.value["id"],
    ) is True
    published_floor = json.loads(published.read_text(encoding="utf8"))["floors"]["3F"]
    assert [rack["name"] for rack in published_floor["racks"]] == ["一号架"]
    active = load_warehouse_twin_layout_draft("3F")
    assert [rack["name"] for rack in active["racks"]] == ["一号架", "二号架"]
    assert active["draft_control"]["status"] == "draft"


def test_default_publish_keeps_static_baseline_read_only_and_versions_runtime_backups(
    tmp_path: Path,
    monkeypatch,
) -> None:
    baseline = _asset(tmp_path / "static-baseline.json")
    runtime = tmp_path / "data" / "layout_runtime" / "twin_layout_v1.json"
    draft = tmp_path / "data" / "layout_drafts" / "twin_layout_v1.draft.json"
    backups = tmp_path / "data" / "layout_backups"
    monkeypatch.setattr(editor, "TWIN_LAYOUT_BASELINE_PATH", baseline)
    monkeypatch.setattr(editor, "TWIN_LAYOUT_PATH", runtime)
    monkeypatch.setattr(editor, "TWIN_LAYOUT_DRAFT_PATH", draft)
    monkeypatch.setattr(editor, "TWIN_LAYOUT_BACKUP_DIR", backups)

    baseline_sha256 = sha256(baseline.read_bytes()).hexdigest()
    baseline_revision = json.loads(baseline.read_text(encoding="utf-8"))["floors"]["3F"]["revision"]
    created = create_warehouse_twin_rack(
        "3F",
        expected_revision=baseline_revision,
        operation_key="runtime-first-create-0001",
        area_feature_id="zone-f1",
        values=_rack_values(level_cell_counts=[1, 2, 3]),
    )
    validate_warehouse_twin_layout_draft(
        "3F",
        expected_revision=created.floor_revision,
    )
    first = publish_warehouse_twin_layout_draft(
        "3F",
        expected_published_revision=baseline_revision,
        expected_draft_revision=created.floor_revision,
        operation_key="runtime-first-publish-0001",
    )
    assert first.value["published_storage"] == "runtime"
    assert runtime.is_file()
    assert sha256(baseline.read_bytes()).hexdigest() == baseline_sha256
    first_runtime_sha256 = sha256(runtime.read_bytes()).hexdigest()
    assert first.value["published_sha256"] == first_runtime_sha256
    first_backup = backups / first.value["backup_name"]
    assert sha256(first_backup.read_bytes()).hexdigest() == baseline_sha256

    updated = update_warehouse_twin_rack(
        "3F",
        created.value["id"],
        expected_revision=created.floor_revision,
        expected_version=1,
        operation_key="runtime-second-update-0001",
        values=_rack_values(name="运行态二次发布", level_cell_counts=[3, 3, 3]),
    )
    validate_warehouse_twin_layout_draft(
        "3F",
        expected_revision=updated.floor_revision,
    )
    second = publish_warehouse_twin_layout_draft(
        "3F",
        expected_published_revision=created.floor_revision,
        expected_draft_revision=updated.floor_revision,
        operation_key="runtime-second-publish-0001",
    )
    second_backup = backups / second.value["backup_name"]
    assert sha256(second_backup.read_bytes()).hexdigest() == first_runtime_sha256
    assert len(list(backups.glob("*.json"))) == 2
    assert sha256(baseline.read_bytes()).hexdigest() == baseline_sha256
    published = json.loads(runtime.read_text(encoding="utf-8"))
    assert published["floors"]["3F"]["racks"][0]["name"] == "运行态二次发布"


def test_damaged_runtime_layout_fails_closed_without_static_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.services import warehouse_twin_layout

    baseline = _asset(tmp_path / "static-baseline.json")
    runtime = tmp_path / "data" / "layout_runtime" / "twin_layout_v1.json"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("{damaged-runtime", encoding="utf-8")
    monkeypatch.setattr(editor, "TWIN_LAYOUT_BASELINE_PATH", baseline)
    monkeypatch.setattr(editor, "TWIN_LAYOUT_PATH", runtime)
    monkeypatch.setattr(warehouse_twin_layout, "TWIN_LAYOUT_PATH", baseline)
    monkeypatch.setattr(warehouse_twin_layout, "TWIN_LAYOUT_RUNTIME_PATH", runtime)

    with pytest.raises(WarehouseTwinLayoutEditError, match="无法读取"):
        load_warehouse_twin_layout_draft("3F")
    with pytest.raises(ValueError, match="运行地图损坏"):
        warehouse_twin_layout.load_warehouse_twin_floor("3F")


def test_uat_editor_missing_runtime_fails_closed_without_static_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = _asset(tmp_path / "static-baseline.json")
    runtime = tmp_path / "uat" / "warehouse" / "runtime" / "twin_layout_v1.json"
    draft = tmp_path / "uat" / "warehouse" / "drafts" / "twin_layout_v1.draft.json"
    backups = tmp_path / "uat" / "warehouse" / "backups"
    baseline_sha256 = sha256(baseline.read_bytes()).hexdigest()
    monkeypatch.setenv("ERP_UAT_ROOT", str(tmp_path / "uat"))
    monkeypatch.setattr(editor, "TWIN_LAYOUT_BASELINE_PATH", baseline)
    monkeypatch.setattr(editor, "TWIN_LAYOUT_PATH", runtime)
    monkeypatch.setattr(editor, "TWIN_LAYOUT_DRAFT_PATH", draft)
    monkeypatch.setattr(editor, "TWIN_LAYOUT_BACKUP_DIR", backups)

    with pytest.raises(WarehouseTwinLayoutEditNotFoundError):
        load_warehouse_twin_layout_draft("3F")

    assert sha256(baseline.read_bytes()).hexdigest() == baseline_sha256
    assert not runtime.exists()
    assert not draft.exists()
    assert not backups.exists()


def test_invalid_or_stale_draft_is_refused_and_never_changes_published(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published = _asset(tmp_path / "published.json")
    draft = tmp_path / "layout.draft.json"
    monkeypatch.setattr(editor, "TWIN_LAYOUT_PATH", published)
    monkeypatch.setattr(editor, "TWIN_LAYOUT_DRAFT_PATH", draft)
    revision = json.loads(published.read_text(encoding="utf-8"))["floors"]["3F"]["revision"]
    created = create_warehouse_twin_rack(
        "3F",
        expected_revision=revision,
        operation_key="draft-create-invalid-0001",
        area_feature_id="zone-f1",
        values=_rack_values(),
    )
    before = sha256(published.read_bytes()).hexdigest()

    document = json.loads(draft.read_text(encoding="utf-8"))
    duplicate = dict(document["floors"]["3F"]["racks"][0])
    duplicate["name"] = "重复编号货架"
    document["floors"]["3F"]["racks"].append(duplicate)
    document["floors"]["3F"]["revision"] = _floor_revision(document["floors"]["3F"])
    draft.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    invalid_revision = document["floors"]["3F"]["revision"]

    invalid = validate_warehouse_twin_layout_draft("3F", expected_revision=invalid_revision)
    assert invalid.value["status"] == "draft"
    assert any("重复货架" in item for item in invalid.value["blockers"])
    with pytest.raises(WarehouseTwinLayoutEditConflictError):
        publish_warehouse_twin_layout_draft(
            "3F",
            expected_published_revision=revision,
            expected_draft_revision=invalid_revision,
            operation_key="publish-invalid-0001",
        )
    assert sha256(published.read_bytes()).hexdigest() == before

    published_document = json.loads(published.read_text(encoding="utf-8"))
    published_document["generated_at"] = "externally-updated"
    published.write_text(json.dumps(published_document, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(WarehouseTwinLayoutEditConflictError):
        load_warehouse_twin_layout_draft("3F")


def test_discard_draft_is_idempotent_and_leaves_published_unchanged(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published = _asset(tmp_path / "published.json")
    draft = tmp_path / "layout.draft.json"
    monkeypatch.setattr(editor, "TWIN_LAYOUT_PATH", published)
    monkeypatch.setattr(editor, "TWIN_LAYOUT_DRAFT_PATH", draft)
    before = sha256(published.read_bytes()).hexdigest()
    revision = json.loads(published.read_text(encoding="utf-8"))["floors"]["3F"]["revision"]
    created = create_warehouse_twin_rack(
        "3F",
        expected_revision=revision,
        operation_key="draft-create-discard-0001",
        area_feature_id="zone-f1",
        values=_rack_values(),
    )

    discarded = discard_warehouse_twin_layout_draft(
        "3F",
        expected_revision=created.floor_revision,
    )
    assert discarded.applied is True
    assert not draft.exists()
    repeated = discard_warehouse_twin_layout_draft(
        "3F",
        expected_revision=created.floor_revision,
    )
    assert repeated.applied is False
    assert sha256(published.read_bytes()).hexdigest() == before


def test_stale_draft_requires_explicit_rebuild_from_current_published_map(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published = _asset(tmp_path / "published.json")
    draft = tmp_path / "layout.draft.json"
    monkeypatch.setattr(editor, "TWIN_LAYOUT_PATH", published)
    monkeypatch.setattr(editor, "TWIN_LAYOUT_DRAFT_PATH", draft)
    initial_revision = json.loads(published.read_text(encoding="utf-8"))["floors"]["3F"]["revision"]
    create_warehouse_twin_rack(
        "3F",
        expected_revision=initial_revision,
        operation_key="stale-draft-create-0001",
        area_feature_id="zone-f1",
        values=_rack_values(),
    )
    stale_sha = sha256(draft.read_bytes()).hexdigest()

    published_document = json.loads(published.read_text(encoding="utf-8"))
    published_document["floors"]["3F"]["features"][0]["name"] = "当前正式区域"
    published_document["floors"]["3F"]["revision"] = _floor_revision(
        published_document["floors"]["3F"]
    )
    current_revision = published_document["floors"]["3F"]["revision"]
    published.write_text(
        json.dumps(published_document, ensure_ascii=False), encoding="utf-8"
    )
    published_sha = sha256(published.read_bytes()).hexdigest()

    with pytest.raises(WarehouseTwinLayoutEditConflictError):
        load_warehouse_twin_layout_draft("3F")
    with pytest.raises(WarehouseTwinLayoutEditConflictError):
        rebuild_stale_warehouse_twin_layout_draft(
            "3F", expected_published_revision=initial_revision
        )

    rebuilt = rebuild_stale_warehouse_twin_layout_draft(
        "3F", expected_published_revision=current_revision
    )
    assert rebuilt.applied is True
    assert rebuilt.value["stale_draft_sha256"] == stale_sha
    assert sha256(published.read_bytes()).hexdigest() == published_sha
    fresh = load_warehouse_twin_layout_draft("3F")
    assert fresh["features"][0]["name"] == "当前正式区域"
    assert fresh["draft_control"]["published_revision"] == current_revision
    repeated = rebuild_stale_warehouse_twin_layout_draft(
        "3F", expected_published_revision=current_revision
    )
    assert repeated.applied is False
    assert repeated.value["status"] == "current"


def test_refresh_export_preserves_operator_racks_and_zone_policy(tmp_path: Path) -> None:
    existing = tmp_path / "twin.json"
    existing.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "floors": {
                    "3F": {
                        "layout_id": "same",
                        "layout_edited_at": "2026-08-06T00:00:00+00:00",
                        "racks": [{"id": "operator-rack", "rack_code": "RACK-3F-F1-EDIT-001"}],
                        "retired_racks": [{"id": "old-rack"}],
                        "layout_edit_receipts": [{"operation_key": "saved"}],
                        "features": [{"id": "zone", "allowed_inventory_types": ["finished"], "storage_layout": "mixed"}],
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    refreshed = {
        "schema_version": 1,
        "floors": {
            "3F": {
                "layout_id": "same",
                "racks": [{"id": "database-rack"}],
                "features": [{"id": "zone", "name": "CAD刷新后的区域"}],
                "revision": "before",
            }
        },
    }
    result = preserve_operator_layout_edits(refreshed, existing)
    floor = result["floors"]["3F"]
    assert floor["racks"] == [{"id": "operator-rack", "rack_code": "RACK-3F-F1-EDIT-001"}]
    assert floor["features"][0]["name"] == "CAD刷新后的区域"
    assert floor["features"][0]["storage_layout"] == "mixed"
    assert floor["features"][0]["allowed_inventory_types"] == ["finished"]


def test_refresh_export_keeps_operator_owned_floor4_when_editor_db_has_only_1f_3f(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "twin.json"
    floor4 = {
        "floor_code": "4F",
        "layout_id": "floor4-scan-plan",
        "revision": "floor4-planning-revision",
        "calibration": {"status": "pending_site_calibration"},
        "features": [],
    }
    existing.write_text(
        json.dumps(
            {"schema_version": 1, "floors": {"4F": floor4}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    refreshed = {
        "schema_version": 1,
        "floors": {
            "1F": {"layout_id": "one", "features": [], "revision": "one"},
            "3F": {"layout_id": "three", "features": [], "revision": "three"},
        },
    }

    result = preserve_operator_layout_edits(refreshed, existing)

    assert result["floors"]["4F"] == floor4


def test_checked_in_f1_uses_two_combined_independently_editable_racks() -> None:
    payload = json.loads((ROOT / "static" / "factory_maps" / "twin_layout_v1.json").read_text(encoding="utf-8"))
    three = payload["floors"]["3F"]
    racks = [item for item in three["racks"] if item["rack_code"].startswith("RACK-3F-F1-")]
    assert len(racks) == 2
    assert {item["width_mm"] for item in racks} == {5600.0}
    assert {item["depth_mm"] for item in racks} == {1100.0}
    assert {item["rotation_deg"] for item in racks} == {90}
    assert all(item["area_code"] == "F1" for item in racks)
