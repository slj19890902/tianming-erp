from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRINT_PAGE = (ROOT / "static" / "requisition-production-print.html").read_text(
    encoding="utf-8"
)
MAIN = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
SERVICE = (
    ROOT / "app" / "services" / "requisition_production_print.py"
).read_text(encoding="utf-8")
INCOMING = (ROOT / "app" / "api" / "incoming.py").read_text(encoding="utf-8")


def test_plan_and_receipt_urls_share_one_physical_print_template() -> None:
    assert not (ROOT / "static" / "incoming-production-card.html").exists()
    assert MAIN.count('/ "requisition-production-print.html"') >= 2
    assert 'route.path == "/incoming-production-card.html"' in MAIN
    assert "receiptMode" in PRINT_PAGE
    assert "待来料计划版" in PRINT_PAGE
    assert "本次实收" in PRINT_PAGE
    assert "本批最多生产" in PRINT_PAGE
    assert "实收与计划完全一致" in PRINT_PAGE


def test_unified_paper_contract_keeps_plan_and_actual_quantities_separate() -> None:
    for marker in (
        '"paper_phase": "planned"',
        '"paper_phase": "actual_receipt"',
        '"planned_sheet_quantity"',
        '"received_sheet_quantity"',
        '"production_capacity_quantity"',
        '"paper_version_key"',
        'f"actual:receipt:{version[\'receipt_item_id\']}"',
        '"reuses_planned_card": not requires_actual',
    ):
        assert marker in SERVICE
    assert "计划版不可冒充实收版" in SERVICE
    assert "matched_reuse_plan" in SERVICE


def test_receipt_projection_is_read_only_and_legacy_facts_fail_closed() -> None:
    ast.parse(INCOMING)
    ast.parse(SERVICE)
    endpoint_start = INCOMING.index("def incoming_production_card(")
    endpoint_end = INCOMING.index('\n\n@router.get("/surplus-locations")', endpoint_start)
    endpoint = INCOMING[endpoint_start:endpoint_end]
    assert "build_receipt_production_print_package(db, fact)" in endpoint
    assert "_legacy_receipt_production_package(legacy_card)" in endpoint
    assert "db.add(" not in endpoint
    assert "db.commit(" not in endpoint
    assert "db.flush(" not in endpoint


def test_unified_page_only_allows_explicit_audited_label_plan_refresh() -> None:
    assert 'method:"GET"' in PRINT_PAGE
    assert "window.print()" in PRINT_PAGE
    assert PRINT_PAGE.count('method:"POST"') == 1
    assert "/api/production/tasks/${encodeURIComponent(row.task_id)}/label-plan-refresh" in PRINT_PAGE
    assert "confirmed_not_started:true" in PRINT_PAGE
    assert "confirmed_no_prior_print:true" in PRINT_PAGE
    assert 'method:"PUT"' not in PRINT_PAGE
    assert 'method:"DELETE"' not in PRINT_PAGE
    assert PRINT_PAGE.count("window.print()") == 1
    assert 'const actualClass = actualPhase ? " actual-card" : "";' in PRINT_PAGE
    assert ".task-card.actual-card .receipt-facts" in PRINT_PAGE
    assert "cardNeedsCompactLayout(card) || actualPhase" in PRINT_PAGE
    assert "single-page" not in PRINT_PAGE
