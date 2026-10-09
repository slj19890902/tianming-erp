from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRINT_PAGE = (ROOT / "static" / "requisition-production-print.html").read_text(
    encoding="utf-8"
)
PRINT_SERVICE = (ROOT / "app" / "services" / "requisition_production_print.py").read_text(
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


def test_large_font_renderer_replaces_ultra_layout():
    assert 'ProductionTaskPaper.render' in PRINT_PAGE
    render = PRINT_PAGE.split('async function render(packageData)',1)[1].split('function packageEndpoint',1)[0]
    assert 'ultra' not in render
    assert 'overflowingCards().length' in render
