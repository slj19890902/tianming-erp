from pathlib import Path


INDEX_HTML = Path(__file__).resolve().parents[1] / "static" / "index.html"


def test_tianhua_entry_is_adjacent_to_delivery_create_button():
    html = INDEX_HTML.read_text(encoding="utf-8")
    start = html.index("activePage === 'deliveries'")
    end = html.index("activePage === 'finance'", start)
    section = html[start:end]
    assert section.index("新增送货单") < section.index("导入天华预送货")
    assert '@click="openTianhuaPreimport"' in section


def test_tianhua_modal_calls_real_fastapi_endpoints():
    html = INDEX_HTML.read_text(encoding="utf-8")
    for required in (
        "modal.type === 'tianhuaPreimport'",
        "上传天华预送货截图",
        "开始识别",
        "识别结果",
        "生成送货单草稿",
        "/api/deliveries/tianhua-preimport/upload",
        "create-draft",
        "update-draft",
    ):
        assert required in html


def test_tianhua_result_table_has_batch_selection_controls():
    html = INDEX_HTML.read_text(encoding="utf-8")
    for required in (
        "全选可生成",
        "只选正常可送",
        "取消全选",
        "toggleAllTianhuaRows",
        "isTianhuaRowGeneratable",
        "异常不可生成",
        "tianhuaRowDisabledReason",
    ):
        assert required in html
