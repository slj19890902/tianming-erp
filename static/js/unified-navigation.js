(function (global) {
  'use strict';
  const allowedPage = (vm, page) => typeof vm.pageAllowed === 'function' && vm.pageAllowed(page);
  function describe(vm) {
    const items = [];
    const add = (key, label, active, more = false) => items.push({key, label, active:!!active, more});
    let label = '', flow = false;
    if (vm.activePage === 'warehouse' && allowedPage(vm, 'warehouse')) {
      label = '仓库';
      add('warehouse:map', '仓库地图', vm.warehouseView === 'map');
      add('warehouse:ledger', '库存列表', vm.warehouseView === 'ledger' && ['finished','semi_finished'].includes(vm.warehouseLedgerTab));
      add('warehouse:movements', '库存流水', vm.warehouseView === 'ledger' && vm.warehouseLedgerTab === 'movements');
      if (vm.warehouseStocktakeVisible()) add('warehouse:stocktake_review', '盘点记录', vm.warehouseView === 'ledger' && vm.warehouseLedgerTab === 'stocktake_review', true);
      add('warehouse:molds', '模具与印版', vm.warehouseView === 'ledger' && ['molds','printing_plates'].includes(vm.warehouseLedgerTab), true);
    } else if (vm.activePage === 'finance' && allowedPage(vm, 'finance')) {
      label = '对账与开票';
      [['overview','经营概览'],['current','客户对账'],['invoice_tasks','开票办理'],['collections','收款办理'],['payables','供应商付款'],['expenses','日常费用']].forEach(([key,title], index) => {
        if (key === 'invoice_tasks' && !vm.canViewInvoiceTasks || key === 'expenses' && !vm.canViewFinanceCosts) return;
        add('finance:'+key,title,vm.financeView === key,index > 2);
      });
    } else if (vm.businessFlowCurrentStep) {
      label = '按顺序做'; flow = true;
      (vm.businessFlowSteps || []).filter(step => allowedPage(vm,step.page)).forEach(step => add('page:'+step.page,step.label,vm.businessFlowCurrentStep.key === step.key));
    } else if (vm.currentNavigationGroup) {
      label = vm.currentNavigationGroup.label;
      vm.currentNavigationGroup.pages.filter(page => allowedPage(vm,page.key)).forEach((page,index) => add('page:'+page.key,page.label,vm.activePage === page.key,index > 3));
    } else {
      const current = (vm.menus || []).find(menu => vm.isMenuActive(menu.key));
      if (current) { label = current.label; add('menu:'+current.key,current.label,true); }
    }
    const admin = ['admin','boss'].includes(vm.user?.role);
    const metrics = vm.warehouseTwinMetrics || {};
    return { label, flow, items, actorId:vm.user.id, generation:vm.authGeneration,
      name:vm.user.real_name || vm.user.username, username:vm.user.username,
      role:vm.roleLabel(vm.user.role), uiMode:vm.uiMode === 'large' ? 'large' : 'standard',
      uiModeSaving:!!vm.uiModeSaving, canApprove:!!(admin || vm.canSubmitBusinessRequest),
      approvalLabel:admin ? '审批中心' : '业务申请',
      busy:!!(vm.loading || vm.modal?.type || vm.productionEntry || vm.warehouseNavigating),
      overview:vm.activePage === 'warehouse' && vm.warehouseView === 'map' ? {
        floor:String(vm.warehouseTwinFloor || ''),
        lots:Number(metrics.active_lots || 0), occupied:Number(metrics.occupied_locations || 0),
        locations:Number(metrics.mapped_locations || 0), unlocated:Number(metrics.unlocated_finished || 0),
        conflicts:Number(metrics.column_conflicts || 0)
      } : null };
  }
  async function execute(vm, key) {
    const ui = describe(vm);
    // Navigation and refresh must not silently dismiss original forms.
    if (ui.busy && !['ui:standard','ui:large'].includes(key)) return 'busy';
    if (key === 'ui:standard' || key === 'ui:large') {
      if (ui.uiModeSaving) return 'busy';
      await vm.setUiMode(key.slice(3)); return 'done';
    }
    if (key === 'action:refresh') { await vm.refreshCurrent(); return 'done'; }
    if (key === 'action:password') { await vm.openChangePassword(); return 'done'; }
    if (key === 'action:logout') { await vm.logout(); return 'done'; }
    if (key.startsWith('menu:')) {
      const name = key.slice(5);
      if (!(vm.menus || []).some(item => item.key === name)) return 'denied';
      await vm.goMenu(name); return 'done';
    }
    if (!ui.items.some(item => item.key === key)) return 'denied';
    const [kind, value] = key.split(':');
    if (kind === 'warehouse') {
      await vm.chooseWarehouseView(value === 'map' ? 'map' : 'ledger', ['map','ledger'].includes(value) ? null : value);
    } else if (kind === 'finance') {
      await vm.setFinanceView(value);
    } else if (kind === 'page' && allowedPage(vm,value)) {
      // Do not reset a selected business filter merely by clicking its active tab.
      if (vm.activePage !== value) await vm.go(value);
    } else return 'denied';
    return 'done';
  }
  global.ERPUnifiedNavigation = {describe, execute};
})(window);
