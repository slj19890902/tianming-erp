from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from tests.test_mold_label_print_pdf import (
    _current_print_styles,
    _qr_data_url,
    headless_browser,
)
from tests.test_mold_tool_workflow import _login
from tests.test_p1_103_mold_label_layout import _complete_named_mold, mold_app
from tests.test_p1_62_mold_40x80_label import _dump_rendered_dom


ROOT = Path(__file__).resolve().parents[1]
LABEL_PAGE = (ROOT / "static" / "mold-label.html").read_text(encoding="utf-8")
LAYOUT_JS = (ROOT / "static" / "assets" / "mold-label-layout.js").read_text(
    encoding="utf-8"
)
LAYOUT_CSS = (ROOT / "static" / "assets" / "mold-label-layout.css").read_text(
    encoding="utf-8"
)


def _p1_115_layout() -> dict:
    return {
        "catalog_version": "p1-115-v1",
        "paper": {"width_mm": 80.0, "height_mm": 40.0},
        "elements": [
            {"id": "board_specification", "kind": "text", "x_mm": 1.2, "y_mm": .8, "width_mm": 61.8, "height_mm": 7.0, "font_size_mm": 5.6, "font_weight": 900, "text_align": "left", "visible": True},
            {"id": "product_specification", "kind": "text", "x_mm": 1.2, "y_mm": 8.7, "width_mm": 48.0, "height_mm": 7.0, "font_size_mm": 4.8, "font_weight": 900, "text_align": "left", "visible": True},
            {"id": "flute_type", "kind": "text", "x_mm": 50.0, "y_mm": 8.7, "width_mm": 13.0, "height_mm": 7.0, "font_size_mm": 4.0, "font_weight": 800, "text_align": "left", "visible": True},
            {"id": "customer_name", "kind": "text", "x_mm": 1.2, "y_mm": 24.6, "width_mm": 61.8, "height_mm": 5.2, "font_size_mm": 4.3, "font_weight": 900, "text_align": "left", "visible": True},
            {"id": "mold_number", "kind": "text", "x_mm": 1.2, "y_mm": 30.0, "width_mm": 61.8, "height_mm": 8.8, "font_size_mm": 6.0, "font_weight": 900, "text_align": "left", "visible": True},
            {"id": "mold_qr", "kind": "qr", "x_mm": 64.4, "y_mm": 24.6, "width_mm": 14.2, "height_mm": 14.2, "visible": True},
        ],
    }


def test_current_wide_layout_separates_the_three_identity_facts() -> None:
    from app.services.mold_label_layout import (
        MoldLabelLayoutError,
        default_layout,
        normalize_layout,
    )

    layout = normalize_layout(default_layout())
    assert layout["catalog_version"] == "p1-117-v1"
    elements = {item["id"]: item for item in layout["elements"]}
    assert set(elements) == {
        "board_specification",
        "product_specification",
        "flute_type",
        "mold_identity",
        "mold_chinese_short_name",
        "mold_qr",
    }

    identity = elements["mold_identity"]
    short_name = elements["mold_chinese_short_name"]
    qr = elements["mold_qr"]
    assert identity["y_mm"] == qr["y_mm"]
    assert identity["font_size_mm"] == 6.0
    assert identity["font_weight"] == 900
    assert identity["text_align"] == "left"
    assert short_name["font_size_mm"] == 6.0
    assert short_name["font_weight"] == 900
    assert short_name["text_align"] == "left"
    assert short_name["y_mm"] >= identity["y_mm"] + identity["height_mm"]
    assert short_name["y_mm"] + short_name["height_mm"] <= 40.0

    assert 'CURRENT_WIDE_CATALOG="p1-117-v1"' in LABEL_PAGE
    assert 'const V6_CATALOG_VERSION = "p1-117-v1"' in LAYOUT_JS
    assert ".mold-identity-customer" in LAYOUT_CSS
    assert ".mold-identity-label" in LAYOUT_CSS
    assert "margin-left: .35em" in LAYOUT_CSS
    assert "font-size: 71.6667%" in LAYOUT_CSS

    invalid_alignment = default_layout()
    next(
        item
        for item in invalid_alignment["elements"]
        if item["id"] == "mold_chinese_short_name"
    )["text_align"] = "center"
    with pytest.raises(MoldLabelLayoutError, match="必须左对齐"):
        normalize_layout(invalid_alignment)


def test_wide_renderer_keeps_one_space_and_the_requested_font_hierarchy(
    headless_browser: Path,
    tmp_path: Path,
) -> None:
    from app.services.mold_label_layout import default_layout

    row = {
        "label_customer_name": "联测",
        "label_mold_number": "现场手写A17 短侧板",
        "label_mold_name": "现场手写A17",
        "label_mold_chinese_short_name": "短侧板",
        "label_product_specification": "430 × 280 × 160",
        "label_report_specification": "920 × 610",
        "label_flute_type": "AB",
        "qr_data_url": _qr_data_url(),
        "products": [],
    }
    envelope = {"version": 0, "layout": default_layout()}
    fixture = tmp_path / "p1-117-wide-identity.html"
    fixture.write_text(
        '<!doctype html><html><head><meta charset="utf-8">'
        + _current_print_styles()
        + '</head><body><main id="labels"></main><script>'
        + LAYOUT_JS.replace("</script>", "<\\/script>")
        + "</script><script>"
        + f"const row={json.dumps(row, ensure_ascii=False)};"
        + f"const envelope={json.dumps(envelope, ensure_ascii=False)};"
        + 'const labels=document.getElementById("labels");'
        + "labels.innerHTML=TmMoldLabelLayout.labelHtml(row,envelope);"
        + "const failures=TmMoldLabelLayout.fitAndValidate(labels);"
        + 'const line=labels.querySelector("[data-layout-id=mold_identity]");'
        + 'const customer=line.querySelector(".mold-identity-customer");'
        + 'const label=line.querySelector(".mold-identity-label");'
        + 'const shortName=labels.querySelector("[data-layout-id=mold_chinese_short_name]");'
        + 'document.body.dataset.fitFailures=failures.join("|");'
        + 'document.body.dataset.identityText=line.textContent;'
        + 'document.body.dataset.shortText=shortName.textContent;'
        + 'document.body.dataset.customerFont=parseFloat(getComputedStyle(customer).fontSize).toFixed(3);'
        + 'document.body.dataset.labelFont=parseFloat(getComputedStyle(label).fontSize).toFixed(3);'
        + 'document.body.dataset.shortFont=parseFloat(getComputedStyle(shortName).fontSize).toFixed(3);'
        + 'document.body.dataset.identityGap=(label.getBoundingClientRect().left-customer.getBoundingClientRect().right).toFixed(3);'
        + 'document.body.dataset.customerWeight=getComputedStyle(customer).fontWeight;'
        + 'document.body.dataset.shortAlign=getComputedStyle(shortName).textAlign;'
        + "</script></body></html>",
        encoding="utf-8",
    )

    rendered = _dump_rendered_dom(headless_browser, fixture, tmp_path)
    assert 'data-fit-failures=""' in rendered
    assert 'data-identity-text="联测 现场手写A17"' in rendered
    assert 'data-short-text="短侧板"' in rendered
    assert 'data-customer-weight="900"' in rendered
    assert 'data-short-align="left"' in rendered

    def body_number(name: str) -> float:
        marker = f'data-{name}="'
        return float(rendered.split(marker, 1)[1].split('"', 1)[0])

    customer_font = body_number("customer-font")
    label_font = body_number("label-font")
    short_font = body_number("short-font")
    identity_gap = body_number("identity-gap")
    assert customer_font > label_font
    assert label_font / customer_font == pytest.approx(4.3 / 6.0, abs=0.01)
    assert short_font / customer_font == pytest.approx(1.0, abs=0.01)
    assert identity_gap >= 4.0


def test_p1_115_snapshot_keeps_its_frozen_merged_second_line(
    headless_browser: Path,
    tmp_path: Path,
) -> None:
    from app.services.mold_label_layout import canonical_json, layout_hash, load_snapshot

    layout = _p1_115_layout()
    frozen = load_snapshot(
        version=12,
        payload_json=canonical_json(layout),
        payload_hash=layout_hash(layout),
    )
    assert frozen["version"] == 12
    assert frozen["layout"]["catalog_version"] == "p1-115-v1"
    assert [item["id"] for item in frozen["layout"]["elements"]][-3:] == [
        "customer_name",
        "mold_number",
        "mold_qr",
    ]

    row = {
        "label_customer_name": "联测",
        "label_mold_number": "现场手写A17 短侧板",
        "label_mold_name": "现场手写A17",
        "label_mold_chinese_short_name": "短侧板",
        "label_product_specification": "430 × 280 × 160",
        "label_report_specification": "920 × 610",
        "label_flute_type": "AB",
        "qr_data_url": _qr_data_url(),
        "products": [],
    }
    fixture = tmp_path / "p1-117-frozen-p1-115.html"
    fixture.write_text(
        '<!doctype html><html><head><meta charset="utf-8">'
        + _current_print_styles()
        + '</head><body><main id="labels"></main><script>'
        + LAYOUT_JS.replace("</script>", "<\\/script>")
        + "</script><script>"
        + f"const row={json.dumps(row, ensure_ascii=False)};"
        + f"const envelope={json.dumps(frozen, ensure_ascii=False)};"
        + 'const labels=document.getElementById("labels");'
        + "labels.innerHTML=TmMoldLabelLayout.labelHtml(row,envelope);"
        + 'document.body.dataset.text=[...labels.querySelectorAll(".mold-layout-text")].map(node=>node.textContent).join("|");'
        + "</script></body></html>",
        encoding="utf-8",
    )
    rendered = _dump_rendered_dom(headless_browser, fixture, tmp_path)
    assert (
        'data-text="片料 920 × 610|430 × 280 × 160|AB|联测|现场手写A17 短侧板"'
        in rendered
    )


def test_label_api_projects_name_and_chinese_short_name_for_both_paper_sizes(
    mold_app,
) -> None:
    app, factory = mold_app
    mold_id = _complete_named_mold(
        factory,
        label_name="现场手写A17",
        chinese_short_name="短侧板",
    )
    from app.models.mold_tool import MoldToolCustomer

    with factory() as db:
        db.add(
            MoldToolCustomer(
                mold_tool_id=mold_id,
                customer_id=1,
                display_order=1,
            )
        )
        db.commit()
    with TestClient(app) as client:
        _login(client, "workshop")
        compact = client.get(
            f"/api/warehouse/molds/{mold_id}/label",
            params={"template_version": "mold_40x30_v1"},
        )
        assert compact.status_code == 200, compact.text
        assert compact.json()["label_customer_name"] == "联测"
        assert compact.json()["label_mold_name"] == "现场手写A17"
        assert compact.json()["label_mold_chinese_short_name"] == "短侧板"

        registered = client.post(
            "/api/warehouse/molds/label-prints",
            json={
                "mold_ids": [mold_id],
                "source": "single",
                "template_version": "mold_80x40_v1",
                "idempotency_key": "p1-117-wide-identity-print",
            },
        )
        assert registered.status_code == 200, registered.text
        assert registered.json()["label_layout"]["layout"]["catalog_version"] == (
            "p1-117-v1"
        )
        wide = client.get(
            f"/api/warehouse/molds/{mold_id}/label",
            params={
                "template_version": "mold_80x40_v1",
                "print_job_id": registered.json()["print_job_id"],
            },
        )
        assert wide.status_code == 200, wide.text
        assert wide.json()["label_customer_name"] == "联测"
        assert wide.json()["label_mold_name"] == "现场手写A17"
        assert wide.json()["label_mold_chinese_short_name"] == "短侧板"


def test_compact_renderer_uses_separate_name_and_short_name_rows() -> None:
    renderer = LABEL_PAGE.split("function labelHtml40(row)", 1)[1].split(
        "function labelHtml80(row)", 1
    )[0]
    assert "row.label_mold_name" in renderer
    assert "row.label_mold_chinese_short_name" in renderer
    assert "mold-label-name" in renderer
    assert "mold-chinese-short-name" in renderer
    assert "customer-name" in renderer
