from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_legacy_direct_destination_returns_to_finished_history() -> None:
    assert "直接待送：选择后整批进入一楼待送区" not in INDEX
    assert "订单内直接待送" in INDEX
    assert 'class="production-compact-command-bar"' in INDEX
    assert 'direct_delivery_quantity: row.completion_mode === "direct" ? Number(row.actual_output_quantity) : 0' in INDEX
    assert '@change="ensureProductionMode(row)"' in INDEX
    ensure_mode = re.search(
        r"async ensureProductionMode\(row\) \{(.*?)\n\s+\},\n"
        r"\s+async onProductionLocationSelection",
        INDEX,
        re.DOTALL,
    )
    assert ensure_mode is not None
    assert 'if (row.completion_mode !== "direct") return;' in ensure_mode.group(1)
    assert "await this.confirmProductionDirectRow(row);" in ensure_mode.group(1)

    direct_submit = re.search(
        r"async confirmProductionDirectRow\(row\) \{(.*?)\n\s+\},\n"
        r"\s+autoSelectProductionRow",
        INDEX,
        re.DOTALL,
    )
    assert direct_submit is not None
    body = direct_submit.group(1)
    assert 'axios.post("/api/production/completion-batches"' in body
    assert "this.productionDirectAttempts[row.id]" in body
    assert "this.productionBusy = true;" in body
    assert "row.completion_mode = \"\";" in body
    assert "this.loadDeliveries()" in body
    assert "直接待送已保存，但页面刷新失败" in body
    assert body.index('axios.post("/api/production/completion-batches"') < body.index(
        "this.loadProduction()"
    )
    assert 'this.productionTab = "pending";' not in body
    assert 'this.productionTab = "history";' in body
    assert "重试会复用同一幂等键" in body


def test_production_confirmation_keeps_destination_and_history_actions_compact() -> None:
    history = INDEX.split('<table class="production-history-table"', 1)[1].split(
        "</table>", 1
    )[0]
    assert 'class="production-history-action-menu"' not in history
    assert ">撤销</button>" in history
    assert "去送货" not in history
    assert "转入成品库存" not in history
    assert ".ui-large .production-table .input" in INDEX
    assert "min-height:32px" in INDEX


def test_stock_location_only_selects_locally_then_customer_groups_are_posted() -> None:
    assert "全部入库：选好库位，再点顶部“批量确认入库”" not in INDEX
    assert "合格品全部入库" in INDEX
    assert "`批量确认入库（${productionSelectedCount()}）`" not in INDEX
    assert "待送成品归位" in INDEX
    assert '@change="onProductionLocationSelection(row)"' in INDEX
    assert "确认入库位置" not in INDEX

    local_selection = re.search(
        r"async onProductionLocationSelection\(row\) \{(.*?)\n\s+\},\n"
        r"\s+async confirmProductionDirectRow",
        INDEX,
        re.DOTALL,
    )
    assert local_selection is not None
    local_body = local_selection.group(1)
    assert "this.autoSelectProductionRow(row)" in local_body
    assert "axios." not in local_body
    assert "/api/" not in local_body

    batch_submit = re.search(
        r"async batchConfirmProduction\(\) \{(.*?)\n\s+\},\n"
        r"\s+async transferProductionCompletionToStock",
        INDEX,
        re.DOTALL,
    )
    assert batch_submit is not None
    batch_body = batch_submit.group(1)
    assert "const groups = new Map();" in batch_body
    assert "for (const [customerId, groupRows] of groups.entries())" in batch_body
    assert "this.productionCompletionAttempts[customerId]" in batch_body
    assert 'axios.post("/api/production/completion-batches"' in batch_body
    assert "confirm(" not in batch_body
    assert "delete this.productionCompletionAttempts[customerId]" in batch_body
    assert "delete this.productionSelected[row.id]" in batch_body
    assert "this.loadDeliveries()" in batch_body
    assert "失败项已保留，可直接重试" in batch_body
    assert 'this.productionTab = "pending";' not in batch_body
    assert 'this.productionTab = "history";' in batch_body
    assert "一次只能确认同一客户" not in INDEX
    assert "当前已选择其他客户" not in INDEX

    service_source = (
        ROOT / "app" / "services" / "production_workflow.py"
    ).read_text(encoding="utf-8")
    assert "一个完工批次只能包含同一客户的生产任务" in service_source


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
