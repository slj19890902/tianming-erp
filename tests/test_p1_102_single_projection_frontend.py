from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_pdf_inventory_locator_uses_canonical_position_status() -> None:
    source = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    start = source.index("          pdfWarehouseLocatorLabel(candidate)")
    end = source.index("          closePdfWarehouseLocator(", start)
    locator = source[start:end]

    assert 'location.position_status === "mapped"' in locator
    assert 'location.map_status === "floor3_mapped"' not in locator
    assert '"实测地图定位"' in locator
