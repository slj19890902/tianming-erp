from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
MOBILE = (ROOT / "static" / "mobile_delivery_pick.html").read_text(encoding="utf-8")


def test_desktop_delivery_list_exposes_pick_task_contract_and_actions() -> None:
    for marker in (
        "/api/delivery-picks",
        "/api/deliveries/${row.id}/pick-task",
        "/mobile/delivery-pick.html?task_id=",
        "拿货",
        "发货打印",
        "待推送拿货",
        "存在数量异常/没货",
        "可发货打印",
    ):
        assert marker in INDEX


def test_desktop_delivery_row_owns_pick_actions_without_duplicate_panel() -> None:
    row_match = re.search(
        r'<tbody>\s*<tr v-for="row in deliveries" :key="row.id".*?</tr>\s*</tbody>',
        INDEX,
        re.DOTALL,
    )
    assert row_match is not None
    delivery_row = row_match.group(0)
    for marker in (
        "createDeliveryPickTask(row)",
        "openDeliveryPickTask(row)",
        "dispatchDelivery(row)",
        "拿货",
        "打开手机拿货",
        "发货打印",
    ):
        assert marker in delivery_row
    assert "delivery-pick-task-panel" not in INDEX
    assert "司机拿货任务" not in INDEX


def test_exception_dispatch_applies_pick_result_before_dispatch() -> None:
    exception_confirm_pos = INDEX.index("司机拿货结果存在异常")
    apply_pos = INDEX.index("await this.applyDeliveryPickTask(row)")
    final_confirm_pos = INDEX.index("confirm(`确认发货并打印", apply_pos)
    dispatch_pos = INDEX.index("await axios.put(`/api/deliveries/${row.id}/dispatch`)")
    assert exception_confirm_pos < apply_pos < final_confirm_pos < dispatch_pos
    assert "task.has_exception" in INDEX
    assert 'task.status === "exception"' in INDEX
    assert "/api/delivery-picks/${task.id}/apply" in INDEX


def test_mobile_page_uses_cookie_auth_and_requested_api_paths() -> None:
    assert 'credentials:"include"' in MOBILE
    assert 'request("/api/auth/me")' in MOBILE
    assert 'request("/api/auth/login"' in MOBILE
    assert "/api/delivery-picks/${encodeURIComponent(taskId)}" in MOBILE
    assert "/api/delivery-picks/${encodeURIComponent(task.id)}/items/" in MOBILE
    assert "/items/${encodeURIComponent(itemId)}`" in MOBILE
    assert "/submit`" in MOBILE


def test_mobile_page_without_task_id_lists_and_selects_pending_tasks() -> None:
    assert "let taskId =" in MOBILE
    assert "if(taskId)" in MOBILE and "else await loadTaskList()" in MOBILE
    assert 'request("/api/delivery-picks"' in MOBILE
    for marker in ("loadTaskList", "selectTask", "taskChooser", "customer_name"):
        assert marker in MOBILE
    assert "/api/delivery-picks/${encodeURIComponent(taskId)}" in MOBILE


def test_desktop_pick_status_maps_driver_confirmed_to_ready_to_dispatch() -> None:
    assert 'driver_confirmed: "可发货打印"' in INDEX
    assert '"driver_confirmed"' in INDEX


def test_mobile_page_hides_business_fields_and_keeps_pick_workflow() -> None:
    for marker in ("product_name", "specification", "product_code", "已拿货", "部分拿货", "没货", "提交异常结果", "本单全部按计划拿齐", "customer_name"):
        assert marker in MOBILE
    for forbidden_label in ("客户单号", "内部备注", "单价", "财务"):
        assert forbidden_label not in MOBILE
    assert 'activeFilter = "open"' in MOBILE
    assert '["picked","已拿货"' in MOBILE


def test_n083_mobile_page_is_location_first_and_keeps_exceptions_collapsed() -> None:
    for marker in (
        "location_groups",
        "renderLocationGroup",
        "pallet_code",
        "group.label",
        "group.needs_relocation",
        "有缺货或数量不一致时再登记",
        "/complete-planned",
        "不会自动发货",
    ):
        assert marker in MOBILE
    assert '<details id="exceptionTools"' in MOBILE


def test_n036_inline_javascript_is_syntactically_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        raise AssertionError("Node.js is required for the frontend contract test")
    for name, source in (("index", INDEX), ("mobile_delivery_pick", MOBILE)):
        scripts = [script for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", source, re.DOTALL) if script.strip()]
        target = tmp_path / f"{name}.js"
        target.write_text("\n".join(scripts), encoding="utf-8")
        result = subprocess.run([node, "--check", str(target)], capture_output=True, text=True, encoding="utf-8")
        assert result.returncode == 0, result.stderr
