from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app.api import warehouse as warehouse_api
from app.services import warehouse_twin_layout_editor as editor


ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE_FRONTEND = (
    ROOT / "factory_twin" / "frontend" / "src" / "WarehouseTwinApp.tsx"
).read_text(encoding="utf-8")
WAREHOUSE_API = (ROOT / "app" / "api" / "warehouse.py").read_text(encoding="utf-8")
CANONICAL_TARGET_POINTS = [[0.0, 1500.0], [2000.0, -1500.0], [2000.0, 1500.0]]
ROTATED_SOURCE_POINTS = [[1500.0, 0.0], [-1500.0, -2000.0], [1500.0, -2000.0]]


def _feature_3f() -> dict:
    return {
        "id": "lift-authority-3f",
        "layout_id": "layout-3f",
        "feature_code": "LIFT-002",
        "name": "货梯（跨楼层定位）",
        "feature_kind": "structure",
        "subtype": "freight_elevator",
        "points": [[0.0, 0.0], [2000.0, 0.0]],
        "width_mm": 3000.0,
        "direction": None,
        "no_stacking": False,
        "storage_mode": "floor",
        "elevation_mm": 0,
        "storage_height_mm": 3500,
        "color": "#475569",
        "area_mm2": 6_000_000,
        "source": "manual",
        "status": "confirmed",
        "is_locked": True,
        "version": 17,
        "erp_area_code": None,
    }


def _floor(code: str, *, features: list[dict] | None = None) -> dict:
    floor = {
        "layout_id": f"layout-{code.lower()}",
        "floor_code": code,
        "name": code,
        "bounds_mm": {"min_x": 0.0, "min_y": 0.0, "max_x": 1000.0, "max_y": 1000.0},
        "structures": [],
        "features": features or [],
        "placements": [],
        "racks": [],
        "pallets": [],
        "assets": [],
    }
    floor["revision"] = editor._floor_revision(floor)
    return floor


def _document() -> dict:
    floor4 = _floor("4F")
    floor4.update(
        {
            "operational_status": "planning_only",
            "metadata": {
                "calibration": {
                    "canonical_target_points": deepcopy(CANONICAL_TARGET_POINTS)
                }
            },
            "calibration": {
                "status": "requires_site_points",
                "target_points": deepcopy(CANONICAL_TARGET_POINTS),
                "applied": False,
            },
            "structures": [
                {"id": "wall", "geometry": {"points": [[100, 200], [300, 200]]}}
            ],
            "placements": [{"id": "equipment", "x_mm": 100, "y_mm": 200, "rotation_deg": 0}],
            "assets": [{"id": "asset", "x_mm": 500, "y_mm": 500, "rotation_deg": 270}],
        }
    )
    floor4["revision"] = editor._floor_revision(floor4)
    return {
        "schema_version": 1,
        "generated_at": "test",
        "floors": {
            "1F": _floor("1F"),
            "3F": _floor("3F", features=[_feature_3f()]),
            "4F": floor4,
        },
    }


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_floor4_three_point_calibration_rotates_all_geometry_and_preserves_1f_3f(
    tmp_path: Path,
) -> None:
    published = tmp_path / "published.json"
    draft = tmp_path / "draft.json"
    document = _document()
    protected = deepcopy({code: document["floors"][code] for code in ("1F", "3F")})
    _write(published, document)

    result = editor.calibrate_floor4_freight_elevator(
        "4f",
        expected_revision=document["floors"]["4F"]["revision"],
        operation_key="floor4-calibration-001",
        source_points=ROTATED_SOURCE_POINTS,
        published_path=published,
        draft_path=draft,
    )

    assert result.applied is True
    saved = json.loads(draft.read_text(encoding="utf-8"))
    assert {code: saved["floors"][code] for code in ("1F", "3F")} == protected
    floor4 = saved["floors"]["4F"]
    assert floor4["structures"][0]["geometry"]["points"][0] == [-200.0, 100.0]
    assert floor4["placements"][0]["x_mm"] == -200.0
    assert floor4["placements"][0]["y_mm"] == 100.0
    assert floor4["placements"][0]["rotation_deg"] == 90.0
    assert floor4["bounds_mm"] == {
        "min_x": -1000.0,
        "min_y": 0.0,
        "max_x": 0.0,
        "max_y": 1000.0,
    }
    calibration = floor4["metadata"]["calibration"]
    assert calibration["status"] == "aligned"
    assert calibration["applied"] is True
    assert calibration["scale"] == 1.0
    assert calibration["mirror"] is False
    assert calibration["max_residual_mm"] == 0.0
    lift = next(item for item in floor4["features"] if item["feature_code"] == "LIFT-002")
    authority = protected["3F"]["features"][0]
    assert lift["points"] == authority["points"]
    assert lift["width_mm"] == authority["width_mm"]
    assert lift["is_locked"] is True

    repeated = editor.calibrate_floor4_freight_elevator(
        "4F",
        expected_revision=document["floors"]["4F"]["revision"],
        operation_key="floor4-calibration-001",
        source_points=ROTATED_SOURCE_POINTS,
        published_path=published,
        draft_path=draft,
    )
    assert repeated.applied is False
    assert repeated.floor_revision == result.floor_revision


def test_floor4_recalibration_applies_incremental_rigid_correction_once(
    tmp_path: Path,
) -> None:
    published = tmp_path / "published.json"
    draft = tmp_path / "draft.json"
    document = _document()
    floor4 = document["floors"]["4F"]
    floor4["features"].append(
        {
            "id": "stable-zone-4f-a1",
            "feature_code": "ZONE-4F-A1",
            "feature_kind": "zone",
            "points": [[100.0, 200.0], [300.0, 200.0], [300.0, 400.0]],
            "rotation_deg": 5.0,
            "stable_location_id": 401,
        }
    )
    floor4["revision"] = editor._floor_revision(floor4)
    protected = deepcopy({code: document["floors"][code] for code in ("1F", "3F")})
    _write(published, document)

    initial = editor.calibrate_floor4_freight_elevator(
        "4F",
        expected_revision=floor4["revision"],
        operation_key="floor4-initial-alignment-001",
        source_points=CANONICAL_TARGET_POINTS,
        published_path=published,
        draft_path=draft,
    )
    stale_text_document = json.loads(draft.read_text(encoding="utf-8"))
    stale_text_floor4 = stale_text_document["floors"]["4F"]
    stale_text_floor4["name"] = "四楼扫描规划图（待现场三点标定）"
    stale_text_floor4["warnings"] = [
        "有 4 个实体无法可靠分类，已作为灰色参考线保留。",
        "四楼当前仅为扫描规划资产；完成现场三点标定前不得作为正式位置或库存地图。",
        "3F/LIFT-002 仅作为坐标权威引用；本资产未生成四楼货梯几何。",
        "本资产未生成区域、货架、栈板、库存或正式库位。",
    ]
    stale_text_floor4["revision"] = editor._floor_revision(stale_text_floor4)
    _write(draft, stale_text_document)
    current_applied_revision = stale_text_floor4["revision"]
    initial_bytes = draft.read_bytes()

    with pytest.raises(
        editor.WarehouseTwinLayoutEditConflictError,
        match="布局已被其他操作更新",
    ):
        editor.calibrate_floor4_freight_elevator(
            "4F",
            expected_revision=floor4["revision"],
            operation_key="floor4-stale-recalibration-001",
            source_points=ROTATED_SOURCE_POINTS,
            published_path=published,
            draft_path=draft,
        )
    assert draft.read_bytes() == initial_bytes

    recalibrated = editor.calibrate_floor4_freight_elevator(
        "4F",
        expected_revision=current_applied_revision,
        operation_key="floor4-recalibration-001",
        source_points=ROTATED_SOURCE_POINTS,
        published_path=published,
        draft_path=draft,
    )

    assert recalibrated.applied is True
    assert recalibrated.value["recalibrated"] is True
    assert recalibrated.value["inventory_changed"] is False
    saved = json.loads(draft.read_text(encoding="utf-8"))
    assert {code: saved["floors"][code] for code in ("1F", "3F")} == protected
    saved_floor4 = saved["floors"]["4F"]
    zone = next(
        item for item in saved_floor4["features"] if item["id"] == "stable-zone-4f-a1"
    )
    assert zone["points"][0] == [-200.0, 100.0]
    assert zone["rotation_deg"] == 95.0
    assert zone["stable_location_id"] == 401
    assert saved_floor4["placements"][0]["id"] == "equipment"
    assert saved_floor4["placements"][0]["x_mm"] == -200.0
    assert saved_floor4["placements"][0]["y_mm"] == 100.0
    assert saved_floor4["name"] == "四楼扫描规划图"
    assert saved_floor4["warnings"] == [
        "有 4 个实体无法可靠分类，已作为灰色参考线保留。"
    ]
    lifts = [
        item
        for item in saved_floor4["features"]
        if item.get("feature_code") == "LIFT-002"
    ]
    assert len(lifts) == 1
    assert lifts[0]["id"] == "LIFT-002-4F-CANONICAL"
    assert lifts[0]["points"] == protected["3F"]["features"][0]["points"]
    assert lifts[0]["version"] == 2
    calibration = saved_floor4["metadata"]["calibration"]
    assert calibration["source_coordinate_basis"] == "current_floor4_geometry"
    assert calibration["audit"]["event"] == "recalibration"
    assert calibration["audit"]["previous_operation_key"] == (
        "floor4-initial-alignment-001"
    )
    history = saved_floor4["metadata"]["recalibration_history"]
    assert history[-1]["operation_key"] == "floor4-recalibration-001"
    assert history[-1]["previous_calibration"]["audit"]["operation_key"] == (
        "floor4-initial-alignment-001"
    )

    saved_bytes = draft.read_bytes()
    replayed = editor.calibrate_floor4_freight_elevator(
        "4F",
        expected_revision=initial.floor_revision,
        operation_key="floor4-recalibration-001",
        source_points=ROTATED_SOURCE_POINTS,
        published_path=published,
        draft_path=draft,
    )
    assert replayed.applied is False
    assert replayed.floor_revision == recalibrated.floor_revision
    assert replayed.value == recalibrated.value
    assert draft.read_bytes() == saved_bytes
    with pytest.raises(
        editor.WarehouseTwinLayoutEditConflictError,
        match="该操作键已用于不同的三点标定请求",
    ):
        editor.calibrate_floor4_freight_elevator(
            "4F",
            expected_revision=recalibrated.floor_revision,
            operation_key="floor4-recalibration-001",
            source_points=CANONICAL_TARGET_POINTS,
            published_path=published,
            draft_path=draft,
        )
    assert draft.read_bytes() == saved_bytes


def test_floor4_empty_warehouse_warning_is_kept_until_spatial_facts_exist() -> None:
    warning = "本资产未生成区域、货架、栈板、库存或正式库位。"
    empty_floor = {
        "name": "四楼扫描规划图",
        "features": [],
        "racks": [],
        "pallets": [],
        "erp_area_codes": [],
        "warnings": [warning],
    }

    empty_result = editor._clear_floor4_stale_calibration_text(empty_floor)

    assert empty_floor["warnings"] == [warning]
    assert empty_result["removed_warnings"] == []

    floor_with_zone = deepcopy(empty_floor)
    floor_with_zone["features"] = [
        {
            "id": "stable-zone-4f-a1",
            "feature_kind": "zone",
            "feature_code": "ZONE-4F-A1",
        }
    ]
    spatial_result = editor._clear_floor4_stale_calibration_text(floor_with_zone)

    assert floor_with_zone["warnings"] == []
    assert spatial_result["removed_warnings"] == [warning]


@pytest.mark.parametrize(
    ("source_points", "message"),
    [
        ([[0, 0], [1000, 0], [0, -1000]], "形成镜像"),
        ([[0, 0], [2000, 0], [0, 2000]], "误差过大"),
    ],
)
def test_floor4_recalibration_rejects_unsafe_transform_without_writing(
    tmp_path: Path,
    source_points: list[list[int]],
    message: str,
) -> None:
    published = tmp_path / "published.json"
    draft = tmp_path / "draft.json"
    document = _document()
    _write(published, document)
    initial = editor.calibrate_floor4_freight_elevator(
        "4F",
        expected_revision=document["floors"]["4F"]["revision"],
        operation_key="floor4-safe-initial-alignment",
        source_points=CANONICAL_TARGET_POINTS,
        published_path=published,
        draft_path=draft,
    )
    before = draft.read_bytes()

    with pytest.raises(editor.WarehouseTwinLayoutEditError, match=message):
        editor.calibrate_floor4_freight_elevator(
            "4F",
            expected_revision=initial.floor_revision,
            operation_key=f"floor4-unsafe-recalibration-{message}",
            source_points=source_points,
            published_path=published,
            draft_path=draft,
        )

    assert draft.read_bytes() == before


def test_floor4_recalibration_requires_exactly_one_materialized_lift(
    tmp_path: Path,
) -> None:
    published = tmp_path / "published.json"
    draft = tmp_path / "draft.json"
    document = _document()
    _write(published, document)
    initial = editor.calibrate_floor4_freight_elevator(
        "4F",
        expected_revision=document["floors"]["4F"]["revision"],
        operation_key="floor4-unique-lift-initial",
        source_points=CANONICAL_TARGET_POINTS,
        published_path=published,
        draft_path=draft,
    )
    saved = json.loads(draft.read_text(encoding="utf-8"))
    floor4 = saved["floors"]["4F"]
    lift = next(
        item for item in floor4["features"] if item.get("feature_code") == "LIFT-002"
    )
    duplicate = deepcopy(lift)
    duplicate["id"] = "duplicate-lift-002"
    floor4["features"].append(duplicate)
    floor4["revision"] = editor._floor_revision(floor4)
    _write(draft, saved)
    before = draft.read_bytes()

    with pytest.raises(
        editor.WarehouseTwinLayoutEditConflictError,
        match="必须且只能存在一个物化货梯 LIFT-002",
    ):
        editor.calibrate_floor4_freight_elevator(
            "4F",
            expected_revision=floor4["revision"],
            operation_key="floor4-duplicate-lift-recalibration",
            source_points=ROTATED_SOURCE_POINTS,
            published_path=published,
            draft_path=draft,
        )

    assert draft.read_bytes() == before
    assert initial.applied is True


def test_floor4_recalibration_api_audits_only_the_applied_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSession:
        commits = 0
        rollbacks = 0

        def commit(self) -> None:
            self.commits += 1

        def rollback(self) -> None:
            self.rollbacks += 1

    mutation = editor.LayoutMutation(
        value={
            "recalibrated": True,
            "inventory_changed": False,
            "calibration": {},
            "freight_elevator": {},
        },
        floor_revision="recalibrated-revision",
        applied=True,
    )
    replay = editor.LayoutMutation(
        value=deepcopy(mutation.value),
        floor_revision=mutation.floor_revision,
        applied=False,
    )
    responses = iter((mutation, replay))
    audit_entries: list[dict] = []
    monkeypatch.setattr(
        warehouse_api,
        "calibrate_floor4_freight_elevator",
        lambda *_args, **_kwargs: next(responses),
    )
    monkeypatch.setattr(
        warehouse_api,
        "snapshot_warehouse_twin_layout_draft",
        lambda: object(),
    )
    monkeypatch.setattr(
        warehouse_api,
        "_twin_layout_asset_log",
        lambda *_args, **kwargs: audit_entries.append(kwargs),
    )
    payload = warehouse_api.TwinFloor4FreightElevatorCalibrationPayload(
        expected_revision="current-floor4-revision",
        operation_key="floor4-api-recalibration-001",
        source_points=ROTATED_SOURCE_POINTS,
        confirmed=True,
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/warehouse/twin-layout/floors/4F/draft/calibrate-freight-elevator",
            "headers": [],
            "client": ("testclient", 50000),
        }
    )
    db = FakeSession()
    user = SimpleNamespace(id=1)

    applied = warehouse_api.calibrate_twin_floor4_freight_elevator(
        "4F", payload, request, db, user
    )
    replayed = warehouse_api.calibrate_twin_floor4_freight_elevator(
        "4F", payload, request, db, user
    )

    assert applied["applied"] is True
    assert replayed["applied"] is False
    assert db.commits == 2
    assert db.rollbacks == 0
    assert len(audit_entries) == 1
    assert audit_entries[0]["action"] == "TWIN_LAYOUT_FLOOR4_RECALIBRATE"
    assert audit_entries[0]["description"] == "管理员重新完成四楼三点货梯标定"
    assert audit_entries[0]["details"]["recalibrated"] is True
    assert audit_entries[0]["details"]["inventory_changed"] is False


@pytest.mark.parametrize(
    ("source_points", "message"),
    [
        ([[0, 0], [100, 0], [200, 0]], "不能共线"),
        ([[0, 0], [1000, 0], [0, -1000]], "形成镜像"),
        ([[0, 0], [2000, 0], [0, 2000]], "误差过大"),
    ],
)
def test_floor4_calibration_rejects_unsafe_point_sets(
    tmp_path: Path, source_points: list[list[int]], message: str
) -> None:
    published = tmp_path / "published.json"
    document = _document()
    _write(published, document)

    with pytest.raises(editor.WarehouseTwinLayoutEditError, match=message):
        editor.calibrate_floor4_freight_elevator(
            "4F",
            expected_revision=document["floors"]["4F"]["revision"],
            operation_key=f"unsafe-{message}-001",
            source_points=source_points,
            published_path=published,
            draft_path=tmp_path / "draft.json",
        )


def test_real_floor4_asset_accepts_point_mm_calibration_targets_without_touching_source(
    tmp_path: Path,
) -> None:
    source_asset = ROOT / "static" / "factory_maps" / "twin_layout_v1.json"
    source_bytes = source_asset.read_bytes()
    document = json.loads(source_bytes.decode("utf-8"))
    floor4 = document["floors"]["4F"]
    target_records = floor4["calibration"]["target_points"]
    assert [record["point_code"][-1] for record in target_records] == ["A", "C", "B"]
    published = tmp_path / "published-real-asset.json"
    draft = tmp_path / "draft-real-asset.json"
    _write(published, document)

    result = editor.calibrate_floor4_freight_elevator(
        "4F",
        expected_revision=floor4["revision"],
        operation_key="real-floor4-point-mm-calibration",
        source_points=[record["point_mm"] for record in target_records],
        published_path=published,
        draft_path=draft,
    )

    assert result.applied is True
    saved = json.loads(draft.read_text(encoding="utf-8"))
    assert saved["floors"]["4F"]["metadata"]["calibration"][
        "canonical_target_points"
    ] == [record["point_mm"] for record in target_records]
    assert source_asset.read_bytes() == source_bytes


def test_floor4_publish_gate_requires_alignment_evidence_and_canonical_lift(
    tmp_path: Path,
) -> None:
    published = tmp_path / "published.json"
    draft = tmp_path / "draft.json"
    document = _document()
    _write(published, document)

    blockers, _ = editor._validate_document_for_publish(
        document, publish_floor_code="4F"
    )
    assert "4F 尚未完成三点货梯标定" in blockers
    unrelated_blockers, _ = editor._validate_document_for_publish(
        document, publish_floor_code="1F"
    )
    assert not any("4F" in blocker and "标定" in blocker for blocker in unrelated_blockers)

    result = editor.calibrate_floor4_freight_elevator(
        "4F",
        expected_revision=document["floors"]["4F"]["revision"],
        operation_key="floor4-calibration-validate",
        source_points=CANONICAL_TARGET_POINTS,
        published_path=published,
        draft_path=draft,
    )
    saved = json.loads(draft.read_text(encoding="utf-8"))
    blockers, _ = editor._validate_document_for_publish(
        saved, publish_floor_code="4F"
    )
    assert blockers == []
    validated = editor.validate_warehouse_twin_layout_draft(
        "4F",
        expected_revision=result.floor_revision,
        published_path=published,
        draft_path=draft,
    )
    assert validated.value["status"] == "validated"
    assert validated.value["blockers"] == []

    saved["floors"]["4F"]["metadata"]["calibration"]["audit"].pop("applied_at")
    saved["floors"]["4F"]["revision"] = editor._floor_revision(saved["floors"]["4F"])
    blockers, _ = editor._validate_document_for_publish(
        saved, publish_floor_code="4F"
    )
    assert "4F 标定审计证据不完整" in blockers
    assert result.floor_revision


@pytest.mark.parametrize(
    ("path", "value", "expected_blocker"),
    [
        (
            ("canonical_target_points",),
            [[1.0, 1500.0], [2000.0, -1500.0], [2000.0, 1500.0]],
            "4F 标定目标点与当前 3F 权威货梯几何不一致",
        ),
        (
            ("canonical_target_points",),
            None,
            "4F 标定目标点与当前 3F 权威货梯几何不一致",
        ),
        (("residuals_mm",), None, "4F 标定逐点残差证据与三点拟合不一致"),
        (
            ("residuals_mm",),
            [1.0, 0.0, 0.0],
            "4F 标定逐点残差证据与三点拟合不一致",
        ),
        (
            ("max_residual_mm",),
            None,
            "4F 标定最大残差证据与三点拟合不一致",
        ),
        (
            ("max_residual_mm",),
            1.0,
            "4F 标定最大残差证据与三点拟合不一致",
        ),
        (
            ("rmse_residual_mm",),
            None,
            "4F 标定均方根残差证据与三点拟合不一致",
        ),
        (
            ("rmse_residual_mm",),
            1.0,
            "4F 标定均方根残差证据与三点拟合不一致",
        ),
        (
            ("thresholds_mm",),
            None,
            "4F 标定发布阈值证据与系统固定阈值不一致",
        ),
        (
            ("thresholds_mm", "max_residual"),
            999.0,
            "4F 标定发布阈值证据与系统固定阈值不一致",
        ),
        (
            ("audit", "source_floor_code"),
            "3F",
            "4F 标定审计证据与当前 3F 权威对象不一致",
        ),
        (
            ("audit", "authority_floor_code"),
            "4F",
            "4F 标定审计证据与当前 3F 权威对象不一致",
        ),
        (
            ("audit", "authority_feature_code"),
            "WRONG",
            "4F 标定审计证据与当前 3F 权威对象不一致",
        ),
        (
            ("audit", "authority_feature_id"),
            None,
            "4F 标定审计证据与当前 3F 权威对象不一致",
        ),
        (
            ("audit", "authority_feature_id"),
            "wrong-id",
            "4F 标定审计证据与当前 3F 权威对象不一致",
        ),
        (
            ("audit", "authority_floor_revision"),
            "stale-revision",
            "4F 标定审计证据与当前 3F 权威对象不一致",
        ),
    ],
)
def test_floor4_publish_gate_rejects_deleted_or_tampered_calibration_evidence(
    tmp_path: Path,
    path: tuple[str, ...],
    value: object,
    expected_blocker: str,
) -> None:
    published = tmp_path / "published.json"
    draft = tmp_path / "draft.json"
    document = _document()
    _write(published, document)
    editor.calibrate_floor4_freight_elevator(
        "4F",
        expected_revision=document["floors"]["4F"]["revision"],
        operation_key="floor4-calibration-tamper-check",
        source_points=CANONICAL_TARGET_POINTS,
        published_path=published,
        draft_path=draft,
    )
    saved = json.loads(draft.read_text(encoding="utf-8"))
    calibration = saved["floors"]["4F"]["metadata"]["calibration"]
    target = calibration
    for key in path[:-1]:
        target = target[key]
    if value is None:
        target.pop(path[-1])
    else:
        target[path[-1]] = value
    saved["floors"]["4F"]["revision"] = editor._floor_revision(
        saved["floors"]["4F"]
    )

    blockers, _ = editor._validate_document_for_publish(
        saved, publish_floor_code="4F"
    )

    assert expected_blocker in blockers


def test_floor4_calibration_api_and_compact_map_tool_are_wired() -> None:
    assert 'draft/calibrate-freight-elevator' in WAREHOUSE_API
    assert "calibrate_floor4_freight_elevator(" in WAREHOUSE_API
    assert 'calibrationMode={floor4CalibrationMode}' in WAREHOUSE_FRONTEND
    assert '"标定货梯/朝向"' in WAREHOUSE_FRONTEND
    assert '"重新标定货梯/朝向"' in WAREHOUSE_FRONTEND
    assert "货梯标定 ${next.length}/3" in WAREHOUSE_FRONTEND
    assert 'drawPointLabels={floor4CalibrationMode ? ["A", "C", "B"] : []}' in WAREHOUSE_FRONTEND
    assert "A · 货梯第一角" in WAREHOUSE_FRONTEND
    assert "C · 货梯对角" in WAREHOUSE_FRONTEND
    assert "B · 货梯邻角" in WAREHOUSE_FRONTEND
    editor_canvas = (
        ROOT / "factory_twin" / "frontend" / "src" / "EditorCanvas.tsx"
    ).read_text(encoding="utf-8")
    assert 'if (effectiveDrawMode === "structure") controls.enablePan = false' in editor_canvas
    assert "扫描规划 / 待现场标定，尚未启用正式作业" in WAREHOUSE_FRONTEND
