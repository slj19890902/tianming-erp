from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")
LABEL = (ROOT / "static" / "finished-goods-label.html").read_text(
    encoding="utf-8"
)


def test_finished_ledger_exposes_version_bound_label_action() -> None:
    assert "openFinishedGoodsLabel(${row.id},${row.version})" in WAREHOUSE
    assert "打印标签" in WAREHOUSE
    assert "/static/finished-goods-label.html?lot_id=" in WAREHOUSE
    assert '"_blank","noopener"' in WAREHOUSE
    assert 'row.status!=="closed"&&physical>0' in WAREHOUSE


def test_label_contains_required_factory_fields_without_prices() -> None:
    for marker in (
        "成品货物标签",
        "当前物理位置",
        "存货编码待确认",
        "当前实物",
        "库存批次：",
        "完工/来源：",
        "标签版本：",
        "打印人/时间：",
        "扫码核对当前位置和标签版本",
        "查看仓库位置",
        "标签打印和补打不入库、不移库、不扣库存",
    ):
        assert marker in LABEL
    for forbidden in ("单价", "成本", "平方价", "estimated_unit_cost"):
        assert forbidden not in LABEL
    assert "size: A4 portrait" in LABEL
    assert "page-break-inside: avoid" in LABEL


def test_label_page_reads_only_the_scoped_versioned_projection() -> None:
    assert 'request("/api/auth/me")' in LABEL
    assert "/api/warehouse/lots/${encodeURIComponent(lotId)}/label?expected_version=" in LABEL
    assert "/warehouse.html?tab=locations&location_id=" in LABEL
    assert 'credentials:"include"' in LABEL
    assert "旧标签已失效" not in LABEL
    for forbidden in (
        'method:"POST"',
        'method: "POST"',
        'method:"PUT"',
        'method: "PUT"',
        'method:"DELETE"',
        'method: "DELETE"',
    ):
        assert forbidden not in LABEL


def test_finished_goods_label_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the label contract test"
    scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>", LABEL, re.DOTALL
        )
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "finished-goods-label-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
