from pathlib import Path
import shutil
import subprocess


def test_order_import_balance_and_refresh_contract():
    root = Path(__file__).resolve().parents[1]
    node = shutil.which("node")
    assert node, "Node.js is required for the import balance contract"
    result = subprocess.run(
        [node, str(root / "tests/order_import_balance_harness.cjs")],
        cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
