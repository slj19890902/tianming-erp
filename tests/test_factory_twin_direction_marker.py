from pathlib import Path


EDITOR_CANVAS = (
    Path(__file__).resolve().parents[1]
    / "factory_twin"
    / "frontend"
    / "src"
    / "EditorCanvas.tsx"
).read_text(encoding="utf-8")


def test_aligned_floors_relabel_drawing_north_as_real_east_without_rotating_geometry():
    assert '["1F", "3F"].includes(floorCode)' in EDITOR_CANVAS
    assert 'floorCode === "4F"' in EDITOR_CANVAS
    assert 'calibration?.status === "aligned"' in EDITOR_CANVAS
    assert 'calibration?.applied === true' in EDITOR_CANVAS
    assert 'floor4CalibratingCompass ? "3F" : realEastCompass ? "E" : "N"' in EDITOR_CANVAS
    assert 'floor4CalibratingCompass ? "对齐3F" : realEastCompass ? "现实东向" : "图纸北向"' in EDITOR_CANVAS
    assert "floor4SharedCompass" not in EDITOR_CANVAS
    assert "const bounds = layout.bounds_mm" in EDITOR_CANVAS
    assert "new THREE.Vector3(xMm - centerX, elevation, -(yMm - centerY))" in EDITOR_CANVAS
    assert "drawingToRealPoint" not in EDITOR_CANVAS
