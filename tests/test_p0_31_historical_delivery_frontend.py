from pathlib import Path


INDEX = Path("static/index.html").read_text(encoding="utf-8")
VERSION = Path("app/version.py").read_text(encoding="utf-8")


def test_release_metadata_describes_p0_31_without_claiming_data_backfill() -> None:
    assert "_V022213_CHANGES = [" in VERSION
    assert "新增受控的历史送货补录模式" in VERSION
    assert "不自动修改既有送货、回单、对账、开票或收款数据" in VERSION
    assert "gp51v8x9z40" in VERSION


def test_historical_delivery_is_explicit_and_keeps_normal_flow_compact() -> None:
    assert "补录历史送货" in INDEX
    assert 'v-model="deliveryForm.historical_backfill"' in INDEX
    assert 'v-if="deliveryForm.historical_backfill || deliveryForm.editingId"' in INDEX
    assert "普通送货自动使用服务器当天" in INDEX
    assert "历史补录只能使用可追溯到正式订单的明细" in INDEX
    assert "payload.idempotency_key = this.deliveryForm.idempotency_key" in INDEX


def test_actual_date_correction_and_reconciliation_month_are_separate() -> None:
    assert "实际送货日期已更正" in INDEX
    # The compact delivery list now exposes date correction through the shared
    # dispatched-document editor instead of adding another row-level button.
    assert "编辑待回单送货单" in INDEX
    assert "deliveryForm.editing_status==='dispatched'" in INDEX
    assert "/actual-date`" in INDEX
    assert "ERP录入时间不会改变" in INDEX
    assert "对账归属月份" in INDEX
    assert "/api/finance/reconciliation-month-options" in INDEX
    assert "只决定进入哪个月的对账，不改实际送货和签收日期" in INDEX
    assert "payload.expected_version = Number(this.receiptForm.version || 1)" in INDEX
    assert "idempotency_key:this.statementForm.idempotency_key" in INDEX
