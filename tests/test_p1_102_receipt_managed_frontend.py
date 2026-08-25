from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DESKTOP_SOURCE = (PROJECT_ROOT / "static" / "index.html").read_text(
    encoding="utf-8"
)
MOBILE_SOURCE = (PROJECT_ROOT / "static" / "mobile_erp.html").read_text(
    encoding="utf-8"
)
INCOMING_SOURCE = (PROJECT_ROOT / "static" / "incoming.html").read_text(
    encoding="utf-8"
)


def test_desktop_preserves_zero_and_receipt_auto_output_without_manual_controls() -> None:
    assert "row.completion_actionable === false" in DESKTOP_SOURCE
    assert "Number(row.available_material_input_quantity ?? 0)" in DESKTOP_SOURCE
    assert "? Number(row.actual_output_quantity ?? 0)" in DESKTOP_SOURCE
    assert "row.completion_block_message || '该任务由收料用途自动推进" in DESKTOP_SOURCE
    assert "成品由收料流水自动进入真实 FIN 成员位置" in DESKTOP_SOURCE


def test_mobile_and_incoming_show_server_built_receipt_auto_progress() -> None:
    assert 'task.receipt_purpose_managed ? "已自动形成"' in MOBILE_SOURCE
    assert "task.completion_block_message" in MOBILE_SOURCE
    assert 'task.receipt_purpose_managed ? "已自动形成"' in INCOMING_SOURCE
    assert "task.completion_block_message" in INCOMING_SOURCE
