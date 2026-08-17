from __future__ import annotations

from pathlib import Path
import re

from app.services.warehouse_floor1_candidate_planner import (
    build_floor1_formal_candidate_plan,
)
from app.services.warehouse_pallet_standard import standard_pallet_contract
from app.services.warehouse_twin_layout import load_warehouse_twin_floor


ROOT = Path(__file__).resolve().parents[1]
INVENTORY_SOURCE = (
    ROOT / "factory_twin" / "frontend" / "src" / "warehouseInventory.mjs"
).read_text(encoding="utf-8")
TWIN_SOURCE = (
    ROOT / "factory_twin" / "frontend" / "src" / "WarehouseTwinApp.tsx"
).read_text(encoding="utf-8")
CANVAS_SOURCE = (
    ROOT / "factory_twin" / "frontend" / "src" / "EditorCanvas.tsx"
).read_text(encoding="utf-8")
SCENE_SOURCE = (
    ROOT / "factory_twin" / "frontend" / "src" / "industrialScene.ts"
).read_text(encoding="utf-8")
PLANNER_SOURCE = (
    ROOT / "app" / "services" / "warehouse_floor1_candidate_planner.py"
).read_text(encoding="utf-8")


def test_backend_has_one_canonical_standard_and_planner_consumes_it() -> None:
    assert standard_pallet_contract() == {
        "contract_version": "standard-pallet-v1",
        "width_mm": 1200,
        "depth_mm": 1000,
        "height_mm": 150,
    }
    assert "STANDARD_PALLET_WIDTH_MM = 1200" not in PLANNER_SOURCE
    assert "STANDARD_PALLET_DEPTH_MM = 1000" not in PLANNER_SOURCE

    plan = build_floor1_formal_candidate_plan(load_warehouse_twin_floor("1F"))
    assert plan["standard_pallet_mm"] == {"width": 1200, "depth": 1000}


def test_operational_frontend_requires_backend_contract_without_local_fallback() -> None:
    assert "normalizeStandardPalletContract" in INVENTORY_SOURCE
    assert "standardPalletContractsMatch" in TWIN_SOURCE
    assert "标准栈板尺寸合同缺失或前后端不一致" in TWIN_SOURCE
    assert "width_mm: 1200" not in INVENTORY_SOURCE
    assert "depth_mm: 1000" not in INVENTORY_SOURCE
    assert "height_mm: 160" not in INVENTORY_SOURCE
    assert "pallets: movePreviewPallets" in TWIN_SOURCE
    assert "pallets: [...layout.pallets" not in TWIN_SOURCE


def test_logical_anchor_is_not_rendered_as_a_tiny_wooden_pallet() -> None:
    assert "if (pallet.is_logical_anchor)" in SCENE_SOURCE
    assert "CylinderGeometry" in SCENE_SOURCE
    assert "warehouseTheme && !pallet.is_logical_anchor" in CANVAS_SOURCE
    assert "buildPalletMarkerVisual(pallet, viewMode, violated)" in CANVAS_SOURCE


def test_built_warehouse_map_keeps_the_same_fail_closed_contract() -> None:
    html_path = ROOT / "static" / "factory-twin-assets" / "warehouse-twin.html"
    html = html_path.read_text(encoding="utf-8")
    references = re.findall(r'/factory-twin-assets/([^"\']+)', html)
    assert references
    for relative in references:
        assert (ROOT / "static" / "factory-twin-assets" / relative).is_file()

    script_reference = next(
        relative for relative in references if relative.startswith("assets/warehouseTwin-")
    )
    script = (
        ROOT / "static" / "factory-twin-assets" / script_reference
    ).read_text(encoding="utf-8")
    assert "standard-pallet-v1" in script
    assert "标准栈板尺寸合同缺失或前后端不一致" in script
    assert "height_mm:160" not in script
