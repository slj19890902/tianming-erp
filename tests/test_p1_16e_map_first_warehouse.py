from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")


def test_warehouse_daily_navigation_defaults_to_measured_twin_floor() -> None:
    tabs = WAREHOUSE[
        WAREHOUSE.index('<div class="tabs">') : WAREHOUSE.index(
            '<div id="pageError"'
        )
    ]
    assert tabs.index('data-tab="locations"') < tabs.index(
        'data-tab="insights"'
    )
    assert '<button class="btn active" data-tab="locations" aria-label="数字孪生库位管理">数字孪生库位</button>' in tabs
    assert 'tab:"locations"' in WAREHOUSE
    assert 'if(!requestedTab&&!locationId&&!lotId&&!keyword){await switchTab("locations");return}' in WAREHOUSE
    assert 'if(requestedTab==="locations"&&!locationId){' in WAREHOUSE
    assert 'await switchTab("locations");' in WAREHOUSE
    assert 'locationView:"floor3"' in WAREHOUSE
    assert 'requestedLocationView==="floor3"' in WAREHOUSE


def test_row_ledgers_are_admin_advanced_entries_not_daily_tabs() -> None:
    assert 'class="btn admin-only inventory-ledger-tab" data-tab="finished">高级成品台账' in WAREHOUSE
    assert 'class="btn admin-only inventory-ledger-tab" data-tab="semi_finished">高级半成品台账' in WAREHOUSE
    assert 'onclick="switchTab(\'finished\')">高级库存台账' in WAREHOUSE
    assert 'onclick="switchLocationView(\'ledger\')">库位结构维护' in WAREHOUSE
    assert 'state.user.role!=="admin"||state.readOnly' in WAREHOUSE
    assert 'document.querySelectorAll(".admin-only")' in WAREHOUSE


def test_map_first_change_does_not_remove_authoritative_ledgers_or_actions() -> None:
    for marker in (
        'id="inventorySection"',
        'id="lotTable"',
        'id="movementSection"',
        'id="locationSection"',
        'id="floor3LocationSection"',
        'async function switchLocationView(view)',
        'async function switchTab(tab)',
        '/api/warehouse/lots',
        '/api/warehouse/floor3/locations',
    ):
        assert marker in WAREHOUSE
    assert "inventory_lots" not in WAREHOUSE.lower()


def test_warehouse_inline_script_remains_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for frontend syntax validation"
    scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>", WAREHOUSE, re.DOTALL
        )
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "p1-16e-map-first-warehouse.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
