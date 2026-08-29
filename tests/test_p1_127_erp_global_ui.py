from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _visual_system() -> str:
    start = INDEX.index("/* P1-127: mature ERP visual system.")
    end = INDEX.index("</style>", start)
    return INDEX[start:end]


def test_enterprise_visual_system_is_scoped_without_changing_page_routing() -> None:
    assert "['app-shell', 'erp-enterprise-ui', `role-${user.role}`" in INDEX
    css = _visual_system()
    assert ".erp-enterprise-ui {" in css
    assert "body:has(.erp-enterprise-ui)" in css
    assert ".erp-enterprise-ui .topbar" in css
    assert ".erp-enterprise-ui .sidebar" in css
    assert ".erp-enterprise-ui .main" in css


def test_dense_erp_filters_tables_and_action_hierarchy_have_visual_contracts() -> None:
    css = _visual_system()
    for marker in (
        ".erp-enterprise-ui .toolbar",
        ".erp-enterprise-ui .filterbar",
        ".erp-enterprise-ui .list-filterbar",
        ".erp-enterprise-ui th",
        ".erp-enterprise-ui td",
        ".erp-enterprise-ui tbody tr:nth-child(even)",
        ".erp-enterprise-ui tbody tr:hover",
        ".erp-enterprise-ui .btn.primary",
        ".erp-enterprise-ui .btn.danger",
        "font-variant-numeric: tabular-nums",
    ):
        assert marker in css


def test_focus_large_type_and_reduced_motion_remain_first_class() -> None:
    css = _visual_system()
    assert ":focus-visible" in css
    assert ".erp-enterprise-ui.ui-large" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "@media (max-width: 1500px)" in css
