"""Run the actual mobile-page navigation and recovery boundary probes."""
from pathlib import Path
import shutil
import subprocess


def test_mobile_field_navigation_contract():
    root = Path(__file__).resolve().parents[1]
    node = shutil.which("node")
    assert node, "Node.js is required for the mobile field-navigation regression"
    result = subprocess.run(
        [node, "--test", str(root / "tests/mobile_field_navigation.cjs")],
        cwd=root, text=True, encoding="utf-8", capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
