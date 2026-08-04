import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MOBILE = (ROOT / "static" / "mobile_tianhua_pick.html").read_text(encoding="utf-8")


def test_tianhua_mobile_load_has_retry_and_stale_response_guards() -> None:
    assert 'id="retry"' in MOBILE
    assert "let loadGeneration = 0" in MOBILE
    assert "loadController?.abort()" in MOBILE
    assert "generation !== loadGeneration" in MOBILE
    assert "new AbortController()" in MOBILE
    assert "正在刷新拿货明细" in MOBILE


def test_tianhua_mobile_write_prevents_duplicate_taps() -> None:
    assert "const busyItems = new Set()" in MOBILE
    assert "if (busyItems.has(id)) return" in MOBILE
    assert "busyItems.add(id)" in MOBILE
    assert "busyItems.delete(id)" in MOBILE
    assert "button.disabled=true" in MOBILE


def test_tianhua_mobile_errors_are_actionable_and_safe() -> None:
    assert "无法连接 ERP 服务，请检查网络后重试" in MOBILE
    assert "typeof detail.message === \"string\"" in MOBILE
    assert "HTTP ${response.status}" in MOBILE
    assert "JSON.stringify({token" in MOBILE
    assert "拿货状态已保存" in MOBILE
    assert "正式发货" not in MOBILE


def test_tianhua_mobile_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for JavaScript syntax validation"
    scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>", MOBILE, flags=re.DOTALL
        )
        if script.strip()
    ]
    assert scripts
    target = tmp_path / "mobile_tianhua_pick.js"
    target.write_text("\n".join(scripts), encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
