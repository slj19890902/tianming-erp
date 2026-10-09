"""Execute the actual mobile page and recovery module against synthetic responses."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_mobile_stocktake_recovery_real_page_handlers():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the isolated frontend regression")
    env = os.environ.copy()
    env.pop("TM_STOCKTAKE_BASELINE", None)
    result = subprocess.run(
        [node, str(ROOT / "tests/mobile_stocktake_recovery.cjs")],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "stocktake recovery scenarios passed" in result.stdout
