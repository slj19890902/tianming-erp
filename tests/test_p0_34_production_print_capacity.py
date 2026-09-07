from __future__ import annotations

import json
import shutil
import re
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PRINT_PAGE = ROOT / "static" / "requisition-production-print.html"


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


def _render_package(tmp_path: Path, package: dict) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("当前环境未找到 Node.js")
    source = PRINT_PAGE.read_text(encoding="utf-8")
    inline_script = source.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = """
import vm from 'node:vm';
const nodes = new Map();
const node = id => {
  if (!nodes.has(id)) nodes.set(id, {
    innerHTML:'', textContent:'', disabled:false, hidden:false,
    addEventListener(){}, querySelectorAll(){return [];},
  });
  return nodes.get(id);
};
const document = {getElementById:node, body:{classList:{toggle(){}}}};
const window = {
  location:new URL('http://fixture.invalid/requisition-production-print.html?id=1'),
  history:{replaceState(){}},
};
vm.runInNewContext(INLINE_SCRIPT, {
  document, window, URL, URLSearchParams, AbortController, setTimeout,
  fetch:async()=>({ok:true,status:200,json:async()=>(PACKAGE)}),
});
await new Promise(setImmediate);
process.stdout.write(JSON.stringify({
  html:node('pages').innerHTML,
  print_disabled:node('printButton').disabled,
  toolbar:node('toolbarNote').textContent,
  message:node('message').textContent,
}));
""".replace("INLINE_SCRIPT", json.dumps(inline_script, ensure_ascii=False)).replace(
        "PACKAGE", json.dumps(package, ensure_ascii=False)
    )
    script = tmp_path / "print-render.mjs"
    script.write_text(harness, encoding="utf-8")
    result = subprocess.run(
        [node, str(script)], capture_output=True, text=True,
        encoding="utf-8", timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr[-1500:]
    return json.loads(result.stdout)


def test_three_color_receipt_keeps_all_plate_facts(tmp_path: Path) -> None:
    output = _render_package(tmp_path, _normal_complex_package())
    assert output["print_disabled"] is False
    assert "长内容自动续页" in output["toolbar"]
    assert all(value in output["html"] for value in (
        "PL-001", "PL-002", "PL-003", "当前位置", "机器设定",
    ))


def test_long_names_notes_and_every_process_component_remain_on_paper(tmp_path: Path) -> None:
    package = _normal_complex_package()
    card = package["cards"][0]
    name = "超长产品名称" * 80 + "产品名称末尾"
    notes = "逐箱核对印刷方向、粘口、标签和捆扎批次；" * 80 + "工艺末尾"
    card["product_name"] = name
    card["production_steps"] = [notes]
    original = card["components"][0]
    card["components"] = [
        dict(original, component_label=f"组件{n}", mold_tool_id=n,
             mold_display_name=f"模具{n}") for n in range(1, 5)
    ]
    reminder = card["fulfillment_reminders"][0]
    card["fulfillment_reminders"] = [
        dict(reminder, content=f"回单交代{n}") for n in range(1, 4)
    ]
    output = _render_package(tmp_path, package)
    assert output["print_disabled"] is False
    assert name in output["html"]
    assert notes in output["html"]
    assert all(f"组件{n}" in output["html"] for n in range(1, 5))
    assert all(f"模具{n}" in output["html"] for n in range(1, 5))
    assert all(f"回单交代{n}" in output["html"] for n in range(1, 4))
    assert "ultra-compact" not in output["html"]
    assert "…扫码查看" not in output["html"]


def test_print_styles_keep_fourteen_point_floor_and_allow_fragmentation() -> None:
    source = PRINT_PAGE.read_text(encoding="utf-8")
    style = source.split("<style>", 1)[1].split("</style>", 1)[0]
    sizes = [float(value) for value in re.findall(r"font-size:(\d+(?:\.\d+)?)pt", style)]
    assert sizes and min(sizes) >= 14
    assert ".task-card small { font-size:14pt; }" in style
    assert "overflow:hidden" not in style
    assert "height:297mm;" not in style.replace("min-height:297mm;", "")
    assert "break-inside:auto" in style
    assert "break-before:page" in style
    assert "grid-template-rows:140.5mm" not in style
    assert "const initialOverflow" not in source


def test_business_print_block_is_preserved(tmp_path: Path) -> None:
    package = _normal_complex_package()
    package["printable"] = False
    assert _render_package(tmp_path, package)["print_disabled"] is True
