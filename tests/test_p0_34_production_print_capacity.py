from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PRINT_PAGE = ROOT / "static" / "requisition-production-print.html"


def _headless_browser() -> Path | None:
    for command in ("msedge", "chrome", "chromium"):
        resolved = shutil.which(command)
        if resolved:
            return Path(resolved)
    candidates = []
    for variable in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        root = os.environ.get(variable)
        if not root:
            continue
        candidates.extend(
            (
                Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe",
            )
        )
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _normal_complex_package() -> dict:
    plates = [
        {
            "color_name": color,
            "plate_code": f"PL-{index:03d}",
            "plate_name": content,
            "current_location": f"一楼印版区第{index}排第{index + 1}位",
        }
        for index, (color, content) in enumerate(
            (("黑色", "客户标志与产品型号"), ("绿色", "环保标识与方向箭头"), ("专红", "易碎品与防潮标识")),
            start=1,
        )
    ]
    component = {
        "component_label": "整片",
        "report_length_mm": 965,
        "report_width_mm": 625,
        "material_code": "K=A 五层加强",
        "flute_type": "BC楞",
        "layer_count": 5,
        "crease_display": "165 / 330 / 165 / 330",
        "cutting_mode": "一开一",
        "print_content": "客户标志、产品型号、环保标识、方向箭头、易碎品与防潮标识",
        "printing_situation": "三色挂板印刷",
        "printing_plate_mode": "plate",
        "printing_plates": plates,
        "plate_alignment_value_mm": 1.25,
        "plate_mount_value_mm": 2.5,
        "machine_set_length_mm": 965,
        "machine_set_width_mm": 625,
        "machine_set_height_mm": 520,
        "mold_tool_id": 18,
        "mold_display_name": "聚晟达 965*625 三色风机加强外箱",
        "mold_location_display": "一楼模具架东侧第二排第三位",
        "mold_is_active": True,
    }
    card = {
        "layout_kind": "carton",
        "box_type_code": "a1_0201",
        "paper_phase": "actual_receipt",
        "paper_phase_label": "分批实收版 1/1",
        "customer_name": "苏州聚晟达电子科技有限公司",
        "product_code": "61452621R1F",
        "product_name": "48入装风机五层加强纸箱",
        "specifications": ["520 × 350 × 300 mm"],
        "structure_reference": {"name": "JSD-48FAN-R1 / REV.F"},
        "delivery_dates": ["2026-09-03"],
        "customer_pos": ["JSD-PO-20260901-001"],
        "customer_order_quantity": 1200,
        "stock_deduction_quantity": 100,
        "planned_finished_quantity": 1100,
        "requisition_quantity": 1120,
        "joining_method": "粘贴 / 钉箱",
        "production_steps": ["先模切，再三色印刷；印刷方向按图纸；粘口不得露钉；成品十只一捆"],
        "components": [component],
        "receipt_number": "RC20260901001",
        "received_sheet_quantity": 1120,
        "production_capacity_quantity": 1120,
        "actual_board_length_mm": 965,
        "actual_board_width_mm": 625,
        "inventory_lot_number": "LOT-20260901-001",
        "employee_location_name": "一楼原料暂存区东侧第二垛",
        "fulfillment_reminders": [
            {
                "scope_type": "product",
                "product_code": "61452621R1F",
                "product_name": "48入装风机五层加强纸箱",
                "content": "每捆十只，标签朝外，同一订单不得混批",
                "source_delivery_number": "DN20260831001",
                "source_received_date": "2026-08-31",
                "suggested_quantity": 10,
                "created_by_name": "仓库管理员",
            }
        ],
        "supplier_order_number": "PO2026090101",
        "paper_version_key": "actual:receipt:20260901001",
        "received_at": "2026-09-01 10:30",
        "review_required": False,
    }
    return {
        "pages": [],
        "cards": [card],
        "printable": True,
        "card_count": 1,
        "created_at": "2026-09-01 10:30",
        "plan_fingerprint": "p0-34-normal-complex",
        "production_label_count": 0,
        "production_label_refresh_options": [],
    }


def _render_package(tmp_path: Path, package: dict) -> str:
    browser = _headless_browser()
    if browser is None:
        pytest.skip("当前环境未找到 Edge/Chrome，跳过实际半张 A4 DOM 验收")

    source = PRINT_PAGE.read_text(encoding="utf-8")
    for name, tag in [('production-task-paper.js','script'), ('production-task-paper.css','style')]:
        content = (ROOT/'static/ui'/name).read_text(encoding='utf-8')
        import re
        pattern = r'<script src="/static/ui/production-task-paper.js\?v=\d+"></script>' if tag == 'script' else r'<link rel="stylesheet" href="/static/ui/production-task-paper.css\?v=\d+">'
        source = re.sub(pattern, lambda _: f'<{tag}>'+content+f'</{tag}>', source)
    package_json = json.dumps(package, ensure_ascii=False)
    mock = (
        "<script>window.fetch=async()=>({ok:true,status:200,json:async()=>("
        + package_json
        + ")});</script>\n  <script>\n    (() => {"
    )
    source = source.replace("  <script>\n    (() => {", mock, 1)
    probe = """
    <script>
      (() => {
        const deadline = Date.now() + 5000;
        const inspect = () => {
          const card = document.querySelector('.task-card:not(.blank)');
          if ((!card || document.getElementById("printButton").disabled) && !document.querySelector(".message.error") && Date.now() < deadline) { setTimeout(inspect, 50); return; }
          if (!card) { document.body.dataset.probeComplete = 'missing'; return; }
          document.body.dataset.probeComplete = 'true';
          document.body.dataset.cardOverflow = String(card.scrollHeight > card.clientHeight + 1);
          document.body.dataset.printDisabled = String(document.getElementById('printButton').disabled);
          document.body.dataset.cardHeight = String(Math.round(card.getBoundingClientRect().height));
          document.body.dataset.cardScrollHeight = String(card.scrollHeight);
          document.body.dataset.message = document.getElementById('message').textContent;
        };
        inspect();
      })();
    </script>
    """
    source = source.replace("</body>", probe + "</body>")
    fixture = tmp_path / "p0-34-normal-complex-task.html"
    fixture.write_text(source, encoding="utf-8")

    result = subprocess.run(
        [
            str(browser),
            "--headless=new",
            "--disable-gpu",
            "--disable-extensions",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1200,1400",
            "--virtual-time-budget=6000",
            f"--user-data-dir={tmp_path / 'profile'}",
            "--dump-dom",
            fixture.resolve().as_uri() + "?id=1",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-1000:]
    return result.stdout


def test_normal_three_color_receipt_task_continues_without_shrinking(tmp_path: Path) -> None:
    output = _render_package(tmp_path, _normal_complex_package())
    assert 'data-probe-complete="true"' in output
    assert 'data-card-overflow="false"' in output
    assert 'data-print-disabled="false"' in output


def test_extreme_required_text_still_fails_closed_and_names_source(tmp_path: Path) -> None:
    package = _normal_complex_package()
    package["cards"][0]["production_steps"] = [
        "极端超长工艺说明：" + "逐箱核对印刷方向、粘口、标签和捆扎批次；" * 80
    ]
    output = _render_package(tmp_path, package)
    assert 'data-probe-complete="true"' in output
    assert 'data-print-disabled="true"' in output
    assert 'data-print-disabled="true"' in output
    assert "任务内容过长：工艺要求" in output


def test_capacity_layout_retains_readable_font_and_rejects_clipping():
    source = (ROOT/'static/ui/production-task-paper.css').read_text(encoding='utf-8')
    assert 'font-size:14pt' in source and 'font-size:13pt' in source
    assert 'font-size:5.5pt' not in source
    script = (ROOT/'static/ui/production-task-paper.js').read_text(encoding='utf-8')
    assert '任务内容过长' in script
    assert 'text-overflow:ellipsis' not in source
