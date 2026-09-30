from pathlib import Path
import shutil
import subprocess

import pytest


def test_drawing_workbench_node_behaviors():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node runtime unavailable")
    result = subprocess.run(
        [node, "tests/js/test_drawing_workbench_behavior.cjs"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_workbench_assets_are_integrated_once():
    page = Path("static/index.html").read_text(encoding="utf-8")
    assert page.count('/static/drawing-workbench.js') == 1
    assert page.count('/static/drawing-workbench.css') == 1
    assert page.count('openDrawingWorkbench') == 2
    assert Path("static/drawing-workbench.js").stat().st_size > 5000
    assert Path("static/drawing-workbench.css").stat().st_size > 1000
