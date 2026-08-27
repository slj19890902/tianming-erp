from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method(name: str, next_name: str) -> str:
    return INDEX.split(f"          {name}(", 1)[1].split(
        f"          {next_name}(", 1
    )[0]


def test_history_separates_completion_disposition_from_current_location() -> None:
    assert "完工处理 / 当前库位" in INDEX
    assert "row.current_inventory_status==='located'" in INDEX
    assert "row.current_warehouse_location_name" in INDEX
    assert "已送完 / 无当前库存" in INDEX
    assert "完工时：" in INDEX


def test_direct_completion_label_is_not_a_hard_coded_physical_floor() -> None:
    method = _method("productionDestinationLabel", "productionCompletionRequestItems")
    assert 'return "订单内直接待送"' in method
    assert "一楼待送区" not in method


def test_inventory_navigation_prefers_current_location_over_completion_snapshot() -> None:
    method = _method("openProductionInventory", "revertProductionCompletion")
    assert "row.current_warehouse_location_id" in method
    assert "row?.current_inventory_status" in method
