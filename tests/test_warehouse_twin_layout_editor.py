from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from app.services import warehouse_twin_layout_editor as editor
from app.services.warehouse_twin_layout_editor import (
    WarehouseTwinLayoutEditConflictError,
    WarehouseTwinLayoutEditError,
    _floor_revision,
    create_warehouse_twin_rack,
    delete_warehouse_twin_rack,
    discard_warehouse_twin_layout_draft,
    load_warehouse_twin_layout_draft,
    publish_warehouse_twin_layout_draft,
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


def test_checked_in_f1_uses_two_combined_independently_editable_racks() -> None:
    payload = json.loads((ROOT / "static" / "factory_maps" / "twin_layout_v1.json").read_text(encoding="utf-8"))
    three = payload["floors"]["3F"]
    racks = [item for item in three["racks"] if item["rack_code"].startswith("RACK-3F-F1-")]
    assert len(racks) == 2
    assert {item["width_mm"] for item in racks} == {5600.0}
    assert {item["depth_mm"] for item in racks} == {1100.0}
    assert {item["rotation_deg"] for item in racks} == {90}
    assert all(item["area_code"] == "F1" for item in racks)
