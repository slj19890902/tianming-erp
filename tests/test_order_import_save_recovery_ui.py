"""Exercise the actual order-import Vue methods and recovery resource via Node."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


def test_order_import_save_recovery_actual_ui(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for actual order-import UI regression")
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "ORDER_IMPORT_UI_EVIDENCE": str(tmp_path)}
    run = subprocess.run(
        [node, str(root / "tests/order_import_save_recovery.cjs")],
        cwd=root, env=env, capture_output=True, text=True, timeout=30,
        encoding="utf-8", errors="replace",
    )
    assert run.returncode == 0, run.stdout + run.stderr
