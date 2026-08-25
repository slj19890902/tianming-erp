from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services.production_packaging_label_layout import default_layout
from app.services.production_packaging_label import apply_packaging_label_print_counts
from tests.test_mold_label_print_pdf import (
    POINTS_TO_MM,
    PdfReader,
    _print_to_pdf,
    headless_browser,
)


ROOT = Path(__file__).resolve().parents[1]
PRINT_PAGE = ROOT / "static" / "production-packaging-label.html"


def _function_source(
    source: str,
    function_name: str,
    next_function_name: str,
) -> str:
    match = re.search(
        rf"(?:async\s+)?function\s+{re.escape(function_name)}\s*\([^)]*\)\s*\{{"
        rf"(?P<body>.*?)\n\s*(?:async\s+)?function\s+{re.escape(next_function_name)}\s*\(",
        source,
        flags=re.DOTALL,
    )
    assert match, f"missing function boundary: {function_name} -> {next_function_name}"
    return match.group("body")


def _print_styles(source: str) -> str:
    styles = re.findall(
        r"<style\b[^>]*>.*?</style>",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert styles
    return "\n".join(styles)


def _render_labels(layout: dict, quantities: list[int]) -> str:
    source = PRINT_PAGE.read_text(encoding="utf-8")
    suffix_body = _function_source(
        source,
        "quantityFixedSuffix",
        "validFixedSuffix",
    )
    value_body = _function_source(
        source,
        "layoutElementValue",
        "layoutDrivenLabelHtml",
    )
    renderer_body = _function_source(
        source,
        "layoutDrivenLabelHtml",
        "legacyLabelHtml",
    )
    node = shutil.which("node")
    assert node is not None
    script = f"""
const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");
const requiredText = (value) => typeof value === "string" && value.trim() ? value.trim() : "";
const DEFAULT_QUANTITY_FIXED_SUFFIX = "只/捆";
const hasOwn = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
function quantityFixedSuffix(element) {{{suffix_body}
function layoutElementValue(label, elementId) {{{value_body}
function layoutDrivenLabelHtml(label, layout) {{{renderer_body}
const layout = {json.dumps(layout, ensure_ascii=False)};
const quantities = {json.dumps(quantities)};
const labels = quantities.map((quantity, index) => ({{
  label_number:index + 1,
  label_count:quantities.length,
  customer_short_name:"思迈",
  product_code:`P1-104-${{String(index + 1).padStart(4, "0")}}`,
  product_name:"HIDDEN-PRODUCT-NAME",
  specification:"380x260x220mm",
  quantity,
}}));
process.stdout.write(labels.map((label) => layoutDrivenLabelHtml(label, layout)).join(""));
"""
    result = subprocess.run(
        [node, "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _write_fixture(path: Path, layout: dict, quantities: list[int]) -> None:
    source = PRINT_PAGE.read_text(encoding="utf-8")
    markup = _render_labels(layout, quantities)
    path.write_text(
        f"""<!doctype html>
<html lang="zh-CN" data-template="current_40x30_v2">
<head><meta charset="utf-8">{_print_styles(source)}</head>
<body><main class="label-list">{markup}</main></body>
</html>
""",
        encoding="utf-8",
    )


def _dump_dom(browser: Path, fixture: Path, work_dir: Path) -> str:
    failures: list[str] = []
    for attempt, headless_flag in enumerate(("--headless=new", "--headless"), start=1):
        profile = work_dir / f"dom-profile-{attempt}"
        command = [
            str(browser),
            headless_flag,
            "--disable-gpu",
            "--disable-extensions",
            "--no-first-run",
            "--no-default-browser-check",
            "--virtual-time-budget=4000",
            f"--user-data-dir={profile}",
            "--dump-dom",
            fixture.resolve().as_uri(),
        ]
        result = subprocess.run(
            command,
            cwd=work_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        if result.returncode == 0 and 'data-complete="true"' in result.stdout:
            return result.stdout
        failures.append(
            f"{headless_flag}: exit={result.returncode}; "
            f"{(result.stderr or result.stdout or '无输出')[-500:]}"
        )
    pytest.fail("Edge/Chromium 无法完成生产标签 DOM 预检：\n" + "\n".join(failures))


def _write_preflight_fixture(path: Path) -> None:
    source = PRINT_PAGE.read_text(encoding="utf-8")
    suffix_body = _function_source(source, "quantityFixedSuffix", "validFixedSuffix")
    value_body = _function_source(
        source,
        "layoutElementValue",
        "layoutDrivenLabelHtml",
    )
    renderer_body = _function_source(
        source,
        "layoutDrivenLabelHtml",
        "legacyLabelHtml",
    )
    preflight_body = _function_source(
        source,
        "preflightEditorLayout",
        "showMessage",
    )
    save_body = _function_source(
        source,
        "saveLayoutDraft",
        "runReleaseOperation",
    )
    layout = copy.deepcopy(default_layout())
    product_code = next(
        element for element in layout["elements"] if element["id"] == "product_code"
    )
    product_code["width_mm"] = 1.2
    document = f"""<!doctype html>
<html lang="zh-CN" data-template="current_40x30_v2">
<head><meta charset="utf-8">{_print_styles(source)}</head>
<body data-complete="false">
<script>
window.requestAnimationFrame = (callback) => window.setTimeout(() => callback(performance.now()), 0);
const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");
const requiredText = (value) => typeof value === "string" && value.trim() ? value.trim() : "";
const DEFAULT_QUANTITY_FIXED_SUFFIX = "只/捆";
const hasOwn = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
const LAYOUT_ELEMENT_LABELS = {{product_code:"存货编码"}};
const deepClone = (value) => JSON.parse(JSON.stringify(value));
function quantityFixedSuffix(element) {{{suffix_body}
function layoutElementValue(label, elementId) {{{value_body}
function layoutDrivenLabelHtml(label, layout) {{{renderer_body}
let editorLayout = {json.dumps(layout, ensure_ascii=False)};
const loadedPackage = {{labels:[{{
  label_number:1,label_count:1,customer_short_name:"思迈",
  product_code:"P1-104-LONG-PRODUCT-CODE",product_name:"外箱",
  specification:"380x260x220mm",quantity:50,
}}]}};
let layoutAdminState = {{draft:{{version:0,layout:editorLayout}},published:{{version:0}}}};
let layoutOperationBusy = false;
let layoutMutationAttempt = null;
let postCalls = 0;
const validateLayoutEnvelope = () => editorLayout;
const setLayoutEditorBusy = (busy) => {{ layoutOperationBusy = busy; }};
const layoutMutationKey = () => "p1-104-preflight";
const requestJson = async () => {{ postCalls += 1; return layoutAdminState; }};
const renderLayoutEditor = () => {{}};
const showLayoutStatus = (message, kind="") => {{
  document.body.dataset.status = message;
  document.body.dataset.kind = kind;
}};
async function preflightEditorLayout() {{{preflight_body}
async function saveLayoutDraft({{quiet=false}} = {{}}) {{{save_body}
(async () => {{
  const saved = await saveLayoutDraft();
  document.body.dataset.saved = String(saved);
  document.body.dataset.postCalls = String(postCalls);
  document.body.dataset.complete = "true";
}})();
</script>
</body>
</html>
"""
    path.write_text(document, encoding="utf-8")


def _quantity_layout(fixed_suffix: str | None, *, legacy_missing: bool = False) -> dict:
    layout = copy.deepcopy(default_layout())
    product_name = next(
        element for element in layout["elements"] if element["id"] == "product_name"
    )
    product_name["visible"] = False
    quantity = next(
        element for element in layout["elements"] if element["id"] == "quantity"
    )
    if legacy_missing:
        quantity.pop("fixed_suffix", None)
    else:
        quantity["fixed_suffix"] = fixed_suffix
    return layout


def test_reduced_3000_piece_job_keeps_30_full_labels_and_the_layout_snapshot() -> None:
    layout = _quantity_layout("只")
    package = {
        "template_version": "current_40x30_v2",
        "plan_fingerprint": "f" * 64,
        "production_task_count": 1,
        "label_count": 300,
        "plans": [
            {
                "production_task_id": 104,
                "production_task_version": 1,
                "product_id": 104,
                "product_version": 1,
                "template_version": "current_40x30_v2",
                "total_quantity": 3000,
                "units_per_label": 10,
                "label_count": 300,
            }
        ],
        "labels": [
            {
                "production_task_id": 104,
                "label_number": number,
                "label_count": 300,
                "quantity": 10,
                "units_per_label": 10,
            }
            for number in range(1, 301)
        ],
        "label_layout": {
            "version": 7,
            "layout": layout,
            "layout_hash": "a" * 64,
        },
        "printable": True,
        "review_required": False,
    }

    frozen = apply_packaging_label_print_counts(package, {104: 30})

    assert frozen["system_label_count"] == 300
    assert frozen["label_count"] == 30
    assert frozen["plans"][0]["print_label_count"] == 30
    assert [label["quantity"] for label in frozen["labels"]] == [10] * 30
    assert frozen["label_layout"] == package["label_layout"]


def test_editor_preflight_blocks_overflow_before_any_layout_write(
    headless_browser: Path,
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "production-layout-preflight.html"
    _write_preflight_fixture(fixture)

    dom = _dump_dom(headless_browser, fixture, tmp_path)

    assert 'data-complete="true"' in dom
    assert 'data-saved="false"' in dom
    assert 'data-post-calls="0"' in dom
    assert 'data-kind="error"' in dom
    assert "存货编码在当前宽高和字号下无法完整显示" in dom


@pytest.mark.parametrize("label_count", (1, 2, 30, 300))
def test_production_layout_pdf_has_one_40x30_page_per_label_and_keeps_tail_quantity(
    label_count: int,
    headless_browser: Path,
    tmp_path: Path,
) -> None:
    if PdfReader is None:
        pytest.skip("生产标签 PDF 回归需要 pypdf")
    quantities = [50] * label_count
    quantities[-1] = 3
    fixture = tmp_path / f"production-layout-{label_count}.html"
    output = tmp_path / f"production-layout-{label_count}.pdf"
    _write_fixture(fixture, _quantity_layout("只"), quantities)
    _print_to_pdf(headless_browser, fixture, output, tmp_path)

    reader = PdfReader(output)
    assert len(reader.pages) == label_count
    for page in reader.pages:
        assert float(page.mediabox.width) * POINTS_TO_MM == pytest.approx(
            40.0,
            abs=0.25,
        )
        assert float(page.mediabox.height) * POINTS_TO_MM == pytest.approx(
            30.0,
            abs=0.25,
        )
        assert "HIDDEN-PRODUCT-NAME" not in (page.extract_text() or "")
    tail = re.sub(r"\s+", "", reader.pages[-1].extract_text() or "")
    assert "3只" in tail


@pytest.mark.parametrize(
    ("fixed_suffix", "legacy_missing", "expected"),
    (("只/捆", False, "50只/捆"), ("只", False, "50只"), ("", False, "50"), (None, True, "50只/捆")),
)
def test_production_layout_pdf_uses_explicit_empty_and_legacy_suffix_semantics(
    fixed_suffix: str | None,
    legacy_missing: bool,
    expected: str,
    headless_browser: Path,
    tmp_path: Path,
) -> None:
    if PdfReader is None:
        pytest.skip("生产标签 PDF 回归需要 pypdf")
    fixture = tmp_path / f"production-suffix-{expected.replace('/', '-')}.html"
    output = tmp_path / f"production-suffix-{expected.replace('/', '-')}.pdf"
    _write_fixture(
        fixture,
        _quantity_layout(fixed_suffix, legacy_missing=legacy_missing),
        [50],
    )
    _print_to_pdf(headless_browser, fixture, output, tmp_path)

    reader = PdfReader(output)
    assert len(reader.pages) == 1
    text = re.sub(r"\s+", "", reader.pages[0].extract_text() or "")
    assert expected in text
    if fixed_suffix == "":
        assert "只" not in text
        assert "捆" not in text
