from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_body(name: str) -> str:
    """Return the Vue method body up to the following method declaration."""

    pattern = rf"(?:async\s+)?{re.escape(name)}\([^)]*\)\s*\{{"
    match = re.search(pattern, INDEX)
    assert match is not None, f"missing Vue method: {name}"
    next_method = re.search(
        r"\n\s{10,}(?:async\s+)?[A-Za-z_$][\w$]*\([^)]*\)\s*\{",
        INDEX[match.end() :],
    )
    assert next_method is not None, f"cannot delimit Vue method: {name}"
    return INDEX[match.start() : match.end() + next_method.start()]


def _delivery_page_section() -> str:
    start = INDEX.index("activePage === 'deliveries'")
    end = INDEX.index("activePage === 'finance'", start)
    return INDEX[start:end]


def test_delivery_first_paint_loads_only_delivery_list() -> None:
    body = _method_body("loadDeliveries")

    assert 'axios.get("/api/deliveries"' in body
    assert "/api/deliveries/pending_items" not in body
    assert "/api/delivery-picks" not in body

    # A refresh must keep a readable list until the replacement response is
    # available.  Clearing it before the await would create a false empty state.
    assert "this.deliveries = []" not in body
    assert "this.deliveries.length = 0" not in body
    assert body.index('axios.get("/api/deliveries"') < body.index("this.deliveries =")


def test_pending_delivery_candidates_are_loaded_only_for_new_or_edit_delivery() -> None:
    pending = _method_body("loadDeliveryPendingItems")
    assert 'axios.get("/api/deliveries/pending_items")' in pending
    customer_options = _method_body("loadDeliveryCustomerOptions")
    assert 'axios.get("/api/deliveries/pending-customer-options", { params:' in customer_options

    open_body = _method_body("openDelivery")
    edit_body = _method_body("editDelivery")
    assert "await this.loadDeliveryCustomerOptions()" in open_body
    assert "loadDeliveryPendingItems" not in open_body
    assert "await this.loadDeliveryPendingItems()" in edit_body

    # The list loader must stay independent from candidate preparation.  Batch
    # and line search keep their existing on-demand search endpoint.
    assert "loadDeliveryPendingItems" not in _method_body("loadDeliveries")
    assert "/api/deliveries/pending-items/search" in _method_body(
        "loadDeliveryBatchItems"
    )
    assert "/api/deliveries/pending-items/search" in _method_body(
        "searchDeliveryLine"
    )


def test_delivery_page_has_distinct_initial_refresh_error_and_empty_states() -> None:
    page = _delivery_page_section()

    # These four flags make it impossible for an unfinished first request to
    # render as an empty result, while a failed refresh preserves old rows.
    for marker in (
        "deliveryListState.initialLoading",
        "deliveryListState.refreshing",
        "deliveryListState.error",
        "deliveryListState.loaded",
        "正在加载送货单",
        "正在刷新送货单",
        "送货单加载失败",
        "当前没有符合条件的送货单",
    ):
        assert marker in page

    # The shared data-panel's generic empty text must not be used for the
    # delivery first-paint branch, because it cannot distinguish loading.
    assert ':empty="!deliveries.length"' not in page


def test_delivery_first_paint_inline_javascript_is_syntax_checkable(tmp_path: Path) -> None:
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
    target = tmp_path / "p1-09a-delivery-first-paint.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
