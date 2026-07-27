from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest


PDF_ENVIRONMENT_VARIABLE = "ERP_TEST_PO2026070806_PDF"
EXPECTED_SIZE = 89_764
EXPECTED_SHA256 = (
    "D51C515AF501DEE949385EBF98E81FBE6D37FB6EB3653D5043C2C74917E22579"
)


def _external_pdf() -> Path:
    configured = os.getenv(PDF_ENVIRONMENT_VARIABLE, "").strip()
    if not configured:
        pytest.skip(
            f"set {PDF_ENVIRONMENT_VARIABLE} to the external original PDF path"
        )
    path = Path(configured).expanduser().resolve()
    if not path.is_file():
        pytest.fail(f"configured external original PDF is unavailable: {path}")
    return path


def test_po2026070806_original_pdf_parses_expected_order_without_git_fixture() -> None:
    from app.services.pdf_parse_pipeline import parse_pdf_bytes

    path = _external_pdf()
    content = path.read_bytes()

    assert len(content) == EXPECTED_SIZE
    assert hashlib.sha256(content).hexdigest().upper() == EXPECTED_SHA256

    draft = parse_pdf_bytes(content, path.name, []).draft
    integrity = draft["integrity_check"]

    assert draft["source_name"] == path.name
    assert draft["customer_po"] == "PO2026070806"
    assert draft["item_count"] == 7
    assert [item["line_no"] for item in draft["items"]] == [
        70,
        60,
        10,
        40,
        50,
        20,
        30,
    ]
    assert integrity["parsed_total_quantity"] == "288"
    assert integrity["parsed_total_amount"] == "1576.32"
    assert integrity["integrity_status"] == "passed"
