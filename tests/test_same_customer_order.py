import shutil
import subprocess
from pathlib import Path


def test_same_customer_order_runtime_boundaries():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [shutil.which("node"), str(root / "tests/same_customer_order.cjs"), str(root)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stdout + result.stderr
