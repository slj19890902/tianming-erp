from __future__ import annotations

import base64
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - exercised only on minimal environments
    PdfReader = None


ROOT = Path(__file__).resolve().parents[1]
MOLD_LABEL_HTML = ROOT / "static" / "mold-label.html"
POINTS_TO_MM = 25.4 / 72.0


def _find_headless_browser() -> Path | None:
    command_names = (
        "msedge",
        "microsoft-edge",
        "microsoft-edge-stable",
        "chrome",
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    )
    for command_name in command_names:
        resolved = shutil.which(command_name)
        if resolved:
            return Path(resolved)

    candidates: list[Path] = []
    for environment_name in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        root = os.environ.get(environment_name)
        if not root:
            continue
        candidates.extend(
            (
                Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe",
                Path(root) / "Chromium" / "Application" / "chrome.exe",
            )
        )

    return next((candidate for candidate in candidates if candidate.is_file()), None)


@pytest.fixture(scope="module")
def headless_browser() -> Path:
    browser = _find_headless_browser()
    if browser is None:
        pytest.skip(
            "离线标签 PDF 回归需要 Edge/Chromium/Chrome，但当前环境未找到可执行文件"
        )
    return browser


@pytest.fixture(scope="module", autouse=True)
def require_pypdf() -> None:
    if PdfReader is None:
        pytest.skip("离线标签 PDF 回归需要 pypdf，但当前环境未安装")


def _current_print_styles() -> str:
    source = MOLD_LABEL_HTML.read_text(encoding="utf-8")
    styles = re.findall(r"<style\b[^>]*>.*?</style>", source, flags=re.IGNORECASE | re.DOTALL)
    assert styles, f"{MOLD_LABEL_HTML} 未找到可用于打印回归的内联样式"
    return "\n".join(styles)


def _qr_data_url() -> str:
    import qrcode
    from io import BytesIO

    qr = qrcode.QRCode(
        version=2,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=3,
        border=4,
    )
    qr.add_data("HTTP://192.168.3.80:8000/M/5")
    qr.make(fit=False)
    stream = BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(
        stream,
        format="PNG",
    )
    return "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()


def _label_markup(index: int, qr_data_url: str) -> str:
    number_variants = (
        ("61452621", ""),
        ("61452621R1F", "compact"),
        ("Z.001.000093", "long"),
        ("MOLD-2026-00000001", "xlong"),
        ("MOLD-2026-000000000001", "xxlong"),
    )
    mold_number, number_class = number_variants[(index - 1) % len(number_variants)]
    return f"""
      <article class="label">
        <div class="fact-row board-row"><span class="fact-key">片料</span><strong class="fact-value">880 × 425</strong></div>
        <div class="product-flute-row">
          <div class="inline-fact"><span class="fact-key">产品</span><strong class="fact-value">430 × 68</strong></div>
          <div class="inline-fact flute-fact"><span class="fact-key">楞型</span><strong class="fact-value flute">AB</strong></div>
        </div>
        <div class="identity">
          <div class="customer-name">聚晟达</div>
          <div class="mold-number {number_class}">{mold_number}</div>
        </div>
        <img class="qr" src="{qr_data_url}" alt="二维码">
      </article>
    """


def _write_offline_fixture(path: Path, label_count: int) -> None:
    qr_data_url = _qr_data_url()
    labels = "\n".join(
        _label_markup(index, qr_data_url)
        for index in range(1, label_count + 1)
    )
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  {_current_print_styles()}
</head>
<body>
  <section id="previewContent">
    <main id="labels" class="labels">
      {labels}
    </main>
  </section>
</body>
</html>
"""
    path.write_text(document, encoding="utf-8")


def _print_to_pdf(browser: Path, fixture: Path, output: Path, work_dir: Path) -> None:
    failures: list[str] = []
    for attempt, headless_flag in enumerate(("--headless=new", "--headless"), start=1):
        if output.exists():
            output.unlink()
        profile_dir = work_dir / f"browser-profile-{attempt}"
        command = [
            str(browser),
            headless_flag,
            "--disable-gpu",
            "--disable-extensions",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-pdf-header-footer",
            "--print-to-pdf-no-header",
            "--run-all-compositor-stages-before-draw",
            f"--user-data-dir={profile_dir}",
            f"--print-to-pdf={output}",
            fixture.resolve().as_uri(),
        ]
        try:
            result = subprocess.run(
                command,
                cwd=work_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired:
            failures.append(f"{headless_flag}: 120 秒超时")
            continue

        if result.returncode == 0 and output.is_file() and output.stat().st_size > 0:
            return
        details = (result.stderr or result.stdout or "无诊断输出").strip()
        failures.append(f"{headless_flag}: exit={result.returncode}; {details[-800:]}")

    pytest.fail("Edge/Chromium 无法生成离线标签 PDF：\n" + "\n".join(failures))


@pytest.mark.parametrize("label_count", (1, 2, 100))
def test_mold_label_print_pdf_has_one_40x30mm_page_per_label(
    label_count: int,
    headless_browser: Path,
    tmp_path: Path,
) -> None:
    fixture = tmp_path / f"mold-label-{label_count}.html"
    output = tmp_path / f"mold-label-{label_count}.pdf"
    _write_offline_fixture(fixture, label_count)
    _print_to_pdf(headless_browser, fixture, output, tmp_path)

    assert PdfReader is not None
    reader = PdfReader(output)
    assert len(reader.pages) == label_count, (
        f"{label_count} 张标签应严格生成 {label_count} 页，"
        f"实际生成 {len(reader.pages)} 页（可能存在漏页或额外空白页）"
    )

    for page_number, page in enumerate(reader.pages, start=1):
        width_mm = float(page.mediabox.width) * POINTS_TO_MM
        height_mm = float(page.mediabox.height) * POINTS_TO_MM
        assert width_mm == pytest.approx(40.0, abs=0.25), (
            f"第 {page_number} 页宽度应约为 40 mm，实际为 {width_mm:.3f} mm"
        )
        assert height_mm == pytest.approx(30.0, abs=0.25), (
            f"第 {page_number} 页高度应约为 30 mm，实际为 {height_mm:.3f} mm"
        )

        page_text = page.extract_text() or ""
        expected_number = (
            "61452621",
            "61452621R1F",
            "Z.001.000093",
            "MOLD-2026-00000001",
            "MOLD-2026-000000000001",
        )[(page_number - 1) % 5]
        for expected_text in ("片料", "楞型", "产品", "聚晟达", expected_number):
            assert expected_text in page_text, (
                f"第 {page_number} 页未找到标签字段 {expected_text}，"
                "该页可能为空白页或发生了标签跨页"
            )
