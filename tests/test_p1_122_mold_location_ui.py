from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_mold_tool_workflow import _login, mold_app
from test_p1_111_mold_location_filters import _seed_filter_matrix


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("1F-M-R01-L2-G01", "一楼，左架，第2层、第1格"),
        ("1F-M-R02-L2-G03", "一楼，中架，第2层、第3格"),
        ("1F-M-R03-L1", "一楼，右架，第1层"),
    ],
)
def test_mold_location_employee_names_are_short_and_do_not_change_codes(
    location: str,
    expected: str,
) -> None:
    from app.services.mold_location import describe_mold_location

    guide = describe_mold_location(location)

    assert guide["location_code"] == location
    assert guide["prompt"] == expected
    assert "前往" not in guide["prompt"]
    assert "模具001" not in guide["prompt"]
    assert "模具002" not in guide["prompt"]


def test_mold_status_attention_sort_is_server_side_and_unprinted_first(
    mold_app,
) -> None:
    app, factory = mold_app
    seeded = _seed_filter_matrix(factory)

    with TestClient(app) as client:
        _login(client, "admin")
        response = client.get(
            "/api/warehouse/molds",
            params={
                "page": 1,
                "page_size": 100,
                "include_unprinted": True,
                "include_repair": True,
                "include_inactive": True,
                "include_archived": True,
                "sort_by": "status_attention",
            },
        )

    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["items"]] == [
        seeded["unprinted"],
        seeded["repair"],
        seeded["archived"],
        seeded["printed"],
        seeded["other"],
    ]


def test_mold_daily_ui_is_query_first_and_removes_static_explanation_blocks() -> None:
    root = Path(__file__).resolve().parents[1]
    warehouse = (root / "static" / "warehouse.html").read_text(encoding="utf-8")
    mold_section = warehouse.split('<section id="moldSection"', 1)[1].split(
        '<section id="printingPlateSection"', 1
    )[0]
    all_html = "\n".join(
        path.read_text(encoding="utf-8") for path in (root / "static").glob("*.html")
    )

    assert 'id="moldForm" class="panel admin-only hidden"' in mold_section
    assert 'id="openMoldCreate"' in mold_section
    assert "function openMoldCreateForm()" in warehouse
    assert 'id="moldStatusSort"' in mold_section
    assert "status_attention" in warehouse
    assert "notice info" not in mold_section
    assert "moldFormModeHint" not in mold_section
    assert '<div id="moldCustomerCandidates" class="floor3-candidates"></div>' in mold_section
    assert '<div id="moldBindingCustomerCandidates" class="floor3-candidates"></div>' in mold_section
    assert "默认只显示已打印、已启用且无需维修" not in mold_section
    assert "模具封存待复用：" not in all_html
    assert "员工标签示例会在选择主客户后显示" not in all_html


def test_mold_location_display_alias_is_used_by_all_employee_channels() -> None:
    root = Path(__file__).resolve().parents[1]
    files = {
        name: (root / "static" / name).read_text(encoding="utf-8")
        for name in (
            "index.html",
            "warehouse.html",
            "mobile_erp.html",
            "mobile_mold_lookup.html",
            "mobile_mold_live.html",
            "mobile_product_live.html",
            "requisition-production-print.html",
        )
    }
    map_source = (root / "factory_twin" / "frontend" / "src" / "WarehouseTwinApp.tsx").read_text(
        encoding="utf-8"
    )

    assert "item.mold_location_display || item.mold_location" in files["index.html"]
    assert "readableAssetLocation(row)" in files["warehouse.html"]
    assert "task.mold_location_display || task.mold_location" in files["mobile_erp.html"]
    assert "row.location_guide?.prompt||row.rack_location" in files["mobile_mold_lookup.html"]
    assert "row.mold_location_display||row.mold_location" in files["mobile_mold_live.html"]
    assert "mold.current_location_display||mold.current_location" in files["mobile_product_live.html"]
    assert "line.mold_location_display || line.mold_location" in files[
        "requisition-production-print.html"
    ]
    assert "moldRackEmployeeName(rack)" in map_source


def test_source_map_facts_keep_original_mold_rack_names() -> None:
    root = Path(__file__).resolve().parents[1]
    source_map = (root / "static" / "factory_maps" / "twin_layout_v1.json").read_text(
        encoding="utf-8"
    )

    assert "R01 左架（模具002，小模切机上方）" in source_map
    assert "R02 中架（模具002，中模切机上方）" in source_map
    assert "R03 右架（模具001）" in source_map
