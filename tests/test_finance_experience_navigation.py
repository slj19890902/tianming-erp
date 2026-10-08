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
    view_start = html.index('          async setFinanceView(')
    view_end = html.index('          toggleFinanceCurrentGroup(', view_start)
    view_method = html[view_start:view_end]
    code = 'const assert=require("node:assert/strict");const month=()=>"2026-09";const vm={' + methods + view_method + r'''};
(async () => {
assert.equal(vm.financeReadinessLabel({blockers:[{code:'month_close_workflow_pending'}]}),'资料已齐');
assert.equal(vm.financeReadinessLabel({blockers:[{code:'actual_material_cost_lineage_incomplete'}]}),'有资料待补');
assert.equal(vm.financeReadinessLabel(null),'尚未读取');
assert.equal(vm.financeReadinessLabel({blockers:[{code:'actual_material_cost_lineage_incomplete'},{code:'month_close_workflow_pending'}],material_cost:{management_cost_ready:true,supplemental_source_count:5}}),'参考成本已补齐');
assert.equal(vm.financeReadinessLabel({blockers:[{code:'manufacturing_cost_unallocated'}],material_cost:{management_cost_ready:true,supplemental_source_count:5}}),'有资料待补');
assert.equal(vm.financeReadinessHint({blockers:[{code:'month_close_workflow_pending'}]}),'可以查看管理月报；正式锁月功能尚未启用。');
vm.financeOverviewMonth='2026-08';vm.financeFilters={};vm.financeCostFilters={};vm.pages={};vm.canViewFinanceCosts=true;
let financeLoads=0;vm.invalidatePageCache=()=>{};vm.loadFinance=async()=>{financeLoads++;};
await vm.openFinanceMonthlyAction('cost_drafts');assert.equal(vm.financeView,'expenses');
assert.equal(vm.financeCostMonth,'2026-08');assert.equal(vm.financeCostFilters.status,'draft');
await vm.openFinanceMonthlyAction('close_readiness');assert.equal(vm.financeView,'expenses');
assert.equal(vm.financeCostFilters.status,'');
await vm.openFinanceMonthlyAction('pending_invoice');assert.equal(vm.financeView,'current');
assert.equal(vm.financeFilters.statement_month,'2026-08');
assert.equal(vm.financeFilters.balance_type,'pending_invoice');assert.equal(vm.financeFilters.customer_id,'');
assert.equal(financeLoads,3);
})().catch(error=>{console.error(error);process.exit(1);});
'''
    result = subprocess.run(['node','-e',code],capture_output=True,text=True)
    assert result.returncode == 0, result.stderr
