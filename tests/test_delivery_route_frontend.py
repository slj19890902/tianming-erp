import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_delivery_route_assistant_is_auxiliary_and_uses_read_only_api() -> None:
    assert "送货路线辅助" in INDEX
    assert "/api/deliveries/route-suggestions" in INDEX
    assert "不是实时路况最优解" in INDEX
    assert "从上一站导航到此" in INDEX
    assert "same_as_previous_address" in INDEX
    assert "navigation_note" in INDEX
    assert "deliveryRoutePlan" in INDEX


def test_delivery_route_frontend_inline_script_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for frontend syntax validation"
    scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, flags=re.DOTALL
        )
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "delivery-route-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
