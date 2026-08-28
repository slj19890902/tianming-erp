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
        "/api/deliveries/${deliveryId}/pick-task",
        "/mobile/delivery-pick.html?task_id=",
        "拿货",
        "发货打印",
        "待推送拿货",
        "存在数量异常/没货",
        "可发货打印",
    ):
        assert marker in INDEX


def test_desktop_delivery_row_owns_pick_actions_without_duplicate_panel() -> None:
    action_position = INDEX.index("runDeliveryPrimaryRowAction(row)")
    row_start = INDEX.rfind("<tr", 0, action_position)
    row_end = INDEX.index("</tr>", action_position)
    assert row_start >= 0
    delivery_row = INDEX[row_start:row_end]
    for marker in (
        "openDeliveryPickTask(row)",
        "dispatchDelivery(row)",
        "拿货",
        "打开手机拿货",
        "发货打印",
    ):
        assert marker in delivery_row
    primary = INDEX[INDEX.index("runDeliveryPrimaryRowAction(row) {") : INDEX.index("deliveryHasSecondaryRowActions(row) {")]
    assert "this.createDeliveryPickTask(row)" in primary
    assert "delivery-pick-task-panel" not in INDEX
    assert "司机拿货任务" not in INDEX


def test_exception_dispatch_applies_pick_result_before_dispatch() -> None:
    exception_confirm_pos = INDEX.index("司机拿货结果存在异常")
    final_confirm_pos = INDEX.index("confirm(`确认发货并打印", exception_confirm_pos)
    apply_pos = INDEX.index("await this.applyDeliveryPickTask(frozenRow)", final_confirm_pos)
    dispatch_pos = INDEX.index("await axios.put(`/api/deliveries/${deliveryId}/dispatch`)", apply_pos)
    assert exception_confirm_pos < final_confirm_pos < apply_pos < dispatch_pos
    assert "task.has_exception" in INDEX
    assert 'task.status === "exception"' in INDEX
    assert "/api/delivery-picks/${task.id}/apply" in INDEX


def test_mobile_page_uses_cookie_auth_and_requested_api_paths() -> None:
    assert 'credentials:"include"' in MOBILE
    assert 'request("/api/auth/me")' in MOBILE
    assert 'request("/api/auth/login"' in MOBILE
    assert "/api/delivery-picks/${encodeURIComponent(selectedId)}" in MOBILE
    assert "/api/delivery-picks/${encodeURIComponent(currentTask.id)}/items/" in MOBILE
    assert "/items/${encodeURIComponent(id)}`" in MOBILE
    assert "/submit`" in MOBILE


def test_mobile_page_without_task_id_lists_and_selects_pending_tasks() -> None:
    assert "let taskId =" in MOBILE
    assert "if(taskId)" in MOBILE and "else await loadTaskList()" in MOBILE
    assert "/api/delivery-picks?" in MOBILE
    assert "response_mode=summary" in MOBILE
    assert "include_dispatched=false" in MOBILE
    assert "page_size=${listPageSize}" in MOBILE
    assert "const listPageSize=20" in MOBILE
    assert "正在读取待拿货任务" in MOBILE
    for marker in ("loadTaskList", "selectTask", "taskChooser", "customer_name"):
        assert marker in MOBILE
    assert "/api/delivery-picks/${encodeURIComponent(selectedId)}" in MOBILE


def test_desktop_pick_status_maps_driver_confirmed_to_ready_to_dispatch() -> None:
    assert 'driver_confirmed: "可发货打印"' in INDEX
    assert '"driver_confirmed"' in INDEX


def test_mobile_page_hides_business_fields_and_keeps_pick_workflow() -> None:
    for marker in ("product_name", "specification", "product_code", "拿齐", "少拿", "未找到", "提交异常结果", "本单全部按计划拿齐", "customer_name"):
        assert marker in MOBILE
    for forbidden_label in ("客户单号", "内部备注", "单价", "财务"):
        assert forbidden_label not in MOBILE
    assert 'activeFilter = "open"' in MOBILE
    assert '["picked","拿齐"' in MOBILE


def test_n083_mobile_page_is_location_first_and_keeps_exceptions_collapsed() -> None:
    for marker in (
        "location_groups",
        "renderLocationGroup",
        "group.label",
        "group.employee_location_name",
        "group.current_address_name",
        "group.needs_relocation",
        "有缺货或数量不一致时再登记",
        "/complete-planned",
        "不会自动发货",
    ):
        assert marker in MOBILE
    assert '<details id="exceptionTools"' in MOBILE


def test_p1_21c_assignment_and_task_scoped_map_are_exposed_without_warehouse_page() -> None:
    for marker in (
        "/api/delivery-picks/assignees",
        "assignDeliveryPickTask(row",
        "picker_user_id",
        "未分配",
    ):
        assert marker in INDEX
    for marker in (
        "map_status",
        "measured-map/floors",
        "feature.points",
        "recommended_sequence",
        "地图定位此位置",
        "未建立实测地图",
        "返回拿货任务",
        "taskScrollY",
        "openTaskMap",
        "closeTaskMap",
    ):
        assert marker in MOBILE
    assert "/warehouse.html" not in MOBILE


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
