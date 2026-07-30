from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = ROOT / "docs" / "P1_21A_ROLE_MOBILE_AUDIT.md"
PROTOTYPE_PATH = (
    ROOT
    / "docs"
    / "prototypes"
    / "p1_21a_role_mobile"
    / "index.html"
)
AUDIT = AUDIT_PATH.read_text(encoding="utf-8")
PROTOTYPE = PROTOTYPE_PATH.read_text(encoding="utf-8")


def test_audit_is_grounded_on_the_live_base_and_single_head() -> None:
    assert "c251f54bada35ae046882d5fcf366acc7d163b51" in AUDIT
    assert "唯一 head `da83v8x9z72`" in AUDIT
    assert "尚未整合 Q1-04" in AUDIT
    assert "本轮没有 migration" in AUDIT

    for anchor in (
        "app/main.py",
        "app/api/deps.py::ROLE_DEFAULT_PERMISSIONS",
        "app/api/deliveries.py::list_delivery_pick_tasks",
        "app/api/warehouse.py::list_lots",
        "app/api/warehouse.py::floor3_product_candidates",
        "app/api/orders.py::download_order_item_drawing",
        "app/api/products.py::download_product_drawing",
        "app/api/production.py::get_production_tasks",
    ):
        assert anchor in AUDIT


def test_audit_does_not_overclaim_delivery_or_production_permissions() -> None:
    for fact in (
        "`delivery_picker`：默认只有 `orders.view` 和 `deliveries.pick`",
        "没有严格绑定“当前这个送货员”的 assignee 门禁",
        "`workshop`：当前默认有订单、来料、仓库和送货查看",
        "没有独立 `production.view`",
        "不能仅根据前端角色名称过滤",
    ):
        assert fact in AUDIT

    for boundary in (
        "客户范围先过滤",
        "直接 ID",
        "不泄露候选数",
        "同库位其他客户",
        "private, no-store",
        "不得授予全仓 `warehouse.view`",
    ):
        assert boundary in AUDIT


def test_prototype_has_three_role_paths_and_safe_return_contract() -> None:
    assert re.findall(r'data-role="([^"]+)"', PROTOTYPE) == [
        "admin",
        "delivery",
        "production",
    ]
    for contract in (
        "查产品和全部位置",
        "成品（只）",
        "半成品（张）",
        "当前尚无正式原料仓数据",
        "地图同时显示全部位置",
        "返回并保留之前状态",
        "我的待拿任务",
        "按位置拿货",
        "我的生产资料",
        "地图查看材料位置",
        "图纸与模具",
        "独立模具位置，不是仓库地图点",
    ):
        assert contract in PROTOTYPE

    assert "不自动正式发货" in PROTOTYPE
    assert "查看资料不等于确认完工" in PROTOTYPE


def test_prototype_visibly_marks_future_role_gates() -> None:
    for warning in (
        "尚未接入严格“本人任务”服务端门禁",
        "尚未接入最小 production.view 与本人任务门禁",
        "当前正式接口尚缺严格任务归属字段",
        "当前正式系统尚无独立 production.view 权限",
        "匿名目标原型",
    ):
        assert warning in PROTOTYPE


def test_prototype_covers_all_loading_failure_and_location_states() -> None:
    state_buttons = set(re.findall(r'data-demo-state="([^"]+)"', PROTOTYPE))
    assert state_buttons == {
        "normal",
        "loading",
        "empty",
        "error",
        "permission-denied",
        "stale",
        "no-location",
        "offline",
    }
    for contract in (
        "正在读取",
        "没有找到",
        "加载失败",
        "无权查看",
        "数据已过期",
        "有数量，暂无可绘制位置",
        "连接中断",
        "多候选必须人工选择",
        "演示更新时间",
        "刷新演示",
    ):
        assert contract in PROTOTYPE


def test_prototype_is_anonymous_offline_and_has_no_side_effect_channel() -> None:
    forbidden = (
        "fetch(",
        "XMLHttpRequest",
        "axios",
        "/api/",
        "localStorage",
        "sessionStorage",
        "indexedDB",
        "WebSocket",
        "serviceWorker",
        "<form",
        'type="file"',
        "http://",
        "https://",
    )
    for token in forbidden:
        assert token not in PROTOTYPE

    for boundary in (
        "匿名离线原型",
        "不连接 ERP、不读取真实客户、不保存任何操作",
        "没有真实登录态、没有网络请求、没有本地持久存储、没有业务提交",
        "原型不执行",
    ):
        assert boundary in PROTOTYPE


def test_mobile_width_guards_and_inline_javascript_are_valid() -> None:
    assert 'name="viewport"' in PROTOTYPE
    assert "width: min(100%, 430px)" in PROTOTYPE
    assert "overflow-x: hidden" in PROTOTYPE
    assert "@media (max-width: 359px)" in PROTOTYPE
    assert "min-width: 0" in PROTOTYPE

    node = shutil.which("node")
    assert node, "Node.js is required for inline JavaScript syntax checks"
    scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>",
            PROTOTYPE,
            flags=re.DOTALL,
        )
        if script.strip()
    ]
    assert scripts
    for script in scripts:
        result = subprocess.run(
            [node, "--check"],
            input=script,
            text=True,
            encoding="utf-8",
            capture_output=True,
            env=os.environ.copy(),
            check=False,
        )
        assert result.returncode == 0, result.stderr
