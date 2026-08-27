from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method(name: str, next_name: str) -> str:
    return INDEX.split(f"          {name}(", 1)[1].split(
        f"          {next_name}(", 1
    )[0]


def test_history_shows_only_current_location_or_non_clickable_delivery_state() -> None:
    history = INDEX.split('<table class="production-history-table"', 1)[1].split(
        "</table>", 1
    )[0]
    assert ">当前库位</th>" in history
    assert "完工处理 / 当前库位" not in history
    assert "row.current_inventory_status==='located'" in INDEX
    assert "row.current_warehouse_location_name" in INDEX
    assert 'row.is_fully_delivered" class="history-line history-delivered">已送完' in history
    assert "completion_warehouse_location_name" not in history


def test_direct_completion_label_is_not_a_hard_coded_physical_floor() -> None:
    history = INDEX.split('<table class="production-history-table"', 1)[1].split(
        "</table>", 1
    )[0]
    assert "订单内直接待送" not in history
    assert "一楼待送区" not in history


def test_inventory_navigation_prefers_current_location_over_completion_snapshot() -> None:
    method = _method("openProductionInventory", "revertProductionCompletion")
    assert "row.current_warehouse_location_id" in method
    assert 'tab:"locations", view:"2d"' in method
    assert 'window.open(`/warehouse.html?' in method
    assert 'row.is_fully_delivered !== true' in method
