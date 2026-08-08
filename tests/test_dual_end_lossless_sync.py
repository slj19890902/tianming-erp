from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE_HTML = (PROJECT_ROOT / "static" / "warehouse.html").read_text(
    encoding="utf-8"
)


def test_warehouse_merge_keeps_labels_and_printing_plate_ledger() -> None:
    """Freeze the manually reconciled seam between both home feature chains."""

    for marker in (
        "locationLabelSelection:new Set()",
        "moldSelection:new Set()",
        "printingPlates:[]",
        "printingPlates:0",
        'id="locationBatchPrint"',
        'id="moldBatchPrint"',
        'id="printingPlateSection"',
        "function locationLabelEligible(row)",
        "function loadPrintingPlates()",
        "function printingPlateStatusLabel(status)",
    ):
        assert marker in WAREHOUSE_HTML

    for marker in ("<<<<<<<", "=======", ">>>>>>>"):
        assert marker not in WAREHOUSE_HTML
