"""Behavioral mobile-shell, navigation and request-race regressions (no business writes)."""
from pathlib import Path
import shutil
import subprocess


def test_shared_mobile_shell_behaviors():
    root = Path(__file__).resolve().parents[1]
    node = shutil.which("node")
    assert node, "Node.js is required; install tests/ui dependencies for linkedom"
    result = subprocess.run(
        [node, "--test", "tests/mobile3_shell.cjs"], cwd=root,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
