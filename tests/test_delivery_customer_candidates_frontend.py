from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_body(name: str) -> str:
    pattern = rf"(?:async\s+)?{re.escape(name)}\([^)]*\)\s*\{{"
    match = re.search(pattern, INDEX)
    assert match is not None, f"missing Vue method: {name}"
    next_method = re.search(
        r"\n\s{10,}(?:async\s+)?[A-Za-z_$][\w$]*\([^)]*\)\s*\{",
        INDEX[match.end() :],
    )
    assert next_method is not None, f"cannot delimit Vue method: {name}"
    return INDEX[match.start() : match.end() + next_method.start()]


def test_new_delivery_customer_selector_uses_real_candidate_union() -> None:
    modal_start = INDEX.index("modal.type === 'delivery'")
    modal_end = INDEX.index("modal.type === 'tianhuaPreimport'", modal_start)
    modal = INDEX[modal_start:modal_end]

    assert 'v-for="c in deliveryCustomerOptions"' in modal
    assert 'v-for="c in activeCustomerOptions"' not in modal
    assert "当前没有待送货客户" in modal


def test_candidate_loader_and_default_selection_use_filtered_customers() -> None:
    loader = _method_body("loadDeliveryPendingItems")
    opener = _method_body("openDelivery")

    assert 'axios.get("/api/deliveries/pending_items")' in loader
    assert "this.deliveryCustomerCandidates = data.customer_candidates || []" in loader
    assert "const availableCustomers = this.deliveryCustomerOptions || []" in opener
    assert "availableCustomers[0]?.id || null" in opener
    assert "activeCustomerOptions" not in opener


def test_editing_keeps_saved_customer_visible_even_if_no_longer_candidate() -> None:
    computed = _method_body("deliveryCustomerOptions")

    assert "this.deliveryForm.editingId" in computed
    assert "Number(row.id) === editingCustomerId" in computed


def test_batch_picker_requests_all_pending_items_without_a_keyword() -> None:
    loader = _method_body("loadDeliveryBatchItems")

    assert 'axios.get("/api/deliveries/pending-items/search"' in loader
    assert "list_all: true" in loader
