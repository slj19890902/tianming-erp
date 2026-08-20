from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from pypdf import PdfReader
import pytest
from sqlalchemy import func, select

from tests.test_mold_tool_workflow import _login, mold_app
from tests.test_mold_label_print_pdf import (
    POINTS_TO_MM,
    _current_print_styles,
    _print_to_pdf,
    _qr_data_url,
    headless_browser,
)


ROOT = Path(__file__).resolve().parents[1]
LABEL = (ROOT / "static" / "mold-label.html").read_text(encoding="utf-8")
WAREHOUSE = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")


def _complete_mold(factory, *, suffix: str = "1") -> int:
    from app.models.mold_tool import MoldTool
    from app.models.product import Product

    with factory() as db:
        mold = MoldTool(
            mold_code=f"P162-M-{suffix}",
            mold_name=f"思迈尔 P162-{suffix}",
            rack_location="1F-M-R01-L1-G01",
        )
        db.add(mold)
        db.flush()
        db.add(
            Product(
                customer_id=1,
                product_code=f"SME-LONG-CODE-{suffix}",
                customer_material_code=f"SME-LONG-CODE-{suffix}",
                product_name=f"五层加强纸箱横向标签样例{suffix}",
                length_mm=520,
                width_mm=350,
                height_mm=300,
                report_length_mm=1100,
                report_width_mm=760,
                flute_type="BC",
                mold_tool_id=mold.id,
            )
        )
        db.commit()
        return mold.id


def test_80x40_projection_and_print_fact_are_explicit_and_idempotent(mold_app) -> None:
    from app.models.mold_tool import MoldLabelPrintJob

    app, factory = mold_app
    mold_id = _complete_mold(factory)
    with TestClient(app) as client:
        _login(client, "workshop")
        legacy = client.get(f"/api/warehouse/molds/{mold_id}/label")
        wide = client.get(
            f"/api/warehouse/molds/{mold_id}/label",
            params={"template_version": "mold_80x40_v1"},
        )
        assert legacy.status_code == wide.status_code == 200
        assert "template_version" not in legacy.json()
        body = wide.json()
        assert body["template_version"] == "mold_80x40_v1"
        assert body["template_label"] == "40×80"
        assert body["label_inventory_code"] == "SME-LONG-CODE-1"
        assert body["label_product_name"] == "五层加强纸箱横向标签样例1"
        assert body["label_product_specification"] == "520 × 350 × 300"
        assert body["label_report_specification"] == "1100 × 760"
        assert body["label_flute_type"] == "BC"
        assert body["lookup_url"] == legacy.json()["lookup_url"]
        assert body["qr_data_url"] == legacy.json()["qr_data_url"]

        payload = {
            "mold_ids": [mold_id],
            "source": "single",
            "template_version": "mold_80x40_v1",
            "idempotency_key": "p1-62-wide-print-0001",
        }
        created = client.post("/api/warehouse/molds/label-prints", json=payload)
        assert created.status_code == 200, created.text
        assert created.json()["template_version"] == "mold_80x40_v1"
        replay = client.post("/api/warehouse/molds/label-prints", json=payload)
        assert replay.status_code == 200
        assert replay.json()["print_job_id"] == created.json()["print_job_id"]
        assert replay.json()["replayed"] is True
        conflict = client.post(
            "/api/warehouse/molds/label-prints",
            json={**payload, "template_version": "mold_40x30_v1"},
        )
        assert conflict.status_code == 409

    with factory() as db:
        assert db.scalar(select(func.count(MoldLabelPrintJob.id))) == 1
        job = db.scalar(select(MoldLabelPrintJob))
        assert job is not None and job.template_version == "mold_80x40_v1"


def test_80x40_fails_closed_for_multiple_bindings_without_writing_fact(mold_app) -> None:
    from app.models.mold_tool import MoldLabelPrintJob
    from app.models.product import Product

    app, factory = mold_app
    mold_id = _complete_mold(factory, suffix="2")
    with factory() as db:
        db.add(
            Product(
                customer_id=1,
                product_code="SME-SECOND",
                customer_material_code="SME-SECOND",
                product_name="第二款",
                length_mm=400,
                width_mm=300,
                report_length_mm=900,
                report_width_mm=650,
                flute_type="B",
                mold_tool_id=mold_id,
            )
        )
        db.commit()

    with TestClient(app) as client:
        _login(client, "workshop")
        legacy = client.get(f"/api/warehouse/molds/{mold_id}/label")
        assert legacy.status_code == 200
        assert legacy.json()["label_product_specification"] == "多款见扫码"
        wide = client.get(
            f"/api/warehouse/molds/{mold_id}/label",
            params={"template_version": "mold_80x40_v1"},
        )
        assert wide.status_code == 409
        assert "一模多款" in wide.json()["detail"]
        rejected = client.post(
            "/api/warehouse/molds/label-prints",
            json={
                "mold_ids": [mold_id],
                "source": "single",
                "template_version": "mold_80x40_v1",
                "idempotency_key": "p1-62-multi-reject-0001",
            },
        )
        assert rejected.status_code == 409
    with factory() as db:
        assert db.scalar(select(func.count(MoldLabelPrintJob.id))) == 0


def test_page_and_warehouse_select_one_frozen_paper_template() -> None:
    for marker in (
        'value="mold_40x30_v1"',
        'value="mold_80x40_v1"',
        "80×40（40宽卷纸横向）",
        "template_version:attempt.templateVersion",
        "moldLabelPrintSignature(source,ids,templateVersion)",
        "不能更换模具或纸型",
    ):
        assert marker in WAREHOUSE
    for marker in (
        'const TEMPLATE_40X30="mold_40x30_v1",TEMPLATE_80X40="mold_80x40_v1"',
        "@page{size:${wideTemplate?\"40mm 80mm\":\"40mm 30mm\"};margin:0}",
        ".label.template-80x40{width:80mm;height:40mm",
        "width:13.9mm;height:13.9mm",
        "transform:translateX(40mm) rotate(90deg)!important",
        'WIDE_PRINTER_QUEUE="Gprinter GP-3120TU - 40x80纵向标签"',
        "真实 40×80 纵向纸型",
        '.template-80x40 .wide-inventory{font:900 6.3mm/.95',
        ".template-80x40 .wide-customer{font-size:4mm",
        "label_inventory_code",
        "label_product_name",
        "wide-inventory",
        "wide-flute",
        "waitForQrImages",
        "window.print()",
    ):
        assert marker in LABEL
    assert "40mm 80mm" in LABEL
    assert "rotate(90deg)" in LABEL
    assert "--print-x-compensation:2mm" in LABEL
    assert "body,html{width:40mm;height:auto" in LABEL


@pytest.mark.parametrize("label_count", (1, 2, 100))
def test_40x80_feed_uses_one_portrait_page_with_one_inner_rotation(
    label_count: int,
    headless_browser: Path,
    tmp_path: Path,
) -> None:
    qr = _qr_data_url()
    labels = "".join(
        f'''<article class="label template-80x40">
        <div class="wide-board">1100 × 760</div>
        <div class="wide-product-row"><div class="wide-product">520 × 350 × 300</div><div class="wide-flute">BC</div></div>
        <div class="wide-identity"><div class="wide-inventory">SME-LONG-CODE-{index:03d}</div><div class="wide-meta"><span class="wide-customer">思迈尔</span><span class="wide-name">五层加强纸箱横向标签样例</span></div></div>
        <img class="qr" src="{qr}" alt="二维码"></article>'''
        for index in range(1, label_count + 1)
    )
    fixture = tmp_path / f"p1-62-{label_count}.html"
    output = tmp_path / f"p1-62-{label_count}.pdf"
    fixture.write_text(
        '<!doctype html><html class="template-80x40"><head><meta charset="utf-8">'
        + _current_print_styles()
        + '<style>@page{size:40mm 80mm;margin:0}</style></head>'
        + f'<body class="template-80x40"><section id="previewContent"><main id="labels" class="labels">{labels}</main></section></body></html>',
        encoding="utf-8",
    )
    _print_to_pdf(headless_browser, fixture, output, tmp_path)
    reader = PdfReader(output)
    assert len(reader.pages) == label_count
    for page_number, page in enumerate(reader.pages, start=1):
        width_mm = float(page.mediabox.width) * POINTS_TO_MM
        height_mm = float(page.mediabox.height) * POINTS_TO_MM
        assert width_mm == pytest.approx(40.0, abs=0.25)
        assert height_mm == pytest.approx(80.0, abs=0.25)
        assert height_mm > width_mm
        text = page.extract_text() or ""
        for expected in (
            "1100 × 760",
            "520 × 350 × 300",
            "BC",
            f"SME-LONG-CODE-{page_number:03d}",
            "思迈尔",
            "五层加强纸箱横向标签样例",
        ):
            assert expected in text


def test_40x80_portrait_pixels_keep_rotated_content_inside_physical_page(
    headless_browser: Path,
    tmp_path: Path,
) -> None:
    fitz = pytest.importorskip("fitz")
    qr = _qr_data_url()
    fixture = tmp_path / "p1-62-visible-bounds.html"
    output = tmp_path / "p1-62-visible-bounds.pdf"
    fixture.write_text(
        '<!doctype html><html class="template-80x40"><head><meta charset="utf-8">'
        + _current_print_styles()
        + '<style>@page{size:40mm 80mm;margin:0}</style></head>'
        + f'''<body class="template-80x40"><section id="previewContent"><main id="labels" class="labels">
        <article class="label template-80x40">
          <div class="wide-board">705 × 700</div>
          <div class="wide-product-row"><div class="wide-product">180 × 160 × 110</div><div class="wide-flute">B</div></div>
          <div class="wide-identity"><div class="wide-inventory">3.D30257</div><div class="wide-meta"><span class="wide-customer">高泰</span><span class="wide-name">纸箱16×18×11内箱</span></div></div>
          <img class="qr" src="{qr}" alt="二维码">
        </article></main></section></body></html>''',
        encoding="utf-8",
    )
    _print_to_pdf(headless_browser, fixture, output, tmp_path)

    document = fitz.open(output)
    page = document[0]
    pixmap = page.get_pixmap(matrix=fitz.Matrix(300 / 72, 300 / 72), colorspace=fitz.csGRAY)
    dark_pixels = [
        (index % pixmap.width, index // pixmap.width)
        for index, value in enumerate(pixmap.samples)
        if value < 180
    ]
    assert dark_pixels, "40×80 标签渲染后不应为空白"
    left = min(point[0] for point in dark_pixels)
    right = max(point[0] for point in dark_pixels)
    top = min(point[1] for point in dark_pixels)
    bottom = max(point[1] for point in dark_pixels)
    pixels_per_mm = 300 / 25.4
    assert left >= 0.5 * pixels_per_mm
    assert right <= pixmap.width - 0.5 * pixels_per_mm
    assert top >= 0.7 * pixels_per_mm
    assert bottom <= pixmap.height - 0.7 * pixels_per_mm
    assert (bottom - top) / pixels_per_mm >= 60
