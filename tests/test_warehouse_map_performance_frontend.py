from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EDITOR_CANVAS = (ROOT / "factory_twin" / "frontend" / "src" / "EditorCanvas.tsx").read_text(encoding="utf-8")


def test_active_threejs_canvas_coalesces_pointer_work_to_animation_frames() -> None:
    pointer_block = EDITOR_CANVAS.split("const processPointerMove", 1)[1].split(
        "const onPointerUp", 1
    )[0]
    assert "const onPointerMove" in pointer_block
    assert "pointerMoveFrame = requestAnimationFrame" in pointer_block
    assert "pendingPointerMove" in pointer_block
    assert "const flushPointerMove" in pointer_block
