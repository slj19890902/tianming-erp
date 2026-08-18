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


def test_server_pagination_never_promotes_a_task_beyond_half_a4() -> None:
    from app.services.requisition_production_print_batch import (
        production_print_batch_pages,
    )

    cards = [_simple_card("one"), _complex_card("two"), _simple_card("three")]
    pages = production_print_batch_pages(cards)

    assert len(pages) == 2
    assert [(page["top"]["source_identity"], page["bottom"] and page["bottom"]["source_identity"]) for page in pages] == [
        ("one", "two"),
        ("three", None),
    ]
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
expect(layouts[0].top.id === 'simple' && layouts[0].bottom.id === 'complex', 'complex task escaped its half-page slot');
expect(layouts[1].top.id === 'odd' && layouts[1].bottom === null, 'odd task must leave the lower half blank');
expect(layouts.every((layout) => layout.fullPage === false), 'a task was promoted to full A4');
const single = printPageLayouts([{{id:'single'}}]);
expect(single.length === 1 && single[0].top.id === 'single' && single[0].bottom === null, 'single task layout changed');
expect(single[0].fullPage === false, 'single task must remain in the upper half');
"""
    subprocess.run(["node", "-e", script], cwd=ROOT, check=True)


def test_print_page_has_only_half_page_layout_and_keeps_overflow_fail_closed() -> None:
    assert "grid-template-rows:140.5mm 140.5mm" in PRINT_PAGE
    assert "single-page" not in PRINT_PAGE
    assert "full-card" not in PRINT_PAGE
    assert "cardNeedsFullPage" not in PRINT_PAGE
    assert 'class="page batch-page"' in PRINT_PAGE
    assert "每个生产任务固定半张 A4" in PRINT_PAGE
    assert "element.scrollHeight > element.clientHeight + 1" in PRINT_PAGE
    assert "任务内容超过页面容量，已停止打印" in PRINT_PAGE


def test_qr_caption_is_removed_and_header_space_favors_customer_name_and_product() -> None:
    assert 'alt="扫码查看当前产品资料"' in PRINT_PAGE
    assert "扫码看当前资料</span>" not in PRINT_PAGE
    assert "customer-metric" in PRINT_PAGE
    assert "code-metric" in PRINT_PAGE
    assert "name-metric" in PRINT_PAGE
    assert ".customer-metric strong { white-space:nowrap;" in PRINT_PAGE
    assert "grid-template-columns:1.24fr .62fr 1.14fr" in PRINT_PAGE
