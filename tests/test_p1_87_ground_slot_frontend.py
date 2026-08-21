from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "factory_twin" / "frontend" / "src" / "WarehouseTwinApp.tsx"
TYPES = ROOT / "factory_twin" / "frontend" / "src" / "types.ts"
SCENE = ROOT / "factory_twin" / "frontend" / "src" / "industrialScene.ts"
STYLE = ROOT / "factory_twin" / "frontend" / "src" / "styles.css"
BUILT = ROOT / "static" / "factory-twin-assets"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _function_block(source: str, start: str, end: str) -> str:
    return source[source.index(start) : source.index(end, source.index(start))]


def test_ground_layout_preview_publish_and_standard_slot_contract() -> None:
    source = _source(APP)
    assert "/ground-layout/floors/${encodeURIComponent(floorCode)}/areas/${encodeURIComponent(selectedAreaCode)}/draft" in source
    assert "/ground-layout/floors/${encodeURIComponent(floorCode)}/areas/${encodeURIComponent(selectedAreaCode)}/publish" in source
    for token in (
        "numbering_origin",
        "row_direction",
        "slot_direction",
        "row_start_no",
        "slot_start_no",
        "expected_policy_version",
        "expected_map_revision",
        "preview_fingerprint",
    ):
        assert token in source
    assert "标准位置 1200×1000mm" in source
    assert "保存并生成预览" in source
    assert "发布当前预览" in source
    draft = _function_block(source, "const saveGroundLayoutDraft", "const publishGroundLayout")
    publish = _function_block(source, "const publishGroundLayout", "const loadGroundStorageCandidates")
    assert "window.confirm" not in draft
    assert "window.confirm" not in publish


def test_employee_map_picker_has_four_states_two_slot_and_one_save() -> None:
    source = _source(APP)
    assert '"/api/warehouse/ground-storage/finished-inbound"' in source
    assert "/api/warehouse/ground-storage/lots/${groundTransferSource.lot_id}/transfer" in source
    assert "/api/warehouse/ground-storage/candidates?" in source
    for field in (
        "expected_layout_version",
        "secondary_location_id",
        "expected_secondary_layout_version",
        "expected_lot_version",
        "capacity_quantity",
        "idempotency_key",
    ):
        assert field in source
    for label in (
        "绿色：空位",
        "蓝色：同款可共位",
        "灰色：不可用/容量不足",
        "红色：冲突",
        "大型货物，占用两个相邻位置（库存数量只记一次）",
        "保存到当前中文位置",
        "已取消页面选择；库存零写入。",
    ):
        assert label in source
    save = _function_block(source, "const saveGroundStorage", "const chooseProductionTask")
    assert "window.confirm" not in save
    assert "pallet_id" not in re.sub(r"groundTransferSource\.pallet_id", "", save)


def test_candidate_colors_reach_map_and_built_bundle() -> None:
    types = _source(TYPES)
    scene = _source(SCENE)
    app = _source(APP)
    styles = _source(STYLE)
    assert "candidate_status_color?: string" in types
    assert "pallet.candidate_status_color || state.color" in scene
    for color in ("#16a34a", "#2563eb", "#64748b", "#dc2626"):
        assert color in app
    assert ".twin-ground-legend .green" in styles

    html = _source(BUILT / "warehouse-twin.html")
    assets = re.findall(r'(?:src|href)="([^"]+)"', html)
    local_assets = [
        BUILT / value.removeprefix("/factory-twin-assets/").removeprefix("./")
        for value in assets
        if value.startswith(("./", "/factory-twin-assets/"))
    ]
    assert local_assets
    assert all(path.is_file() for path in local_assets)
    bundled_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in local_assets
        if path.suffix in {".js", ".css"}
    )
    assert "地图点选成品存放" in bundled_text
    assert "保存到当前中文位置" in bundled_text
