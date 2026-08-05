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

    assert ':options="deliveryCustomerOptions"' in modal
    assert 'label-key="_delivery_label"' in modal
    assert 'placeholder="输入客户名称、缩写或拼音首字母"' in modal
    assert 'v-for="c in activeCustomerOptions"' not in modal
    assert "正在加载可送货客户" in modal
    assert "当前没有待送订单或可送成品库存" in modal


def test_candidate_loader_opens_first_and_does_not_guess_a_customer() -> None:
    loader = _method_body("loadDeliveryCustomerOptions")
    opener = _method_body("openDelivery")

    assert 'axios.get("/api/deliveries/pending-customer-options")' in loader
    assert "this.deliveryCustomerCandidates = data.items || []" in loader
    assert 'this.modal = { type:"delivery", title:"新增送货单" }' in opener
    assert opener.index('this.modal = { type:"delivery", title:"新增送货单" }') < opener.index("await this.loadDeliveryCustomerOptions()")
    assert "availableCustomers[0]?.id || null" not in opener
    assert "preferredCustomerId ? Number(preferredCustomerId) : null" in opener
    assert "activeCustomerOptions" not in opener


def test_editing_keeps_saved_customer_visible_even_if_no_longer_candidate() -> None:
    computed = _method_body("deliveryCustomerOptions")

    assert "this.deliveryForm.editingId" in computed
    assert "Number(row.id) === editingCustomerId" in computed
    assert "row.customer_code" in computed
    assert "candidate.pending_item_count" in computed
    assert "candidate.unordered_lot_count" in computed


def test_batch_picker_uses_fast_server_paging_with_clear_loading_state() -> None:
    loader = _method_body("loadDeliveryBatchItems")

    assert 'axios.get("/api/deliveries/pending-items/search"' in loader
    assert "list_all: true" not in loader
    assert "page: Math.max(1, Number(page || 1))" in loader
    assert "page_size: this.deliveryBatchPicker.page_size" in loader
    assert "正在载入待送订单，请稍候" in loader
