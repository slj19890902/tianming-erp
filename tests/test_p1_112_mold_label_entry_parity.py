from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.test_mold_tool_workflow import _login, mold_app
from tests.test_p1_62_mold_40x80_label import _complete_mold


ROOT = Path(__file__).resolve().parents[1]
LABEL_PAGE = (ROOT / "static" / "mold-label.html").read_text(encoding="utf-8")
LAYOUT_JS = (
    ROOT / "static" / "assets" / "mold-label-layout.js"
).read_text(encoding="utf-8")
WAREHOUSE_PAGE = (ROOT / "static" / "warehouse.html").read_text(
    encoding="utf-8"
)


def test_current_40x80_catalog_uses_the_single_label_business_content() -> None:
    from app.services.mold_label_layout import default_layout, normalize_layout

    layout = normalize_layout(default_layout())
    assert layout["catalog_version"] == "p1-115-v1"
    assert layout["paper"] == {"width_mm": 80.0, "height_mm": 40.0}
    assert [element["id"] for element in layout["elements"]] == [
        "board_specification",
        "product_specification",
        "flute_type",
        "customer_name",
        "mold_number",
        "mold_qr",
    ]
    assert min(
        element["font_size_mm"]
        for element in layout["elements"]
        if element["kind"] == "text"
    ) >= 3.05


def test_single_batch_and_editor_share_one_current_40x80_renderer() -> None:
    assert 'CURRENT_WIDE_CATALOG="p1-115-v1"' in LABEL_PAGE
    assert "function labelHtml80(row){return TmMoldLabelLayout.labelHtml" in LABEL_PAGE
    assert 'attempt.source==="batch"?' in WAREHOUSE_PAGE
    assert "template_version=${encodeURIComponent(attempt.templateVersion)}" in WAREHOUSE_PAGE
    assert 'const V4_CATALOG_VERSION = "p1-112-v1"' in LAYOUT_JS
    assert 'const V5_CATALOG_VERSION = "p1-115-v1"' in LAYOUT_JS
    assert "product_specification" in LAYOUT_JS
    assert "mold_number" in LAYOUT_JS
    assert "inventory_code" not in LAYOUT_JS.split(
        "const CURRENT_ELEMENT_LABELS", 1
    )[1].split("});", 1)[0]


def test_40x80_keeps_real_paper_size_without_print_scaling() -> None:
    assert '@page{size:${wideTemplate?"40mm 80mm":"40mm 30mm"};margin:0}' in LABEL_PAGE
    assert "scale(" not in LABEL_PAGE
    assert "zoom:" not in LABEL_PAGE


def test_operation_single_and_selected_batch_project_identical_visible_facts(
    mold_app,
) -> None:
    from app.models.product import Product

    app, factory = mold_app
    mold_id = _complete_mold(factory, suffix="p112-parity")
    with factory() as db:
        db.add(
            Product(
                customer_id=1,
                product_code="P112-SECOND",
                customer_material_code="P112-SECOND",
                product_name="第二款共用产品",
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
        single_job = client.post(
            "/api/warehouse/molds/label-prints",
            json={
                "mold_ids": [mold_id],
                "source": "single",
                "template_version": "mold_80x40_v1",
                "idempotency_key": "p1-112-operation-single",
            },
        )
        batch_job = client.post(
            "/api/warehouse/molds/label-prints",
            json={
                "mold_ids": [mold_id],
                "source": "batch",
                "template_version": "mold_80x40_v1",
                "idempotency_key": "p1-112-selected-batch",
            },
        )
        assert single_job.status_code == batch_job.status_code == 200
        single_response = client.get(
            f"/api/warehouse/molds/{mold_id}/label",
            params={
                "template_version": "mold_80x40_v1",
                "print_job_id": single_job.json()["print_job_id"],
            },
        )
        batch_response = client.get(
            "/api/warehouse/molds/labels",
            params={
                "mold_ids": str(mold_id),
                "template_version": "mold_80x40_v1",
                "print_job_id": batch_job.json()["print_job_id"],
            },
        )
        single = single_response.json()
        batch_body = batch_response.json()
        batch = batch_body["items"][0]

    assert single["label_layout"] == batch_body["label_layout"]
    assert single["label_layout"]["layout"]["catalog_version"] == "p1-115-v1"

    for field in (
        "label_customer_name",
        "label_mold_number",
        "label_product_specification",
        "label_report_specification",
        "label_flute_type",
    ):
        assert batch[field] == single[field]
