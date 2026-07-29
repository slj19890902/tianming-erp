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


def test_model_level_validation_error_shows_backend_message(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        return
    function_start = WAREHOUSE.index("function apiErrorMessage")
    function_end = WAREHOUSE.index("function apiErrorCode", function_start)
    script = tmp_path / "warehouse-validation-message.js"
    script.write_text(
        WAREHOUSE[function_start:function_end]
        + """
const modelError = apiErrorMessage(
  {detail:[{loc:["body"],msg:"Value error, 三楼库位必须填写所属区域"}]},
  422,
  "/api/warehouse/locations"
);
if (modelError !== "三楼库位必须填写所属区域") {
  throw new Error(`unexpected model error: ${modelError}`);
}
const fieldError = apiErrorMessage(
  {detail:[{loc:["body","location_code"],msg:"String should have at least 1 character"}]},
  422,
  "/api/warehouse/locations"
);
if (fieldError !== "库位编码输入有误") {
  throw new Error(`unexpected field error: ${fieldError}`);
}
const customFieldError = apiErrorMessage(
  {detail:[{loc:["body","warehouse_type"],msg:"Value error, 库位类型必须是成品、半成品或共用"}]},
  422,
  "/api/warehouse/locations"
);
if (customFieldError !== "库位类型输入有误") {
  throw new Error(`unexpected custom field error: ${customFieldError}`);
}
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(script)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


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
