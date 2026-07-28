from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_valid_production_destination_is_auto_selected_but_still_requires_submit() -> None:
    assert "选择有效的完工去向后系统会自动勾选该行" in INDEX
    assert "只有点击上方“批量确认完工”并确认成功，才算保存并进入待送货" in INDEX
    assert '@change="ensureProductionMode(row)"' in INDEX
    assert '@change="autoSelectProductionRow(row)"' in INDEX
    assert "已加入本次确认；请点击上方“批量确认完工”保存" in INDEX
    assert "尚未保存：请补全有效数量/库位，或先完成当前客户的确认" in INDEX

    ensure_mode = re.search(
        r"ensureProductionMode\(row\) \{(.*?)\n\s+\},\n\s+autoSelectProductionRow",
        INDEX,
        re.DOTALL,
    )
    assert ensure_mode is not None
    assert "this.autoSelectProductionRow(row);" in ensure_mode.group(1)

    auto_select = re.search(
        r"autoSelectProductionRow\(row\) \{(.*?)\n\s+\},\n\s+productionSelectedCount",
        INDEX,
        re.DOTALL,
    )
    assert auto_select is not None
    body = auto_select.group(1)
    assert "if (!this.canConfirmProductionRow(row))" in body
    assert "Number(row.customer_id) !== this.productionSelectedCustomerId" in body
    assert "this.productionSelected[row.id] = true;" in body


def test_delivery_gate_still_requires_persisted_production_completion() -> None:
    delivery_source = (ROOT / "app" / "api" / "deliveries.py").read_text(
        encoding="utf-8"
    )
    assert 'if task.status not in {"completed", "not_required"}:' in delivery_source
    assert "production_ready_quantity(db, order_item)" in delivery_source


def test_double_splice_uses_pieces_per_box_for_finished_quantity() -> None:
    assert "productionTheoreticalOutput(row)" in INDEX
    assert "Math.floor(input * factor / piecesPerBox)" in INDEX
    assert "const piecesPerBox = Math.max(Number(row.pieces_per_box || 1), 1);" in INDEX
    assert (
        "return Math.max(this.productionTheoreticalOutput(row) - "
        "Number(row.actual_output_quantity || 0), 0);"
    ) in INDEX

    supplement = re.search(
        r"async supplementProductionCompletion\(row\) \{(.*?)\n\s+\},\n"
        r"\s+openProductionInventory",
        INDEX,
        re.DOTALL,
    )
    assert supplement is not None
    assert "this.productionTheoreticalOutput({" in supplement.group(1)
    assert "actual_input_quantity: input" in supplement.group(1)
