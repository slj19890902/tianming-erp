from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


INDEX = Path("static/index.html").read_text(encoding="utf-8")


def test_over_receipt_requires_an_explicit_surplus_choice_and_sends_it() -> None:
    assert '<option value="finished">做成品</option>' in INDEX
    assert '<option value="semi_finished_reserve">片料备库</option>' in INDEX
    assert "incomingSurplusDispositionRequired(row)" in INDEX
    assert '!["finished","semi_finished_reserve"].includes(surplusDisposition)' in INDEX
    assert "surplus_disposition:surplusDisposition" in INDEX
    assert "finished_disposition_expected_finished_output_qty" in INDEX
    assert "semi_finished_reserve_expected_finished_output_qty" in INDEX


def test_over_delivery_confirmation_is_quantity_bound_and_auditable() -> None:
    assert "confirmDeliveryOverages(lines)" in INDEX
    assert "onDeliveryQuantityChanged(line)" in INDEX
    assert "line.over_delivery_confirmed = false" in INDEX
    assert "line.over_delivery_reason = \"\"" in INDEX
    assert "over_delivery_confirmed_quantity" in INDEX
    assert "over_delivery_confirmed_order_remaining" in INDEX
    assert "用户确认超订单送货" in INDEX
    assert "已取消超订单送货，本次没有保存" in INDEX


def test_index_inline_javascript_is_syntactically_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the frontend syntax contract"
    scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL
        )
        if script.strip()
    ]
    assert scripts
    target = tmp_path / "p0-32-index-inline.js"
    target.write_text("\n".join(scripts), encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
