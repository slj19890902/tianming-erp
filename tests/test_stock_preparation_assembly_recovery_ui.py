import json
from pathlib import Path
import shutil
import subprocess


def test_actual_group_assembly_recovery_page():
    root = Path(__file__).resolve().parents[1]
    node = shutil.which("node")
    assert node, "Node is required for actual assembly HTML/mixin recovery regression"
    result = subprocess.run(
        [node, str(root / "tests/stock_preparation_assembly_recovery.cjs")],
        cwd=root, text=True, encoding="utf-8", capture_output=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["total"] >= 54
    assert evidence["passed"] == evidence["total"]
