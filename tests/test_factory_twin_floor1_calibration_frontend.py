from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "factory_twin/frontend/src/App.tsx").read_text(encoding="utf-8")
STYLES = (ROOT / "factory_twin/frontend/src/styles.css").read_text(encoding="utf-8")


def test_floor1_calibration_has_direct_structure_and_equipment_worklists() -> None:
    assert "一楼现场校正" in APP
    assert 'Floor1CalibrationKind = "structure" | "equipment"' in APP
    assert "floor1EditableStructures.map" in APP
    assert "floor1EditableEquipment.map" in APP
    assert "搜索一楼校正对象" in APP
    assert "墙 / 门 / 窗" in APP
    assert "先解锁" in APP


def test_floor1_calibration_exposes_safe_actions_without_unlocking_anchors() -> None:
    assert "删除这段结构" in APP
    assert "解除锁定并调整" in APP
    assert "调整完成，确认并锁定设备" in APP
    assert "柱子/货梯定位基准不可修改" in APP
    assert '"custom_wall", "rolling_door", "custom_window"' in APP
    assert '"custom_column", "freight_elevator"' in APP
    assert "selectedFloor1EditableStructure" in APP
    assert ".floor1-actionbar" in STYLES
    assert ".floor1-calibration-panel" in STYLES
