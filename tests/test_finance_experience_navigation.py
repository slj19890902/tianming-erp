from pathlib import Path
import subprocess


def test_finance_task_navigation_and_month_readiness():
    html = Path('static/index.html').read_text(encoding='utf-8')
    assert 'aria-label="财务办理事项"' in html
    assert "financeView==='expenses'" in html
    assert 'financeReadinessLabel(financeOverview.cost_pool)' in html
    start = html.index('          financeReadinessLabel(')
    end = html.index('          async exportFinanceManagementReport(', start)
    methods = html[start:end]
    code = 'const assert=require("node:assert/strict");const month=()=>"2026-09";const vm={' + methods + r'''};
assert.equal(vm.financeReadinessLabel({blockers:[{code:'month_close_workflow_pending'}]}),'资料已齐');
assert.equal(vm.financeReadinessLabel({blockers:[{code:'actual_material_cost_lineage_incomplete'}]}),'有资料待补');
assert.equal(vm.financeReadinessLabel(null),'尚未读取');
assert.equal(vm.financeReadinessHint({blockers:[{code:'month_close_workflow_pending'}]}),'可以查看管理月报；正式锁月功能尚未启用。');
vm.financeOverviewMonth='2026-08';vm.financeFilters={};vm.financeCostFilters={};vm.pages={};vm.setFinanceView=v=>v;
assert.equal(vm.openFinanceMonthlyAction('cost_drafts'),'expenses');
assert.equal(vm.financeCostMonth,'2026-08');assert.equal(vm.financeCostFilters.status,'draft');
assert.equal(vm.openFinanceMonthlyAction('close_readiness'),'expenses');
assert.equal(vm.financeCostFilters.status,'');
assert.equal(vm.openFinanceMonthlyAction('pending_invoice'),'current');
assert.equal(vm.financeFilters.statement_month,'2026-08');
'''
    result = subprocess.run(['node','-e',code],capture_output=True,text=True)
    assert result.returncode == 0, result.stderr
