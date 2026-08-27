from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from pypdf import PdfReader

from tests.test_mold_label_print_pdf import (
    POINTS_TO_MM,
    _current_print_styles,
    _label_markup,
    _print_to_pdf,
    _qr_data_url,
    headless_browser,
)
from tests.test_p1_62_mold_40x80_label import _dump_rendered_dom, _layout_driven_label


ROOT = Path(__file__).resolve().parents[1]
LABEL_PAGE = (ROOT / "static" / "mold-label.html").read_text(encoding="utf-8")
LAYOUT_JS = (ROOT / "static" / "assets" / "mold-label-layout.js").read_text(
    encoding="utf-8"
)
LAYOUT_CSS = (ROOT / "static" / "assets" / "mold-label-layout.css").read_text(
    encoding="utf-8"
)


def test_current_layout_keeps_board_prefix_and_aligns_customer_with_qr() -> None:
    from app.services.mold_label_layout import default_layout, normalize_layout

    layout = normalize_layout(default_layout())
    assert layout["catalog_version"] == "p1-117-v1"

    elements = {item["id"]: item for item in layout["elements"]}
    customer = elements["mold_identity"]
    mold_number = elements["mold_chinese_short_name"]
    qr = elements["mold_qr"]

    assert customer["y_mm"] == qr["y_mm"]
    line_gap = mold_number["y_mm"] - (customer["y_mm"] + customer["height_mm"])
    assert 0 <= line_gap <= 0.3
    assert mold_number["y_mm"] + mold_number["height_mm"] <= 40.0

    assert 'CURRENT_WIDE_CATALOG="p1-117-v1"' in LABEL_PAGE
    assert 'const V6_CATALOG_VERSION = "p1-117-v1"' in LAYOUT_JS
    assert "catalogVersion === V6_CATALOG_VERSION" in LAYOUT_JS
    assert 'elementId === "board_specification" ? "片料 " : ""' in LAYOUT_JS


def test_40x30_renderer_keeps_board_prefix_only() -> None:
    renderer = LABEL_PAGE.split("function labelHtml40(row)", 1)[1].split(
        "function labelHtml80(row)", 1
    )[0]
    assert 'factRow("片料"' in renderer
    for prefix in ('inlineFact("产品"', 'inlineFact("楞型"'):
        assert prefix not in renderer
    for field in ("board", "productSize", "flute"):
        assert field in renderer


def test_print_assets_are_release_versioned_and_pages_cannot_flex_shrink() -> None:
    assert 'mold-label-layout.css?v=0.22.203' in LABEL_PAGE
    assert 'mold-label-layout.js?v=0.22.203' in LABEL_PAGE
    assert ".mold-label-page" in LAYOUT_CSS
    assert "flex: none" in LAYOUT_CSS
    assert "break-inside: avoid-page" in LAYOUT_CSS
    assert "contain: strict !important" in LAYOUT_CSS
    assert "width: 40mm !important" in LAYOUT_CSS
    assert "height: 80mm !important" in LAYOUT_CSS


def test_label_javascript_is_syntax_valid() -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for mold label syntax validation"
    external = subprocess.run(
        [node, "--check", str(ROOT / "static" / "assets" / "mold-label-layout.js")],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert external.returncode == 0, external.stderr
    inline_scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>", LABEL_PAGE, flags=re.DOTALL
        )
        if script.strip()
    ]
    assert len(inline_scripts) == 1
    inline = subprocess.run(
        [node, "--check"],
        input=inline_scripts[0],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert inline.returncode == 0, inline.stderr


def test_40x30_customer_top_is_explicitly_aligned_with_qr_top() -> None:
    assert "--identity-qr-top:15.1mm" in LABEL_PAGE
    assert "top:var(--identity-qr-top)" in LABEL_PAGE
    assert ".mold-number{margin-top:.15mm" in LABEL_PAGE


def test_40x30_customer_and_qr_top_render_at_the_same_coordinate(
    headless_browser: Path,
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "p1-115-40x30-top-alignment.html"
    fixture.write_text(
        '<!doctype html><html><head><meta charset="utf-8">'
        + _current_print_styles()
        + "</head><body>"
        + _label_markup(1, _qr_data_url())
        + "<script>"
        + 'const customer=document.querySelector(".customer-name").getBoundingClientRect();'
        + 'const qr=document.querySelector(".qr").getBoundingClientRect();'
        + "document.body.dataset.customerQrTopDelta="
        + "Math.abs(customer.top-qr.top).toFixed(3);"
        + "</script></body></html>",
        encoding="utf-8",
    )
    rendered = _dump_rendered_dom(headless_browser, fixture, tmp_path)
    match = re.search(r'data-customer-qr-top-delta="([0-9.]+)"', rendered)
    assert match is not None
    assert float(match.group(1)) <= 0.1


def test_single_and_batch_first_pages_render_at_identical_physical_size(
    headless_browser: Path,
    tmp_path: Path,
) -> None:
    fitz = pytest.importorskip("fitz")
    qr = _qr_data_url()

    def write_fixture(path: Path, labels: str) -> None:
        path.write_text(
            '<!doctype html><html class="template-80x40"><head><meta charset="utf-8">'
            + _current_print_styles()
            + '<style>@page{size:40mm 80mm;margin:0}</style></head>'
            + '<body class="template-80x40"><section id="previewContent">'
            + f'<main id="labels" class="labels">{labels}</main>'
            + "</section></body></html>",
            encoding="utf-8",
        )

    single_fixture = tmp_path / "p1-115-single.html"
    batch_fixture = tmp_path / "p1-115-batch.html"
    single_pdf = tmp_path / "p1-115-single.pdf"
    batch_pdf = tmp_path / "p1-115-batch.pdf"
    first_label = _layout_driven_label(1, qr)
    write_fixture(single_fixture, first_label)
    write_fixture(batch_fixture, first_label + _layout_driven_label(2, qr))

    _print_to_pdf(headless_browser, single_fixture, single_pdf, tmp_path)
    _print_to_pdf(headless_browser, batch_fixture, batch_pdf, tmp_path)

    single_reader = PdfReader(single_pdf)
    batch_reader = PdfReader(batch_pdf)
    assert len(single_reader.pages) == 1
    assert len(batch_reader.pages) == 2
    for page in (single_reader.pages[0], batch_reader.pages[0]):
        assert float(page.mediabox.width) * POINTS_TO_MM == pytest.approx(40.0, abs=0.25)
        assert float(page.mediabox.height) * POINTS_TO_MM == pytest.approx(80.0, abs=0.25)
        text = page.extract_text() or ""
        assert "片料" in text
        assert "产品" not in text
        assert "楞型" not in text

    with fitz.open(single_pdf) as single_document, fitz.open(batch_pdf) as batch_document:
        matrix = fitz.Matrix(203 / 72, 203 / 72)
        single_pixels = single_document[0].get_pixmap(matrix=matrix, colorspace=fitz.csGRAY)
        batch_pixels = batch_document[0].get_pixmap(matrix=matrix, colorspace=fitz.csGRAY)
        assert (single_pixels.width, single_pixels.height) == (
            batch_pixels.width,
            batch_pixels.height,
        )
        assert single_pixels.samples == batch_pixels.samples
