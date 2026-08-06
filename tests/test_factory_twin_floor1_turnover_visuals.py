from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "factory_twin" / "frontend" / "src"


def test_floor1_turnover_visual_contract_is_explicit_and_isolated() -> None:
    app = (FRONTEND / "App.tsx").read_text(encoding="utf-8")
    canvas = (FRONTEND / "EditorCanvas.tsx").read_text(encoding="utf-8")
    scene = (FRONTEND / "industrialScene.ts").read_text(encoding="utf-8")
    status = (FRONTEND / "palletStatus.ts").read_text(encoding="utf-8")
    seed = (ROOT / "factory_twin" / "scripts" / "seed_floor1_turnover_pallets.py").read_text(encoding="utf-8")

    for label in ["空栈板", "待生产", "生产周转中", "完工待转运", "异常暂存"]:
        assert label in status
    assert "palletStatusInfo(pallet.visual_status)" in canvas
    assert "palletStatusInfo(pallet.visual_status)" in scene
    assert "隔离模拟数据 · 不绑定正式库存" in app
    assert "不生成正式库位，也不代表库存数量" in app
    assert "temporary_turnover" in app
    assert "formal_erp_inventory_touched" in seed
    assert "is_simulated=True" in seed
    for forbidden in ["InventoryLot", "inventory_lots", "production_orders", "order_items"]:
        assert forbidden not in seed
