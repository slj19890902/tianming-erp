from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
TASK_PRINT = (ROOT / "static" / "requisition-production-print.html").read_text(
    encoding="utf-8"
)
LABEL_PRINT = (ROOT / "static" / "production-packaging-label.html").read_text(
    encoding="utf-8"
)
MAIN = (ROOT / "app" / "main.py").read_text(encoding="utf-8")


def _inline_scripts(source: str) -> list[str]:
    return [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>", source, re.DOTALL
        )
        if script.strip()
    ]


def test_product_editor_exposes_disabled_by_default_label_policy() -> None:
    for marker in (
        "打印标签",
        'aria-label="每张标签代表只数"',
        'v-model="productForm.production_label_enabled"',
        'v-model="productForm.production_label_units_per_label"',
        "production_label_enabled: false",
        "production_label_units_per_label: null",
        "onProductProductionLabelToggle",
        "productProductionLabelError",
    ):
        assert marker in INDEX

    assert 'code === "a1_0201"' in INDEX
    for box_type in (
        "die_cut_inner_box",
        "liner",
        "die_cut_partition",
        "divider",
    ):
        assert box_type in INDEX
    assert "每张标签数量必须是正整数" in INDEX
    assert "payload.production_label_units_per_label = null" in INDEX
    assert "!!productProductionLabelError" in INDEX


def test_product_policy_participates_in_payload_hydration_and_dirty_tracking() -> None:
    assert "payload.production_label_enabled = payload.production_label_enabled === true" in INDEX
    assert "form.production_label_enabled = form.production_label_enabled === true" in INDEX
    assert "production_label_enabled: f.production_label_enabled" in INDEX
    assert (
        "production_label_units_per_label: f.production_label_units_per_label"
        in INDEX
    )
    label_panel = INDEX.split('class="product-production-label-config"', 1)[1]
    assert "打印标签" in label_panel
    assert 'aria-label="每张标签代表只数"' in label_panel
    assert "每张标签代表只数" in label_panel
    assert "product-production-label-note" not in label_panel


def test_waiting_material_task_page_opens_separate_packaging_label_page() -> None:
    assert 'id="labelButton"' in TASK_PRINT
    assert "生产包装标签" in TASK_PRINT
    assert "/production-packaging-label.html?id=" in TASK_PRINT
    assert 'window.open(`/production-packaging-label.html?id=${encodeURIComponent(orderId)}`, "_blank", "noopener")' in TASK_PRINT
    assert "window.opener" not in TASK_PRINT


def test_packaging_label_page_uses_current_size_and_audited_print_jobs() -> None:
    compact = re.sub(r"\s+", "", LABEL_PRINT)
    for marker in (
        "生产包装标签",
        "客户名称",
        "存货编码",
        "产品名称",
        "规格",
        "本标签数量",
        "current_40x30_v1",
        "legacy_65x45_v1",
        "40 × 30 mm",
        "65 × 45 mm（历史作业）",
        "--label-width:40mm",
        "--label-height:30mm",
        "width:var(--label-width)",
        "height:var(--label-height)",
        "label.customer_name",
        "label.product_code",
        "label.product_name",
        "label.specification",
        "/production-packaging-label-package",
        "/production-packaging-label-jobs",
        'method:"GET"',
        'method:"POST"',
        'method:"PUT"',
        "/production-packaging-label-layout/admin/draft",
        'credentials:"include"',
        'cache:"no-store"',
        "detail.reasons",
        "生产计划已变化或标签快照不完整",
        "确认已实际打印",
        "scrollHeight",
        "clientHeight",
        "scrollWidth",
        "clientWidth",
    ):
        assert marker in LABEL_PRINT

    assert "@page{size:40mm30mm;margin:0" in compact
    assert "@page{size:65mm45mm;margin:0" in compact
    assert LABEL_PRINT.count("window.print()") == 1
    assert 'label.customer_code || label.customer_name' not in LABEL_PRINT
    assert "text-overflow:ellipsis" not in LABEL_PRINT

    for forbidden in (
        "计划总数：",
        "标签序号：",
        "生产任务：",
        "报料单号：",
        "系统订单：",
        "客户单号：",
        "计划指纹：",
        "background:#000",
        "background:black",
        "库存批次",
        "库位",
        "可用库存",
        "实收数量",
        "单价",
        "成本",
        "价格",
        'method:"PATCH"',
        'method:"DELETE"',
        "window.opener",
        "finished-goods-label",
    ):
        assert forbidden not in LABEL_PRINT


def test_packaging_label_route_uses_conditional_same_origin_file_response() -> None:
    assert 'route.path == "/production-packaging-label.html"' in MAIN
    assert '"production-packaging-label.html"' in MAIN
    assert "_conditional_file_endpoint(production_packaging_label_path)" in MAIN
    assert 'methods=["GET"]' in MAIN


def test_packaging_label_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the label contract test"
    scripts = _inline_scripts(LABEL_PRINT)
    assert len(scripts) == 1
    target = tmp_path / "production-packaging-label-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
