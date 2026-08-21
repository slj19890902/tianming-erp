from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRINT_PAGE = (ROOT / "static" / "requisition-production-print.html").read_text(
    encoding="utf-8"
)


def _function_body(source: str, start: str, end: str) -> str:
    section = source.split(start, 1)[1].split(end, 1)[0]
    return section.rsplit("}", 1)[0]


def _simple_card(identity: str) -> dict:
    return {
        "source_identity": identity,
        "components": [
            {
                "printing_situation": "单色印刷",
                "printing_plate_mode": "no_plate",
                "printing_colors": ["黑色"],
            }
        ],
    }


def _complex_card(identity: str) -> dict:
    return {
        "source_identity": identity,
        "components": [
            {
                "printing_situation": "三色印刷",
                "printing_plate_mode": "plate",
                "printing_plates": [{}, {}, {}],
            }
        ],
    }


def test_server_pagination_uses_two_fixed_half_a4_slots_for_all_counts() -> None:
    from app.services.requisition_production_print_batch import (
        production_print_batch_pages,
    )

    for count, page_count in ((1, 1), (2, 1), (3, 2), (5, 3)):
        pages = production_print_batch_pages(
            [_simple_card(str(index)) for index in range(count)]
        )
        assert len(pages) == page_count
        assert all(page["full_page"] is False for page in pages)
        assert (pages[-1]["bottom"] is None) is (count % 2 == 1)


def test_complex_task_keeps_sequence_and_never_promotes_to_full_a4() -> None:
    from app.services.requisition_production_print_batch import (
        production_print_batch_pages,
    )

    cards = [_simple_card("one"), _complex_card("two"), _simple_card("three")]
    pages = production_print_batch_pages(cards)

    assert [
        (page["top"]["source_identity"], page["bottom"] and page["bottom"]["source_identity"])
        for page in pages
    ] == [("one", "two"), ("three", None)]
    assert all(page["full_page"] is False for page in pages)


def test_browser_pagination_is_always_two_fixed_half_a4_slots() -> None:
    body = _function_body(
        PRINT_PAGE,
        "function printPageLayouts(cards) {",
        "function detailHtml(card) {",
    )
    script = f"""
const printPageLayouts = new Function('cards', {json.dumps(body, ensure_ascii=False)});
const expect = (value, message) => {{ if (!value) throw new Error(message); }};
const cards = [{{id:'simple'}}, {{id:'complex'}}, {{id:'odd'}}];
const layouts = printPageLayouts(cards);
expect(layouts.length === 2, 'three tasks must use two A4 pages');
expect(layouts[0].top.id === 'simple' && layouts[0].bottom.id === 'complex', 'sequence changed');
expect(layouts[1].top.id === 'odd' && layouts[1].bottom === null, 'odd lower half must be blank');
expect(layouts.every((layout) => layout.fullPage === false), 'task promoted to full A4');
const single = printPageLayouts([{{id:'single'}}]);
expect(single.length === 1 && single[0].top.id === 'single' && single[0].bottom === null, 'single layout changed');
expect(single[0].fullPage === false, 'single task must stay in upper half');
"""
    subprocess.run(["node", "-e", script], cwd=ROOT, check=True)


def test_print_page_has_only_half_page_layout_and_fail_closed_overflow() -> None:
    assert "grid-template-rows:140.5mm 140.5mm" in PRINT_PAGE
    assert "single-page" not in PRINT_PAGE
    assert "full-card" not in PRINT_PAGE
    assert "cardNeedsFullPage" not in PRINT_PAGE
    assert 'class="page batch-page"' in PRINT_PAGE
    assert "每个生产任务固定半张 A4" in PRINT_PAGE
    assert "element.scrollHeight > element.clientHeight + 1" in PRINT_PAGE
    assert "任务内容超过页面容量，已停止打印" in PRINT_PAGE


def test_qr_caption_removed_and_header_space_favors_business_fields() -> None:
    assert 'alt="扫码查看当前产品资料"' in PRINT_PAGE
    assert "扫码看当前资料</span>" not in PRINT_PAGE
    assert "customer-metric" in PRINT_PAGE
    assert "code-metric" in PRINT_PAGE
    assert "name-metric" in PRINT_PAGE
    assert ".customer-metric strong { white-space:nowrap;" in PRINT_PAGE
    assert "grid-template-columns:1.24fr .62fr 1.14fr" in PRINT_PAGE


def test_complex_cards_use_compact_half_page_styles_without_hiding_required_content() -> None:
    assert "function cardNeedsCompactLayout(card)" in PRINT_PAGE
    assert ".task-card.printing-heavy" in PRINT_PAGE
    assert ".task-card.printing-heavy .product-qr img { width:16mm; height:16mm; }" in PRINT_PAGE
    assert 'style="display:none"' not in PRINT_PAGE
    for label in (
        "客户",
        "存货编码",
        "产品名称",
        "成品内尺寸",
        "图号 / 图纸版本",
        "交期",
        "客户订单号",
        "订单数量",
        "库存抵扣",
        "计划生产",
        "采购张数",
        "材质 / 楞型",
        "特别注意事项",
    ):
        assert label in PRINT_PAGE
    assert 'class="structure-body"' not in PRINT_PAGE
    assert '<object data="${escapeHtml(drawing.url)}"' not in PRINT_PAGE
