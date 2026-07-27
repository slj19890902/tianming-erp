from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")


def test_location_ledger_exposes_placement_status_and_storage_type() -> None:
    assert 'id="locationStorageType"' in WAREHOUSE
    assert '<option value="">暂未确定</option>' in WAREHOUSE
    assert '<option value="ground">地面位</option>' in WAREHOUSE
    assert "待布局，禁止入库" in WAREHOUSE
    assert "x.placement_status!==\"unplaced\"" in WAREHOUSE
    assert "storage_type:storageType||null" in WAREHOUSE


def test_warehouse_inline_javascript_has_valid_syntax(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        return
    scripts = "\n".join(
        re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>",
            WAREHOUSE,
            flags=re.DOTALL,
        )
    )
    script = tmp_path / "n081-space-readiness-warehouse.js"
    script.write_text(scripts, encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(script)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
