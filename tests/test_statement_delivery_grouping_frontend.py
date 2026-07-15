from pathlib import Path
import re
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_customer_statement_cycle_field_matches_the_monthly_boundary_contract() -> None:
    assert 'v-model.number="customerForm.statement_cycle_start_day"' in INDEX
    assert 'type="number" min="1" max="28" step="1"' in INDEX
    assert "statement_cycle_start_day: 20" in INDEX
    assert "20表示上月20日至本月19日归本月" in INDEX
    assert "statementCycleStartDay < 1 || statementCycleStartDay > 28" in INDEX


def test_statement_modal_groups_deliveries_and_submits_delivery_ids() -> None:
    assert "Array.isArray(data?.deliveries) ? data.deliveries" in INDEX
    assert "statement_month:this.statementForm.statement_month" in INDEX
    assert "送货日期" in INDEX
    assert "送货单号" in INDEX
    assert "明细数" in INDEX
    assert "实收总数" in INDEX
    assert "应收总额" in INDEX
    assert "toggleStatementDeliveryExpanded(row)" in INDEX
    assert "row.items" in INDEX
    assert "this.statementForm.selected[row.delivery_id] = true" in INDEX
    assert "delivery_ids:ids" in INDEX
    assert "statementForm.selected[row.return_receipt_item_id]" not in INDEX
    assert "return_receipt_item_ids:ids" not in INDEX


def test_statement_delivery_selection_blocks_exception_groups_and_select_all_skips_them() -> None:
    assert ":disabled=" in INDEX
    assert "row.selection_blocked" in INDEX
    assert "row.exception_reason" in INDEX
    assert "异常历史" in INDEX
    assert "this.pendingStatements.filter(row => !row.selection_blocked)" in INDEX
    assert "this.statementForm.selected[row.delivery_id]" in INDEX


def test_inline_frontend_script_is_syntactically_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for frontend syntax validation"
    scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, flags=re.DOTALL
        )
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "statement-delivery-grouping-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
