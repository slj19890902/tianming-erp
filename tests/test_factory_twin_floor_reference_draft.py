from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "factory_twin" / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
CANVAS = (ROOT / "factory_twin" / "frontend" / "src" / "EditorCanvas.tsx").read_text(encoding="utf-8")


def test_floor1_can_show_adjustable_floor3_left_half_reference() -> None:
    assert "三楼左半区柱墙参照" in APP
    assert 'sourceFloor: "3F-left-half"' in APP
    assert "offset_x_mm" in APP and "offset_y_mm" in APP
    assert "scale_x" in APP and "scale_y" in APP
    assert "mirror_x" in APP and "mirror_y" in APP
    assert "rotation_deg" in APP
    assert "输入 5000 就移动 5000mm" in APP
    assert "↑ 1000" in APP
    assert 'referenceGroup.name = "3F-left-half-reference-overlay"' in CANVAS
    assert 'layout.floor_code.toUpperCase() === "1F"' in CANVAS


def test_floor_reference_save_is_local_draft_without_layout_write() -> None:
    block = APP.split("const saveReferenceOverlayDraft = () => {", 1)[1].split(
        "const exportReferenceOverlayDraft", 1
    )[0]
    assert "localStorage.setItem" in block
    assert "api.update" not in block
    assert "api.create" not in block
    assert 'geometryChanged: false' in block
    assert 'schemaVersion: 3' in block


def test_aligned_floor_uses_shared_measured_coordinates_and_discards_legacy_overlay() -> None:
    assert 'FLOOR3_COORDINATE_FRAME_MARKER = "COORDINATE_FRAME:3F_MEASURED_V1"' in APP
    assert '"tm-floor-reference-overlay-shared-v1"' in APP
    assert '"shared_3f_measured_coordinates"' in APP
    assert "createDefaultReferenceOverlay(sharesFloor3Coordinates)" in APP
