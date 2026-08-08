from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _reported_page() -> str:
    start = INDEX.index("requisitionTab==='submitted'", INDEX.index("activePage === 'requisition'"))
    end = INDEX.index("activePage === 'incoming'", start)
    return INDEX[start:end]


def test_reported_page_prioritizes_customer_length_and_width_filters() -> None:
    page = _reported_page()
    first_row = page[
        page.index('<div v-if="requisitionTab===\'submitted\'" class="list-filterbar">') :
    ]
    markers = [
        "reportedFilters.customer_id",
        "reportedFilters.report_length_mm",
        "reportedFilters.report_width_mm",
        ">查询<",
        "详细查找",
        ">清空<",
    ]
    positions = [first_row.index(marker) for marker in markers]
    assert positions == sorted(positions)
    assert ':options="reportedCustomerOptions"' in page
    assert 'label-key="label"' in page
    assert "reportedFilters.material_code" in page
    assert "材质代码（支持 +）" in page


def test_reported_page_groups_one_document_row_and_expands_all_lines() -> None:
    page = _reported_page()
    assert "reported-document-table" in page
    assert "查看明细" in page
    assert "row.line_items || []" in page
    assert "line.stable_id || line.id" in page
    assert "reported-match-row" in page
    assert "reportedFieldMatched(line,'report_length_mm')" in page
    assert "reportedFieldMatched(line,'report_width_mm')" in page
    assert "当前条件命中" in page
    assert "报料单仍按一张一行显示" in page


def test_reported_loader_keeps_server_paging_and_latest_request_guard() -> None:
    start = INDEX.index("async loadReportedDocuments() {")
    end = INDEX.index("async selectRequisitionTab", start)
    loader = INDEX[start:end]
    assert "page: this.pages.requisitionReported" in loader
    assert "page_size: this.pageSize" in loader
    assert 'beginLatestRequest("requisition:reported")' in loader
    assert 'axios.get("/api/requisition/reported-documents"' in loader
    assert "data.matched_line_count" in loader
    assert 'axios.get("/api/requisition/reported-customer-options"' in loader


def test_reported_document_table_never_requires_horizontal_scrolling() -> None:
    assert ".reported-compact-table { table-layout: fixed; width: 100%; min-width: 0; }" in INDEX
    assert ".reported-detail-card { display: grid;" in INDEX
    assert "overflow-wrap: anywhere" in INDEX


def test_inline_javascript_remains_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for frontend syntax validation"
    scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "q1-21-index.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
